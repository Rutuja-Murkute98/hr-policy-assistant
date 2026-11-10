"""Reviewing the evaluation labels, and turning real questions into test items. No model or database."""
import json
import shutil
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from evaluation import candidates as candidates_cli
from evaluation import review
from evaluation.golden import GOLDEN_FILE, load_golden

APP_FILE = str(Path(__file__).resolve().parents[2] / "evaluation" / "review_app.py")
CHUNK = "Leave_Policy > 4. Types of Leave and Entitlements > 4.1. Earned Leave (EL) / Annual Leave > 0"


def golden(*items) -> dict:
    return {"status": "draft", "items": [dict(item) for item in items]}


def item(item_id="normal-001", category="normal", question="How much earned leave?", **fields) -> dict:
    return {"id": item_id, "category": category, "question": question, "history": [], "relevant_chunks": [CHUNK],
            "must_contain": ["18"], "must_refuse": False, "review_status": "unreviewed", "notes": "", **fields}


def test_progress_counts_only_approved_and_rejected_as_decided():
    items = [item(review_status="approved"), item(review_status="rejected"), item(), item()]
    assert review.progress(items) == {"approved": 1, "rejected": 1, "unreviewed": 2, "total": 4,
                                      "decided_share": 0.5}
    assert review.progress([])["decided_share"] == 0.0


def test_a_review_needs_a_known_status_and_keeps_notes_tidy():
    data = golden(item())
    review.set_review(data, "normal-001", "approved", "  Checked against the policy.  ")
    assert data["items"][0]["review_status"] == "approved"
    assert data["items"][0]["notes"] == "Checked against the policy."
    with pytest.raises(review.ReviewError):
        review.set_review(data, "normal-001", "maybe")
    with pytest.raises(review.ReviewError):
        review.set_review(data, "normal-999", "approved")


TEXTS = {CHUNK: "Leave Policy > 4.1\n\nConfirmed employees get 18 days of earned leave a year.",
         "Leave_Policy > 5. Sick Leave > 0": "Leave Policy > 5\n\nTwelve days of sick leave."}


def relabel(data, item_id="normal-001", category="normal", must_refuse=False, must_contain=(), chunks=(CHUNK,)):
    return review.update_golden_labels(data, item_id, category=category, must_refuse=must_refuse,
                                       must_contain=list(must_contain), relevant_chunks=list(chunks), chunk_texts=TEXTS)


def test_labels_are_tidied_when_saved():
    data = golden(item())
    relabel(data, category="exception", must_contain=["18", " ", " DAYS "], chunks=[CHUNK, CHUNK])
    assert data["items"][0]["must_contain"] == ["18", "DAYS"]
    assert data["items"][0]["relevant_chunks"] == [CHUNK]
    assert data["items"][0]["category"] == "exception"


@pytest.mark.parametrize("change,problem", [
    ({"chunks": ["Leave_Policy > typo > 0"]}, "don't exist"),
    ({"category": "unanswerable", "must_refuse": True}, "refuse"),
    ({"category": "unanswerable", "must_refuse": True, "chunks": [], "must_contain": ["18"]}, "refuse"),
    ({"chunks": []}, "needs at least one chunk"),
    ({"must_contain": ["24 days"]}, "aren't in the chosen chunks"),
    ({"category": "follow_up"}, "follow-up"),
    ({"category": "other"}, "category"),
])
def test_labels_that_the_tests_would_reject_are_refused(change, problem):
    data = golden(item())
    with pytest.raises(review.ReviewError, match=problem):
        relabel(data, **change)
    assert data["items"][0] == item()  # nothing changed


def test_a_refusal_can_be_saved_with_no_chunks_or_facts():
    data = golden(item("unanswerable-001", "unanswerable"))
    relabel(data, "unanswerable-001", "unanswerable", must_refuse=True, chunks=[])
    assert data["items"][0]["relevant_chunks"] == [] and data["items"][0]["must_refuse"] is True


def test_agent_labels_are_checked():
    data = {"items": [{"id": "policy-001", "expected_intent": "policy_question", "expected_route": "rag",
                       "expected_tool": None}]}
    review.update_agent_labels(data, "policy-001", expected_intent="action_request", expected_route="self_service",
                               expected_tool="raise_hr_ticket")
    assert data["items"][0]["expected_tool"] == "raise_hr_ticket"
    review.update_agent_labels(data, "policy-001", expected_intent="greeting", expected_route="greeting",
                               expected_tool="")
    assert data["items"][0]["expected_tool"] is None
    with pytest.raises(review.ReviewError):
        review.update_agent_labels(data, "policy-001", expected_intent="chat", expected_route="rag", expected_tool=None)


