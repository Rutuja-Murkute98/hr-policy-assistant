"""What the chat screen shows for citations, sources and errors. No Streamlit or API needed."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ui"))

from chat_format import error_html, link_citations, sources_html, usable_history  # noqa: E402

SOURCES = [
    {"ref": 1, "policy": "Leave Policy", "section": "4.1. Earned Leave", "text": "18 days of leave."},
    {"ref": 4, "policy": "Sick <b>Policy</b>", "section": "A & B", "text": "Line one.\n\nLine <script>two</script>."},
]


def test_citations_become_links_to_their_source():
    assert link_citations("18 days [1].", SOURCES, 3) == "18 days [[1](#src-3-1)]."


def test_grouped_citations_each_get_a_link():
    assert link_citations("Yes [1, 4].", SOURCES, 2) == "Yes [[1](#src-2-1), [4](#src-2-4)]."


def test_a_number_with_no_source_stays_as_text():
    assert link_citations("Odd [9] and [1, 9].", SOURCES, 0) == "Odd [9] and [[1](#src-0-1), 9]."


def test_other_brackets_are_left_alone():
    assert link_citations("See [the policy] on 2026.", SOURCES, 0) == "See [the policy] on 2026."


def test_sources_list_has_an_anchor_and_the_policy_text_for_each():
    built = sources_html(SOURCES, 3)
    assert "Sources (2)" in built
    assert 'id="src-3-1"' in built and 'id="src-3-4"' in built
    assert "18 days of leave." in built


def test_sources_are_escaped():
    built = sources_html(SOURCES, 0)
    assert "<script>" not in built and "&lt;script&gt;" in built
    assert "Sick &lt;b&gt;Policy&lt;/b&gt;" in built and "A &amp; B" in built


def test_sources_stay_on_one_line():
    assert "\n" not in sources_html(SOURCES, 0)
    assert "Line one.<br>Line" in sources_html(SOURCES, 0)


def test_a_source_without_text_says_so():
    built = sources_html([{"ref": 1, "policy": "P", "section": "S"}], 0)
    assert "No preview available." in built and "<blockquote>" not in built


def test_error_is_an_alert_and_escaped():
    built = error_html("Bad <thing>")
    assert 'role="alert"' in built and "&lt;thing&gt;" in built


def test_history_drops_failed_replies_and_their_questions():
    good_q, good_a = {"role": "user", "content": "Q1"}, {"role": "assistant", "content": "A1"}
    bad_q, bad_a = {"role": "user", "content": "Q2"}, {"role": "assistant", "content": "Oops", "error": True}
    assert usable_history([good_q, good_a, bad_q, bad_a]) == [good_q, good_a]


def test_history_keeps_everything_when_nothing_failed():
    messages = [{"role": "user", "content": "Q"}, {"role": "assistant", "content": "A"}]
    assert usable_history(messages) == messages
