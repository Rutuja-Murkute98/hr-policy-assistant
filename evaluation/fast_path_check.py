"""Measure, with real searches and no chat model, which first messages would skip the model reading them.

    python -m evaluation.fast_path_check
    python -m evaluation.fast_path_check --compare-run phase9

A first message skips it when the text check finds no personal, action, old-version or rule words, and its search is
at least as strong as SKIP_UNDERSTAND_CONFIDENCE. This shows, at several strengths, how many messages would skip it,
and whether any of them is labelled as something other than a policy question. It also compares with the intent the
model gave in saved workflow runs: the recorded agent run, and a golden set run.
"""
import argparse
import json

from app import config
from app.agent.graph import looks_like_policy_question
from app.db import get_engine
from app.rag.embedding_cache import QuestionEmbeddingCache
from app.rag.rerank import get_reranker
from app.rag.retrieval import SearchFilters
from app.rag.service import GREETING, RagService
from app.rag.store import PolicyIndex, get_database_uri, get_store, run_migrations
from evaluation.golden import load_golden
from evaluation.run_agent_eval import AGENT_SET
from evaluation.run_eval import CACHE, REPORTS, load_json, save_json

STRENGTHS = (4.0, 4.5, 5.0, 6.0)


def strength(service: RagService, message: str) -> float | None:
    """The search's confidence, or None if the text check already sends the message to the model."""
    if GREETING.fullmatch(message.strip()) or not looks_like_policy_question(message):
        return None
    chunks, confidence = service.search(message, SearchFilters(tenant_id=config.TENANT_ID))
    return confidence if chunks else None


def summarise(rows: list[dict], label: str) -> dict:
    """At each strength: how many would skip the model, and which of those aren't labelled policy questions."""
    result: dict = {"messages": len(rows), "pass_text_check": sum(r["confidence"] is not None for r in rows)}
    for limit in STRENGTHS:
        skipped = [r for r in rows if r["confidence"] is not None and r["confidence"] >= limit]
        result[str(limit)] = {"skipped": len(skipped),
                              "not_policy_question": [r["id"] for r in skipped if r[label] != "policy_question"]}
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compare-run", default="phase9", help="a saved workflow run on the golden set")
    args = parser.parse_args()
    run_migrations()
    engine = get_engine(get_database_uri())
    service = RagService(PolicyIndex(get_store()), None, get_reranker(),
                         query_cache=QuestionEmbeddingCache(engine, config.TENANT_ID))

    agent = json.loads(AGENT_SET.read_text(encoding="utf-8"))["items"]
    recorded = load_json(CACHE / "agent_eval.json")  # the recorded agent run, where the model read every message
    agent_rows = [{"id": item["id"], "label": item["expected_intent"], "confidence": strength(service, item["message"]),
                   "model_intent": recorded.get(item["id"], {}).get("intent", "")}
                  for item in agent if item["review_status"] != "rejected"]

    saved = load_json(CACHE / f"answers_{args.compare_run}.json")
    golden_rows = []
    for item in load_golden():
        if item["history"]:
            continue
        # The intent the model gave when it read the message. An overruled "out of scope" is saved as a policy question
        golden_rows.append({"id": item["id"], "label": "out_of_scope" if item["must_refuse"] else "policy_question",
                            "model_intent": saved.get(item["id"], {}).get("intent", ""),
                            "confidence": strength(service, item["question"])})

    report = {"strength_setting": config.SKIP_UNDERSTAND_CONFIDENCE,
              "agent_set": summarise(agent_rows, "label"),
              "agent_set_against_the_recorded_run": summarise(agent_rows, "model_intent") if recorded else None,
              "golden_first_messages": summarise(golden_rows, "model_intent") if saved else None,
              "golden_must_refuse": summarise(golden_rows, "label")}
    save_json(REPORTS / "fast_path_check.json", report)
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
