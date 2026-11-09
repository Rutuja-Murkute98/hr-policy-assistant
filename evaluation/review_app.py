"""A local screen for reviewing the evaluation labels. It edits the files in evaluation/data directly.

    venv\\Scripts\\streamlit run evaluation/review_app.py

It needs no API key, database or model: chunk text comes from the policy files, and answers from earlier runs come
from .cache. Review the most common categories first, and approve an item only when its labels are right: approved
items are what the reports trust.
"""
import os
import sys
from pathlib import Path

import streamlit as st

# Streamlit only puts this file's folder on the import path, so add the project's for the evaluation package
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from evaluation import review  # noqa: E402
from evaluation.golden import CATEGORIES, REVIEW_STATES  # noqa: E402

DATA = Path(os.getenv("REVIEW_DATA_DIR") or review.DATA)
CACHE = Path(os.getenv("REVIEW_CACHE_DIR") or review.CACHE)
FILES = {"Golden set": DATA / "golden_set.json", "Agent set": DATA / "agent_set.json",
         "Candidates": DATA / "candidates.json"}
NO_TOOL = "(none)"
TOOLS = [NO_TOOL, "get_leave_balance", "get_latest_payslip", "apply_for_leave", "raise_hr_ticket"]

st.set_page_config(page_title="Label review", page_icon="✅", layout="wide")


def flash(message: str) -> None:
    st.session_state["flash"] = message


def show_flash() -> None:
    if message := st.session_state.pop("flash", None):
        st.success(message)


def choose_item(items: list[dict], key: str) -> dict | None:
    """Pick an item, after filtering. The choice survives reruns, so saving doesn't jump elsewhere."""
    if not items:
        st.info("Nothing matches these filters.")
        return None
    ids = [item["id"] for item in items]
    if st.session_state.get(key) not in ids:
        st.session_state[key] = ids[0]
    chosen = st.selectbox("Item", ids, key=key)
    return next(item for item in items if item["id"] == chosen)


def save_and_report(path: Path, data: dict, message: str) -> None:
    review.save(path, data)
    flash(message)
    st.rerun()


def golden_page(path: Path) -> None:
    data = review.load(path)
    show_progress(data["items"])
    left, right = st.columns(2)
    status = left.selectbox("Show", ["unreviewed", "all", *sorted(REVIEW_STATES - {"unreviewed"})])
    categories = right.multiselect("Categories", sorted(CATEGORIES))
    items = [item for item in data["items"] if (status == "all" or item["review_status"] == status)
             and (not categories or item["category"] in categories)]
    item = choose_item(items, "golden_item")
    if item is None:
        return

    texts = review.chunk_texts()
    st.subheader(item["question"])
    st.caption(f"{item['id']} · {item['category']} · {item['review_status']}")
    for turn in item.get("history", []):
        st.markdown(f"**{turn['role'].title()} earlier:** {turn['content']}")
    st.markdown("**Chunks it should find**" if item["relevant_chunks"] else "**No chunks: it should be refused**"
                if item["must_refuse"] else "**No chunks labelled yet**")
    for chunk_id in item["relevant_chunks"]:
        with st.expander(chunk_id):
            st.text(texts.get(chunk_id, "This chunk no longer exists. Pick another below."))
    show_saved_answer(item["id"], texts)

    with st.form(f"labels-{item['id']}"):
        category = st.selectbox("Category", sorted(CATEGORIES), index=sorted(CATEGORIES).index(item["category"]))
        must_refuse = st.checkbox("The bot must refuse this", value=item["must_refuse"])
        facts = st.text_area("Facts the answer must contain, one per line", "\n".join(item["must_contain"]))
        chunks = st.multiselect("Chunks it should find", sorted(texts),
                                default=[chunk for chunk in item["relevant_chunks"] if chunk in texts])
        notes = st.text_area("Notes", item.get("notes", ""))
        approve, reject, keep = st.columns(3)
        decision = ("approved" if approve.form_submit_button("Approve", type="primary")
                    else "rejected" if reject.form_submit_button("Reject")
                    else "keep" if keep.form_submit_button("Save labels only") else None)
    if decision is None:
        return
    try:
        review.update_golden_labels(data, item["id"], category=category, must_refuse=must_refuse,
                                    must_contain=facts.splitlines(), relevant_chunks=chunks, chunk_texts=texts)
        review.set_review(data, item["id"], item["review_status"] if decision == "keep" else decision, notes)
    except review.ReviewError as error:
        st.error(str(error))
        return
    save_and_report(path, data, f"Saved {item['id']}" + ("" if decision == "keep" else f" as {decision}"))


