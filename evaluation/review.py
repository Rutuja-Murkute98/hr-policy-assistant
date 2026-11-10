"""Review the evaluation labels: approve, reject or correct each item, and turn real questions into new items.

The review screen in evaluation/review_app.py uses these functions. They read and write the label files directly,
so every change shows up in git as a normal diff.

Questions from real use (unanswered ones and answers with a thumbs down) are kept in candidates.json first, not in
the golden set. A candidate has no labels yet, so in the golden set it would count as answerable with nothing to
check, and distort the results. The reviewer promotes it once they've decided what the right answer is.
"""
import json
import re
from functools import cache
from pathlib import Path

from evaluation.golden import CATEGORIES, GOLDEN_FILE, REVIEW_STATES

DATA = GOLDEN_FILE.parent
AGENT_FILE = DATA / "agent_set.json"
CANDIDATES_FILE = DATA / "candidates.json"
CACHE = DATA.parent.parent / ".cache"
CANDIDATE_STATES = {"new", "promoted", "dismissed"}
CANDIDATE_SOURCES = {"unanswered", "thumbs_down", "new_section"}
INTENTS = {"policy_question", "personal_data_request", "action_request", "out_of_scope", "greeting"}
ROUTES = {"rag", "self_service", "refuse", "greeting", "needs_identity"}
EMPTY_CANDIDATES = {"status": "Questions from real use, waiting to be reviewed. They aren't evaluated until promoted.",
                    "items": []}


class ReviewError(ValueError):
    """A change the label files can't accept. The message says why."""


def load(path: Path) -> dict:
    if not path.exists() and path.name == CANDIDATES_FILE.name:
        return json.loads(json.dumps(EMPTY_CANDIDATES))
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, data: dict) -> None:
    """Write in the files' existing format, so a change only touches the lines it changes."""
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def find(data: dict, item_id: str) -> dict:
    for item in data["items"]:
        if item["id"] == item_id:
            return item
    raise ReviewError(f"There's no item {item_id}")


def progress(items: list[dict]) -> dict:
    """How far the review has got. Only approved and rejected items count as decided."""
    counts = {state: sum(item.get("review_status") == state for item in items) for state in sorted(REVIEW_STATES)}
    decided = counts["approved"] + counts["rejected"]
    return {**counts, "total": len(items), "decided_share": round(decided / len(items), 3) if items else 0.0}


def set_review(data: dict, item_id: str, status: str, notes: str | None = None) -> dict:
    if status not in REVIEW_STATES:
        raise ReviewError(f"A review status is one of: {', '.join(sorted(REVIEW_STATES))}")
    item = find(data, item_id)
    item["review_status"] = status
    if notes is not None:
        item["notes"] = notes.strip()
    return item


def update_golden_labels(data: dict, item_id: str, *, category: str, must_refuse: bool, must_contain: list[str],
                         relevant_chunks: list[str], chunk_texts: dict[str, str]) -> dict:
    """Correct a golden item's labels, keeping the rules the tests check, so a saved label never fails CI.

    Chunk ids have to exist, so a typo can't quietly lower recall, and each expected fact has to appear in the
    labelled chunks, so the bot is never marked down for leaving out something the policy doesn't say.
    """
    if category not in CATEGORIES:
        raise ReviewError(f"A category is one of: {', '.join(sorted(CATEGORIES))}")
    item = find(data, item_id)
    facts = [fact.strip() for fact in must_contain if fact.strip()]
    chunks = list(dict.fromkeys(relevant_chunks))
    unknown = [chunk for chunk in chunks if chunk not in chunk_texts]
    if unknown:
        raise ReviewError(f"These chunks don't exist: {', '.join(unknown)}")
    if must_refuse and (chunks or facts):
        raise ReviewError("An item the bot must refuse can't have chunks or facts to find")
    if not must_refuse and not chunks:
        raise ReviewError("An answerable item needs at least one chunk that answers it")
    labelled_text = " ".join(chunk_texts[chunk].lower() for chunk in chunks)
    absent = [fact for fact in facts if fact.lower() not in labelled_text]
    if absent:
        raise ReviewError(f"These facts aren't in the chosen chunks: {', '.join(absent)}")
    if (category == "follow_up") != bool(item.get("history")):
        raise ReviewError("Only an item with earlier messages can be a follow-up, and every such item is one")
    item.update(category=category, must_refuse=must_refuse, relevant_chunks=chunks, must_contain=facts)
    return item


def update_agent_labels(data: dict, item_id: str, *, expected_intent: str, expected_route: str,
                        expected_tool: str | None) -> dict:
    if expected_intent not in INTENTS:
        raise ReviewError(f"An intent is one of: {', '.join(sorted(INTENTS))}")
    if expected_route not in ROUTES:
        raise ReviewError(f"A route is one of: {', '.join(sorted(ROUTES))}")
    item = find(data, item_id)
    item.update(expected_intent=expected_intent, expected_route=expected_route, expected_tool=expected_tool or None)
    return item


