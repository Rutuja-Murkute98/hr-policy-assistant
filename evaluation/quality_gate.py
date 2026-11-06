"""Fail CI if the recorded evaluation is out of date, or its deterministic results are too low.

CI has no model key and doesn't hold the real embeddings, so it can't repeat the evaluation.
Instead this checks the committed results, and that they match the current code and golden set.

To refresh them after changing retrieval or the golden set:
    python -m evaluation.run_eval answers --name workflow --workflow
    python -m evaluation.quality_gate --record workflow
"""
import argparse
import json
import sys

from evaluation.run_eval import METRICS_FILE, REPORTS, fingerprint

# What's enforced now. These are floors to catch regressions, not the brief's targets.
FLOORS = {
    "recall_at_5": 0.85,
    "refused_when_it_should": 0.90,
    "citations_valid": 0.95,
    "facts_correct": 0.70,
}


def record(name: str) -> None:
    answers = json.loads((REPORTS / f"answers_{name}.json").read_text(encoding="utf-8"))
    METRICS_FILE.write_text(json.dumps({"fingerprint": fingerprint(), "metrics": answers["metrics"]}, indent=1) + "\n",
                            encoding="utf-8", newline="\n")
    print(f"Recorded {METRICS_FILE.name}")


def check(recorded: dict, current_fingerprint: str) -> list[str]:
    problems = []
    if recorded["fingerprint"] != current_fingerprint:
        problems.append("The retrieval code or golden set changed since the evaluation last ran.")
    for name, floor in FLOORS.items():
        value = recorded["metrics"].get(name)
        if value is None:
            problems.append(f"{name} wasn't recorded.")
        elif value < floor:
            problems.append(f"{name} is {value}, below the floor of {floor}.")
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description="Check the recorded evaluation")
    parser.add_argument("--record", metavar="NAME", help="record this answer run's results as the latest")
    args = parser.parse_args()
    if args.record:
        record(args.record)
        return
    if not METRICS_FILE.exists():
        sys.exit(f"{METRICS_FILE} is missing. Run the evaluation first.")
    problems = check(json.loads(METRICS_FILE.read_text(encoding="utf-8")), fingerprint())
    if problems:
        sys.exit("Quality gate failed:\n- " + "\n- ".join(problems))
    print("Quality gate passed")


if __name__ == "__main__":
    main()