def test_candidates_skip_questions_already_known_and_unknown_sources():
    data = golden(item(question="How much earned leave?"))
    waiting = {"items": []}
    added = review.add_candidates(waiting, data, [
        {"question": "how much earned leave", "source": "unanswered"},        # already in the golden set
        {"question": "Is there a gym?", "source": "unanswered", "asked": 3},
        {"question": "is there a gym", "source": "thumbs_down"},               # a repeat
        {"question": "Can I bring my dog?", "source": "somewhere"},
        {"question": "  ", "source": "unanswered"},
        {"question": "Can I carry over leave?", "source": "thumbs_down", "answer": "No.", "comment": "Wrong"},
    ])
    assert added == 2
    assert [(c["id"], c["question"], c["asked"]) for c in waiting["items"]] == [
        ("candidate-001", "Is there a gym?", 3), ("candidate-002", "Can I carry over leave?", 1)]
    assert review.add_candidates(waiting, data, [{"question": "Is there a gym?", "source": "unanswered"}]) == 0


def test_a_promoted_candidate_joins_the_golden_set_unreviewed_and_only_once():
    data = golden(item(), item("unanswerable-015", "unanswerable"))
    waiting = {"items": []}
    review.add_candidates(waiting, data, [{"question": "Is there a gym?", "source": "unanswered"}])
    added = review.promote_candidate(waiting, data, "candidate-001", "unanswerable")
    assert added["id"] == "unanswerable-016"
    assert added["must_refuse"] is True and added["review_status"] == "unreviewed"
    assert added["relevant_chunks"] == [] and "candidate-001" in added["notes"]
    assert waiting["items"][0]["status"] == "promoted"
    with pytest.raises(review.ReviewError, match="already"):
        review.promote_candidate(waiting, data, "candidate-001", "normal")


def test_saving_keeps_the_files_existing_format():
    """The label files are written exactly as before, so a review only changes the lines it touches."""
    original = GOLDEN_FILE.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert json.dumps(review.load(GOLDEN_FILE), indent=1, ensure_ascii=False) + "\n" == original


def test_every_labelled_chunk_in_the_golden_set_exists():
    """The review screen shows each labelled chunk's text, so a label that points nowhere would show up as a gap."""
    texts = review.chunk_texts()
    missing = {chunk for entry in load_golden() for chunk in entry["relevant_chunks"] if chunk not in texts}
    assert not missing


def test_saved_runs_skip_unreadable_files(tmp_path):
    (tmp_path / "answers_good.json").write_text(json.dumps({"normal-001": {"answer": "18 days"}}), encoding="utf-8")
    (tmp_path / "answers_broken.json").write_text("{not json", encoding="utf-8")
    assert review.saved_runs(tmp_path) == {"good": {"normal-001": {"answer": "18 days"}}}


def test_the_import_command_adds_new_candidates(tmp_path):
    source = tmp_path / "download.json"
    source.write_text(json.dumps([{"question": "Is there a gym?", "source": "unanswered"}]), encoding="utf-8")
    golden_file = tmp_path / "golden_set.json"
    golden_file.write_text(json.dumps(golden(item())), encoding="utf-8")
    waiting_file = tmp_path / "candidates.json"
    assert candidates_cli.import_file(source, waiting_file, golden_file) == 1
    assert candidates_cli.import_file(source, waiting_file, golden_file) == 0
    assert json.loads(waiting_file.read_text(encoding="utf-8"))["items"][0]["question"] == "Is there a gym?"
    source.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    with pytest.raises(review.ReviewError):
        candidates_cli.import_file(source, waiting_file, golden_file)


# The review screen, on copies of the label files

