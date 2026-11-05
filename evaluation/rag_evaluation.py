"""Stage G: measure the system, so "it looked fine" isn't the only evidence.

    venv\\Scripts\\python -m evaluation.rag_evaluation            simple checks (about 5 minutes)
    venv\\Scripts\\python -m evaluation.rag_evaluation --ragas    then RAGAS scores (slow, many LLM calls)

The simple checks need no LLM to judge them:
  - retrieval: did search find the policy the answer is in? Compared with the old chunking.
  - answers: does each answer contain the known facts, and are off-topic questions refused?
Run this after every change. Results are saved in evaluation/results/.
"""
import argparse
import json
import sys
import time
import types
from pathlib import Path

from app.rag.prompt import FALLBACK_ANSWER
from app.rag.service import create_service
from app.rag.store import get_store
from evaluation.test_set import ANSWERABLE, UNANSWERABLE

RESULTS_DIR = Path(__file__).resolve().parent / "results"
ANSWERS_FILE = RESULTS_DIR / "answers.json"
PAUSE_SECONDS = 5  # stay under the chat model's free-tier limit
OLD_COLLECTION = "hr_policies"  # the fixed-size chunks from mini_rag.py's first version


def run_simple_checks() -> dict:
    service = create_service()
    old_store = get_store(OLD_COLLECTION)
    rows, old_hits = [], 0

    for item in ANSWERABLE:
        old_chunks = old_store.similarity_search_by_vector(
            service.store.embeddings.embed_query(item["question"]), k=4)
        old_hits += any(Path(c.metadata["source"]).name == item["file"] for c in old_chunks)

        result = service.ask(item["question"])
        found = [c.metadata["source"] for c in result.chunks]
        answer = result.answer.lower()
        missing = [text for text in item["must_contain"] if text.lower() not in answer]
        rows.append({**item, "answer": result.answer, "retrieved_files": found,
                     "contexts": [c.page_content for c in result.chunks],
                     "retrieval_hit": item["file"] in found, "missing_facts": missing})
        status = "ok" if not missing else f"MISSING {missing}"
        print(f"[{status}] {item['question']}\n    {result.answer}", flush=True)
        time.sleep(PAUSE_SECONDS)

    refusals = []
    for question in UNANSWERABLE:
        answer = service.ask(question).answer
        refusals.append({"question": question, "answer": answer, "refused": answer == FALLBACK_ANSWER})
        print(f"[{'refused' if refusals[-1]['refused'] else 'ANSWERED'}] {question}\n    {answer}", flush=True)
        time.sleep(PAUSE_SECONDS)

    total = len(ANSWERABLE)
    summary = {
        "retrieval_hit_rate_new": sum(r["retrieval_hit"] for r in rows) / total,
        "retrieval_hit_rate_old": old_hits / total,
        "answers_with_all_facts": sum(not r["missing_facts"] for r in rows) / total,
        "refused_when_it_should": sum(r["refused"] for r in refusals) / len(refusals),
    }
    RESULTS_DIR.mkdir(exist_ok=True)
    ANSWERS_FILE.write_text(json.dumps({"summary": summary, "answers": rows, "refusals": refusals},
                                       indent=2), encoding="utf-8")
    print("\n" + json.dumps(summary, indent=2))
    return summary


def run_ragas() -> None:
    # ragas 0.4.3 imports a module that newer langchain-community removed. It's only used
    # for Google Vertex AI, which this project doesn't use, so an empty stand-in is safe.
    stand_in = types.ModuleType("langchain_community.chat_models.vertexai")
    stand_in.ChatVertexAI = type("ChatVertexAI", (), {})
    sys.modules.setdefault(stand_in.__name__, stand_in)

    from langchain_google_genai import ChatGoogleGenerativeAI
    from ragas import EvaluationDataset, evaluate
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import Faithfulness, LLMContextPrecisionWithReference, LLMContextRecall
    from ragas.run_config import RunConfig

    from app import config

    rows = json.loads(ANSWERS_FILE.read_text(encoding="utf-8"))["answers"]
    dataset = EvaluationDataset.from_list([
        {"user_input": r["question"], "response": r["answer"],
         "retrieved_contexts": r["contexts"], "reference": r["ground_truth"]}
        for r in rows
    ])
    judge = LangchainLLMWrapper(ChatGoogleGenerativeAI(model=config.CHAT_MODEL))
    result = evaluate(dataset,
                      metrics=[Faithfulness(), LLMContextRecall(), LLMContextPrecisionWithReference()],
                      llm=judge,
                      run_config=RunConfig(max_workers=2, timeout=180, max_retries=6, max_wait=70))
    table = result.to_pandas()
    table.to_csv(RESULTS_DIR / "ragas_scores.csv", index=False)
    means = table.mean(numeric_only=True).round(3).to_dict()
    unscored = int(table.select_dtypes("number").isna().sum().sum())
    (RESULTS_DIR / "ragas_summary.json").write_text(
        json.dumps({"means": means, "unscored_cells": unscored}, indent=2), encoding="utf-8")
    print("\nRAGAS scores:", json.dumps(means, indent=2), "\nUnscored cells:", unscored)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate the HR helpdesk")
    parser.add_argument("--ragas", action="store_true", help="also compute RAGAS scores")
    parser.add_argument("--ragas-only", action="store_true", help="score the saved answers again")
    args = parser.parse_args()
    if not args.ragas_only:
        run_simple_checks()
    if args.ragas or args.ragas_only:
        run_ragas()
