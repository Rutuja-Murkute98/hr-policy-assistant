"""Check every policy for clashes with the real model, and see how many of the five known contradictions it finds.

    python -m evaluation.conflict_check

The five are listed in docs/FINAL_REPORT.md. Each is matched by a phrase from each side, so a clash between any section
holding one phrase and any holding the other counts. Every other clash it raises is listed, for a person to judge. No
embedding is made, and it uses about one answer call per 15 pairs of sections, paced for the free tier. See D60.
"""
import json
from typing import Any

from app.policy_checks import PolicyChecks
from app.rag.store import run_migrations
from app.workflow import create_workflow
from evaluation.run_eval import REPORTS, save_json

PAUSE_SECONDS = 5  # the free tier allows 15 calls a minute
KNOWN = [  # (what it's about, a phrase from one side, a phrase from the other)
    ("Sick leave on probation", "entitled to 12 days of Sick Leave", "Sick Leave of up to 6 days"),
    ("Casual leave on probation", "6 days of Casual Leave", "Casual Leave of up to 3 days"),
    ("Referral bonus, non-technical Band 2", "the referral bonus is Rs. 15,000", "20,000 for non-technical roles"),
    ("Time to raise a salary query", "within 30 days of the payslip", "within 5 working days of salary credit"),
    ("ESOP vesting", "vest over 4 years", "Year 5 is 100 percent"),
]


def holding(chunks: dict, phrase: str) -> set[str]:
    return {chunk_id for chunk_id, chunk in chunks.items() if phrase.lower() in chunk.page_content.lower()}


def main() -> None:
    run_migrations()
    workflow = create_workflow()
    try:
        checks = PolicyChecks(workflow.knowledge)
        run = checks.check_clashes(workflow.llm, pause_seconds=PAUSE_SECONDS)
        chunks = workflow.knowledge.index.stored_chunks()
        clashes = checks.clashes()
        pairs = {frozenset((clash["chunk_a"], clash["chunk_b"])) for clash in clashes}
        known: list[dict[str, Any]] = []
        for about, one, other in KNOWN:
            first, second = holding(chunks, one), holding(chunks, other)
            found = any(frozenset((a, b)) in pairs for a in first for b in second if a != b)
            known.append({"about": about, "found": found, "sections": [sorted(first), sorted(second)]})
            print(f"{'found ' if found else 'MISSED'} {about}", flush=True)
        known_pairs = {frozenset((a, b)) for item in known for a in item["sections"][0] for b in item["sections"][1]}
        others = [clash for clash in clashes if frozenset((clash["chunk_a"], clash["chunk_b"])) not in known_pairs]
        report = {**run, "known_found": sum(item["found"] for item in known), "known": known,
                  "clashes_raised": len(clashes), "other_clashes": others}
    finally:
        workflow.close()
    save_json(REPORTS / "conflict_check.json", report)
    print(json.dumps({key: value for key, value in report.items() if key not in ("known", "other_clashes")}, indent=1))
    for clash in others:
        print(f"- {clash['file_a']} / {clash['file_b']}: {clash['explanation']}")


if __name__ == "__main__":
    main()