@pytest.fixture
def screen(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    for name in ("golden_set.json", "agent_set.json"):
        shutil.copy(review.DATA / name, data / name)
    monkeypatch.setenv("REVIEW_DATA_DIR", str(data))
    monkeypatch.setenv("REVIEW_CACHE_DIR", str(tmp_path / "cache"))
    app = AppTest.from_file(APP_FILE, default_timeout=60)
    app.run()
    assert not app.exception
    return app, data


def test_approving_on_the_screen_saves_to_the_file(screen):
    app, data = screen
    first = app.selectbox(key="golden_item").value
    next(button for button in app.button if button.label == "Approve").click().run()
    assert not app.exception
    saved = review.find(review.load(data / "golden_set.json"), first)
    assert saved["review_status"] == "approved"
    assert any(f"Saved {first} as approved" in message.value for message in app.success)


def test_the_agent_set_can_be_rejected_with_a_note(screen):
    app, data = screen
    app.sidebar.radio[0].set_value("Agent set").run()
    first = app.selectbox(key="agent_item").value
    app.text_area[0].input("Wrong route.")
    next(button for button in app.button if button.label == "Reject").click().run()
    assert not app.exception
    saved = review.find(review.load(data / "agent_set.json"), first)
    assert (saved["review_status"], saved["notes"]) == ("rejected", "Wrong route.")


def test_a_candidate_is_promoted_from_the_screen(screen):
    app, data = screen
    golden_data = review.load(data / "golden_set.json")
    waiting = review.load(data / "candidates.json")
    review.add_candidates(waiting, golden_data, [{"question": "Is there an on-site gym?", "source": "unanswered"}])
    review.save(data / "candidates.json", waiting)
    app.sidebar.radio[0].set_value("Candidates").run()
    next(button for button in app.button if button.label == "Add to the golden set").click().run()
    assert not app.exception
    promoted = [entry for entry in review.load(data / "golden_set.json")["items"]
                if entry["question"] == "Is there an on-site gym?"]
    assert promoted and promoted[0]["category"] == "unanswerable"


# Drafting questions for sections with no test

def test_uncovered_sections_skip_labelled_boilerplate_and_short_ones():
    texts = {CHUNK: "x\n\n" + "Earned leave is 18 days. " * 10,
             "Leave_Policy > 5. Sick Leave > 0": "x\n\n" + "Twelve days of sick leave a year. " * 10,
             "Leave_Policy > 13. Policy Review > 0": "x\n\n" + "Reviewed annually by HR. " * 10,
             "Leave_Policy > 6. Short > 0": "x\n\nToo short.",
             "Attendance_Policy > 3. Hours > 0": "x\n\n" + "Core hours are 10 to 4. " * 10}
    data = golden(item())
    assert review.uncovered_chunks(data, texts) == ["Leave_Policy > 5. Sick Leave > 0",
                                                    "Attendance_Policy > 3. Hours > 0"]
    assert review.uncovered_chunks(data, texts, "Attendance_Policy") == ["Attendance_Policy > 3. Hours > 0"]


def test_a_draft_keeps_only_facts_found_in_its_section():
    text = "Leave Policy > 5\n\nEmployees get twelve days of sick leave a year."
    draft = review.checked_draft("c1", text, " How much sick leave do I get? ", ["twelve days", "15 days", " "])
    assert draft == {"question": "How much sick leave do I get?", "source": "new_section", "relevant_chunks": ["c1"],
                     "must_contain": ["twelve days"]}
    assert review.checked_draft("c1", text, "  ", ["twelve days"]) is None


class FakeWriter:
    def __init__(self):
        self.calls = 0

    def with_structured_output(self, schema):
        return self

    def invoke(self, messages):
        self.calls += 1
        excerpt = messages[1][1]
        return candidates_cli.DraftQuestion(question=f"What does section {self.calls} say?",
                                            facts=[excerpt.split("\n\n", 1)[1][:12], "not in the text"])


def test_drafting_adds_labelled_candidates_once_and_promotes_them_with_their_labels(tmp_path):
    golden_file, waiting_file = tmp_path / "golden_set.json", tmp_path / "candidates.json"
    golden_file.write_text(json.dumps(golden(item())), encoding="utf-8")
    writer = FakeWriter()
    added = candidates_cli.draft(writer, "Disciplinary_Policy", 2, waiting_file, golden_file, pause_seconds=0,
                                 retry=lambda call: call())
    assert added == 2 and writer.calls == 2
    waiting = review.load(waiting_file)
    first = waiting["items"][0]
    assert first["source"] == "new_section" and first["relevant_chunks"][0].startswith("Disciplinary_Policy > ")
    assert first["must_contain"] and "not in the text" not in first["must_contain"]
    # A second run drafts the next sections, not the same ones again
    candidates_cli.draft(writer, "Disciplinary_Policy", 1, waiting_file, golden_file, pause_seconds=0,
                         retry=lambda call: call())
    chunks = [entry["relevant_chunks"][0] for entry in review.load(waiting_file)["items"]]
    assert len(chunks) == len(set(chunks)) == 3

    data, waiting = review.load(golden_file), review.load(waiting_file)
    promoted = review.promote_candidate(waiting, data, first["id"], "normal")
    assert promoted["relevant_chunks"] == first["relevant_chunks"]
    assert promoted["must_contain"] == first["must_contain"]
    assert "Drafted from a policy section" in promoted["notes"]
