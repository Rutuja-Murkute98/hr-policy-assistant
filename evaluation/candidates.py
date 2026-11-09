"""Add questions from real use to the candidates waiting for review.

    python -m evaluation.candidates import test_candidates.json

The file comes from the admin screen's Feedback tab ("Prepare test questions"). Questions already in the golden set
or the candidates are skipped. Review and promote them with: streamlit run evaluation/review_app.py
"""
import argparse
import json
import sys
from pathlib import Path

from evaluation import review


def import_file(source: Path, candidates_file: Path = review.CANDIDATES_FILE,
                golden_file: Path = review.GOLDEN_FILE) -> int:
    new = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(new, list):
        raise review.ReviewError("Expected a list of questions, as the admin screen downloads")
    candidates, golden = review.load(candidates_file), review.load(golden_file)
    added = review.add_candidates(candidates, golden, new)
    review.save(candidates_file, candidates)
    return added


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["import"])
    parser.add_argument("file", type=Path)
    args = parser.parse_args(argv)
    try:
        added = import_file(args.file)
    except (OSError, ValueError) as error:
        print(f"Couldn't import {args.file}: {error}", file=sys.stderr)
        return 1
    print(f"Added {added} new candidates. Review them with: streamlit run evaluation/review_app.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
