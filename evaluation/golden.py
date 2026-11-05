"""Load the golden set: questions with chunk-level labels, drafted for the project owner to review."""
import json
from pathlib import Path

GOLDEN_FILE = Path(__file__).resolve().parent / "data" / "golden_set.json"
REVIEW_STATES = {"unreviewed", "approved", "rejected"}
CATEGORIES = {"normal", "unanswerable", "ambiguous", "multi_hop", "follow_up", "conflict", "exception", "adversarial"}


def load_golden(path: Path = GOLDEN_FILE) -> list[dict]:
    """Return the items to evaluate. Items the owner rejected are left out."""
    items = json.loads(path.read_text(encoding="utf-8"))["items"]
    return [item for item in items if item["review_status"] != "rejected"]


def split_of(item: dict) -> str:
    """Put each item in a fixed group, so settings can be tuned on one and checked on the other."""
    return "tune" if int(item["id"].rsplit("-", 1)[1]) % 2 == 0 else "check"


def review_summary(items: list[dict]) -> dict[str, int]:
    return {state: sum(item["review_status"] == state for item in items) for state in sorted(REVIEW_STATES)}
