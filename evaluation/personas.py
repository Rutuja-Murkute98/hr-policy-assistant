"""Questions whose right answer depends on who asks, each asked as two employees. See the plan's Phase 11.

    python -m evaluation.personas --name phase11
    python -m evaluation.personas --name phase11_anonymous --anonymous

Each item in evaluation/data/persona_set.json names two employees from the fake HR data and the facts each one's answer
must contain. --anonymous asks every question with no employee, which shows what knowing the asker adds. Answers aren't
judged, to save quota: the facts, the search and the citations are scored as in the golden set.
"""
import argparse
import json
from pathlib import Path

from app import config
from app.agent.hrms import DEFAULT_DATA_FILE
from evaluation.metrics import facts_present
from evaluation.run_eval import (
    CACHE,
    REPORTS,
    answer_metrics,
    call_with_retry,
    complete,
    load_json,
    save_json,
    timed,
)

PERSONA_FILE = Path(__file__).parent / "data" / "persona_set.json"


def load_personas(path: Path = PERSONA_FILE) -> list[dict]:
    """The items to run, checked so a typo can't quietly score nothing. Items the owner rejected are left out."""
    employees = json.loads(DEFAULT_DATA_FILE.read_text(encoding="utf-8"))["employees"]
    items = [item for item in json.loads(path.read_text(encoding="utf-8"))["items"]
             if item["review_status"] != "rejected"]
    for item in items:
        askers = [asker["employee"] for asker in item["askers"]]
        if len(askers) != 2 or len(set(askers)) != 2:
            raise ValueError(f"{item['id']} needs two different employees")
        if not item["relevant_chunks"]:
            raise ValueError(f"{item['id']} has no labelled chunks")
        for asker in item["askers"]:
            if asker["employee"] not in employees:
                raise ValueError(f"{item['id']} names {asker['employee']}, who isn't in the fake HR data")
            if not asker["must_contain"]:
                raise ValueError(f"{item['id']} has no facts to check for {asker['employee']}")
    return items


def expand(items: list[dict], anonymous: bool = False) -> list[dict]:
    """One golden-style item per question and asker, so the golden set's scoring applies unchanged."""
    return [{"id": f"{item['id']}@{asker['employee']}", "category": "persona", "item": item["id"],
             "question": item["question"], "history": [], "relevant_chunks": item["relevant_chunks"],
             "must_contain": asker["must_contain"], "must_refuse": False,
             "employee": None if anonymous else asker["employee"]}
            for item in items for asker in item["askers"]]


def ask_as(service, question: dict):
    """Ask once, as that employee, with nothing saved. With no employee, it's an anonymous question."""
    return service.ask(question["question"], user_id=question["employee"], save_history=False)


def both_right(items: list[dict], rows: dict[str, dict]) -> float:
    """The share of items where both employees got the facts that fit them, the measure that matters here."""
    pairs: dict[str, list[bool]] = {}
    for question in items:
        pairs.setdefault(question["item"], []).append(
            facts_present(rows[question["id"]]["answer"], question["must_contain"]))
    return round(sum(all(results) for results in pairs.values()) / len(pairs), 3) if pairs else 0.0


def run(args) -> None:
    from app.workflow import create_workflow

    config.CACHE_ANSWERS = False  # a stored answer would hide what the model does now
    questions = expand(load_personas(), args.anonymous)[: args.limit or None]
    progress_file = CACHE / f"personas_{args.name}.json"
    saved = load_json(progress_file)
    service = create_workflow()
    try:
        for number, question in enumerate(questions, start=1):
            if question["id"] in saved:
                continue
            asked = next((saved[other["id"]] for other in questions if args.anonymous and other["id"] in saved
                          and other["item"] == question["item"]), None)
            if asked is not None:  # with no employee, both askers get the same answer
                saved[question["id"]] = asked
                save_json(progress_file, saved)
                continue
            result, seconds = call_with_retry(
                lambda question=question: timed(lambda: complete(ask_as(service, question))))
            saved[question["id"]] = {
                "answer": result.answer, "answered": result.answered, "abstain_reason": result.abstain_reason,
                "cited": [source["chunk_id"] for source in result.sources],
                "retrieved": [chunk.id for chunk in result.chunks], "question_used": result.question_used,
                "seconds": round(seconds, 2), "step_ms": dict(result.step_ms or {}), "intent": result.intent,
                "persona": result.persona}
            save_json(progress_file, saved)
            print(f"{number}/{len(questions)} {question['id']} answered={result.answered}", flush=True)
    finally:
        service.close()
    metrics = {**answer_metrics(questions, saved), "both_right": both_right(questions, saved)}
    missing = [question["id"] for question in questions
               if not facts_present(saved[question["id"]]["answer"], question["must_contain"])]
    report = {"name": args.name, "anonymous": args.anonymous, "metrics": metrics, "missing_facts": missing}
    save_json(REPORTS / f"personas_{args.name}.json", report)
    print(json.dumps(metrics, indent=1))
    print(json.dumps(missing))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="new")
    parser.add_argument("--anonymous", action="store_true", help="ask every question with no employee, to compare")
    parser.add_argument("--limit", type=int, default=0, help="run at most this many questions")
    run(parser.parse_args())