def show_saved_answer(item_id: str, texts: dict[str, str]) -> None:
    runs = {name: rows for name, rows in review.saved_runs(CACHE).items() if item_id in rows}
    if not runs:
        st.caption("No saved answer for this item yet.")
        return
    name = st.selectbox("What the bot said in run", list(runs), index=len(runs) - 1)
    row = runs[name][item_id]
    if not row.get("answered", True):
        st.warning(f"It refused ({row.get('abstain_reason') or 'no reason saved'}).")
    st.markdown(row.get("answer", ""))
    if row.get("cited"):
        st.caption("Cited: " + " · ".join(row["cited"]))
    verdicts = row.get("verdicts") or ({"judge": row["verdict"]} if "verdict" in row else {})
    for verdict_judge, verdict in verdicts.items():
        st.caption(f"{verdict_judge}: faithful={verdict.get('faithful')}, correct={verdict.get('correct')}. "
                   f"{verdict.get('reason', '')}")


def agent_page(path: Path) -> None:
    data = review.load(path)
    show_progress(data["items"])
    status = st.selectbox("Show", ["unreviewed", "all", *sorted(REVIEW_STATES - {"unreviewed"})])
    items = [item for item in data["items"] if status == "all" or item["review_status"] == status]
    item = choose_item(items, "agent_item")
    if item is None:
        return
    st.subheader(item["message"])
    st.caption(f"{item['id']} · as {item.get('user_id') or 'nobody signed in'} · {item['review_status']}")
    if item.get("expected_args"):
        st.json(item["expected_args"])
    with st.form(f"agent-{item['id']}"):
        intents, routes = sorted(review.INTENTS), sorted(review.ROUTES)
        intent = st.selectbox("Expected intent", intents, index=intents.index(item["expected_intent"]))
        route = st.selectbox("Expected route", routes, index=routes.index(item["expected_route"]))
        tool = st.selectbox("Expected tool", TOOLS, index=TOOLS.index(item["expected_tool"] or NO_TOOL))
        notes = st.text_area("Notes", item.get("notes", ""))
        approve, reject, keep = st.columns(3)
        decision = ("approved" if approve.form_submit_button("Approve", type="primary")
                    else "rejected" if reject.form_submit_button("Reject")
                    else "keep" if keep.form_submit_button("Save labels only") else None)
    if decision is None:
        return
    try:
        review.update_agent_labels(data, item["id"], expected_intent=intent, expected_route=route,
                                   expected_tool=None if tool == NO_TOOL else tool)
        review.set_review(data, item["id"], item["review_status"] if decision == "keep" else decision, notes)
    except review.ReviewError as error:
        st.error(str(error))
        return
    save_and_report(path, data, f"Saved {item['id']}" + ("" if decision == "keep" else f" as {decision}"))


def candidates_page(path: Path, golden_path: Path) -> None:
    candidates = review.load(path)
    waiting = [item for item in candidates["items"] if item["status"] == "new"]
    st.metric("Waiting", len(waiting))
    st.caption("Add more with: python -m evaluation.candidates import <file downloaded from the admin screen>")
    item = choose_item(waiting, "candidate_item")
    if item is None:
        return
    st.subheader(item["question"])
    st.caption(f"{item['id']} · {item['source'].replace('_', ' ')} · asked {item['asked']} times · {item['last_seen']}")
    if item["answer"]:
        st.markdown(f"**The bot said:** {item['answer']}")
    if item["comment"]:
        st.markdown(f"**Their comment:** {item['comment']}")
    category = st.selectbox("Category in the golden set", sorted(CATEGORIES),
                            index=sorted(CATEGORIES).index("unanswerable" if item["source"] == "unanswered"
                                                           else "normal"))
    promote, dismiss = st.columns(2)
    if promote.button("Add to the golden set", type="primary"):
        golden = review.load(golden_path)
        added = review.promote_candidate(candidates, golden, item["id"], category)
        review.save(golden_path, golden)
        label_now = "" if added["must_refuse"] else " CI fails until an answerable item has its chunks labelled."
        save_and_report(path, candidates, f"Added as {added['id']}. Label it on the Golden set page.{label_now}")
    if dismiss.button("Dismiss"):
        review.dismiss_candidate(candidates, item["id"])
        save_and_report(path, candidates, f"Dismissed {item['id']}")


def show_progress(items: list[dict]) -> None:
    counts = review.progress(items)
    columns = st.columns(4)
    columns[0].metric("Approved", counts["approved"])
    columns[1].metric("Rejected", counts["rejected"])
    columns[2].metric("Unreviewed", counts["unreviewed"])
    columns[3].metric("Decided", f"{counts['decided_share']:.0%}")


st.title("Label review")
page = st.sidebar.radio("Labels", list(FILES))
st.sidebar.caption("Changes are saved to the files in evaluation/data straight away. Commit them when you're done.")
show_flash()
if page == "Golden set":
    golden_page(FILES[page])
elif page == "Agent set":
    agent_page(FILES[page])
else:
    candidates_page(FILES[page], FILES["Golden set"])