def next_id(items: list[dict], category: str) -> str:
    """The next free id in a category, such as normal-056."""
    numbers = [int(item["id"].rsplit("-", 1)[1]) for item in items if item["id"].rsplit("-", 1)[0] == category]
    return f"{category}-{max(numbers, default=0) + 1:03d}"


def normalise(question: str) -> str:
    return re.sub(r"\s+", " ", question).strip().lower().rstrip("?.! ")


def add_candidates(candidates: dict, golden: dict, new: list[dict]) -> int:
    """Add questions from real use, skipping any already in the golden set or the candidates. Returns how many."""
    seen = {normalise(item["question"]) for item in golden["items"]}
    seen |= {normalise(item["question"]) for item in candidates["items"]}
    added = 0
    for entry in new:
        question = str(entry.get("question", "")).strip()
        source = entry.get("source")
        if not question or normalise(question) in seen or source not in CANDIDATE_SOURCES:
            continue
        seen.add(normalise(question))
        candidate = {
            "id": next_id(candidates["items"], "candidate"), "question": question, "source": source,
            "asked": int(entry.get("asked") or 1), "answer": str(entry.get("answer") or ""),
            "comment": str(entry.get("comment") or ""), "last_seen": str(entry.get("last_seen") or ""),
            "status": "new"}
        if entry.get("relevant_chunks"):  # a drafted question already knows the section it's about
            candidate |= {"relevant_chunks": list(entry["relevant_chunks"]),
                          "must_contain": list(entry.get("must_contain") or [])}
        candidates["items"].append(candidate)
        added += 1
    return added


def promote_candidate(candidates: dict, golden: dict, candidate_id: str, category: str) -> dict:
    """Move a candidate into the golden set as an unreviewed item, to be labelled next."""
    if category not in CATEGORIES:
        raise ReviewError(f"A category is one of: {', '.join(sorted(CATEGORIES))}")
    candidate = find(candidates, candidate_id)
    if candidate["status"] != "new":
        raise ReviewError(f"{candidate_id} has already been {candidate['status']}")
    refuse = category == "unanswerable"
    origin = "drafted from a policy section" if candidate["source"] == "new_section" else \
        f"from real use, {candidate['source'].replace('_', ' ')}"
    item = {"id": next_id(golden["items"], category), "category": category, "question": candidate["question"],
            "history": [], "relevant_chunks": [] if refuse else list(candidate.get("relevant_chunks", [])),
            "must_contain": [] if refuse else list(candidate.get("must_contain", [])), "must_refuse": refuse,
            "review_status": "unreviewed", "notes": f"{origin.capitalize()} ({candidate_id}). Check its labels."}
    golden["items"].append(item)
    candidate["status"] = "promoted"
    return item


def dismiss_candidate(candidates: dict, candidate_id: str) -> dict:
    candidate = find(candidates, candidate_id)
    candidate["status"] = "dismissed"
    return candidate


BOILERPLATE_SECTIONS = ("Policy Review", "Roles and Responsibilities", "Policy Brief", "Scope")
MIN_DRAFT_CHARS = 200  # a shorter section is usually a heading's lead-in, not something to ask about


def uncovered_chunks(golden: dict, texts: dict[str, str], policy: str = "") -> list[str]:
    """Policy sections no golden item labels, in file order: new sections land here, and so do neglected ones."""
    covered = {chunk for item in golden["items"] for chunk in item["relevant_chunks"]}
    return [chunk_id for chunk_id, text in texts.items()
            if chunk_id not in covered and (not policy or chunk_id.split(" > ")[0] == policy)
            and not any(f"> {section}" in chunk_id or f". {section}" in chunk_id for section in BOILERPLATE_SECTIONS)
            and len(text.partition("\n\n")[2]) >= MIN_DRAFT_CHARS]


def checked_draft(chunk_id: str, text: str, question: str, facts: list[str]) -> dict | None:
    """Keep a drafted question only with facts that really are in its section, as the tests require."""
    kept = [fact.strip() for fact in facts if fact.strip() and fact.strip().lower() in text.lower()]
    if not question.strip():
        return None
    return {"question": question.strip(), "source": "new_section", "relevant_chunks": [chunk_id],
            "must_contain": kept[:3]}


@cache
def chunk_texts() -> dict[str, str]:
    """Every chunk of the sample policies, by id, made the same way as indexing does. Needs no database or model."""
    from pipeline.chunk_documents import load_chunks

    return {chunk.id: chunk.page_content for chunk in load_chunks() if chunk.id}


def saved_runs(cache_dir: Path = CACHE) -> dict[str, dict]:
    """Answers saved by earlier evaluation runs, by run name, so the reviewer can see what the bot said."""
    runs = {}
    for path in sorted(cache_dir.glob("answers_*.json")):
        try:
            runs[path.stem.removeprefix("answers_")] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return runs
