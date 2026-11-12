"""Several judges, how much they agree, and the deterministic citation score. No model is called."""
import json

import pytest

from evaluation import run_eval
from evaluation.metrics import cited_statement_share, cohen_kappa, judge_agreement, statements
from evaluation.run_eval import (
    answer_metrics,
    check_judges,
    parse_judges,
    run_judge,
    select_items,
    verdicts_of,
    write_answer_report,
)

YES = {"faithful": True, "correct": True, "relevant": True, "reason": ""}
NO = {"faithful": False, "correct": False, "relevant": True, "reason": "Missing a condition."}


def test_citation_share_counts_statements_that_point_at_an_excerpt():
    assert cited_statement_share("Confirmed employees get 18 days of earned leave [1]. "
                                 "Unused days lapse at the end of the year. [2]") == 1.0
    assert cited_statement_share("Confirmed employees get 18 days of earned leave [1]. "
                                 "You can also work from home on any Friday.") == 0.5
    assert cited_statement_share("- Earned leave is 18 days a year [1]\n- Sick leave is twelve days a year") == 0.5
    assert cited_statement_share("Here's what the policy says:") is None  # a lead-in, not a claim
    assert cited_statement_share("") is None
    # Amounts and abbreviations don't end a statement
    assert cited_statement_share("The room rent limit is Rs. 5,000 per day [1].") == 1.0
    assert cited_statement_share("Some benefits, e.g. gym access, need approval from HR first [2].") == 1.0


def test_a_citation_on_its_own_belongs_to_the_statement_before_it():
    found = statements("Confirmed employees get 18 days of earned leave. [1]\nAsk HR for anything else.")
    assert found == [("Confirmed employees get 18 days of earned leave.", True), ("Ask HR for anything else.", False)]


def test_kappa_rewards_agreement_beyond_chance():
    assert cohen_kappa([True, False, True, False], [True, False, True, False]) == pytest.approx(1)
    assert cohen_kappa([True, False], [False, True]) == pytest.approx(-1)
    assert cohen_kappa([True, True], [True, True]) is None  # chance explains it all
    assert cohen_kappa([], []) is None


def test_agreement_is_measured_on_items_both_judges_graded():
    by_item = [{"a": YES, "b": YES}, {"a": YES, "b": NO}, {"a": NO, "b": NO}, {"a": YES}]
    result = judge_agreement(by_item)["a vs b"]
    assert result["both_judged"] == 3
    assert result["correct_agree"] == pytest.approx(0.667)
    assert result["correct_kappa"] == pytest.approx(0.4)


def test_judges_are_read_as_provider_and_model():
    assert parse_judges(["anthropic:claude-sonnet-5-5", "gemini-3.1-flash-lite", " "], "google") == [
        ("anthropic", "claude-sonnet-5-5"), ("google", "gemini-3.1-flash-lite")]
    assert parse_judges(["ollama:llama3.1:8b", "llama3.1:8b"], "ollama") == [
        ("ollama", "llama3.1:8b"), ("ollama", "llama3.1:8b")]
    assert parse_judges([], "google") == [("google", run_eval.DEFAULT_JUDGE_MODEL)]


def test_the_answering_model_cant_judge_itself_and_same_vendor_is_flagged():
    with pytest.raises(SystemExit):
        check_judges([("google", "gemini-x")], "google", "gemini-x")
    warnings = check_judges([("anthropic", "claude-x"), ("google", "gemini-y")], "google", "gemini-x")
    assert len(warnings) == 1 and "google:gemini-y" in warnings[0]


class FakeJudge:
    def __init__(self, verdict):
        self.verdict, self.calls = verdict, 0

    def invoke(self, messages):
        self.calls += 1
        return type("Verdict", (), {"model_dump": lambda _: dict(self.verdict)})()


def saved_rows():
    return {
        "normal-001": {"answer": "Confirmed employees get 18 days a year [1].", "answered": True, "excerpts": ["18 days"], "question_used": "q",
                       "cited": ["c1"], "retrieved": ["c1"], "seconds": 3.0,
                       "verdict": YES, "judge_model": "gemini-old"},  # judged by an older single-judge run
        "normal-002": {"answer": "The notice period is 60 days for staff [1].", "answered": True, "excerpts": ["60 days"],
                       "question_used": "q2", "cited": ["c2"], "retrieved": ["c2"], "seconds": 5.0},
        "unanswerable-001": {"answer": "Please ask HR.", "answered": False, "excerpts": [], "question_used": "q3",
                             "cited": [], "retrieved": [], "seconds": 1.0},
    }


ITEMS = [{"id": "normal-001", "category": "normal", "must_refuse": False, "must_contain": ["18"],
          "relevant_chunks": ["c1"], "review_status": "approved"},
         {"id": "normal-002", "category": "normal", "must_refuse": False, "must_contain": ["60"],
          "relevant_chunks": ["c2"], "review_status": "unreviewed"},
         {"id": "unanswerable-001", "category": "unanswerable", "must_refuse": True, "must_contain": [],
          "relevant_chunks": [], "review_status": "rejected"}]


def test_each_judge_grades_only_what_it_hasnt_and_older_verdicts_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(run_eval.config, "LLM_PROVIDER", "google")
    rows, progress = saved_rows(), tmp_path / "progress.json"
    claude = FakeJudge(NO)
    assert run_judge(claude, "anthropic:claude-x", ITEMS, rows, progress, pause_seconds=0) == 2
    assert run_judge(claude, "anthropic:claude-x", ITEMS, rows, progress, pause_seconds=0) == 0  # resumes, no repeat
    assert claude.calls == 2
    assert set(verdicts_of(rows["normal-001"])) == {"google:gemini-old", "anthropic:claude-x"}
    assert verdicts_of(rows["unanswerable-001"]) == {}  # a refusal has nothing to judge
    assert json.loads(progress.read_text(encoding="utf-8"))["normal-002"]["verdicts"]["anthropic:claude-x"] == NO


def test_metrics_report_every_judge_and_their_agreement():
    rows = saved_rows()
    rows["normal-001"]["verdicts"] = {"anthropic:claude-x": YES, "google:gemini-y": YES}
    rows["normal-002"]["verdicts"] = {"anthropic:claude-x": NO, "google:gemini-y": YES}
    metrics = answer_metrics(ITEMS, rows)
    assert metrics["judge_correct"] == 0.5  # the main judge is the first one asked
    assert metrics["judges"]["google:gemini-y"]["correct"] == 1.0
    assert metrics["judge_agreement"]["anthropic:claude-x vs google:gemini-y"]["correct_agree"] == 0.5
    assert metrics["statements_cited"] == 1.0


def test_an_older_single_judge_run_still_reports_as_before(monkeypatch):
    monkeypatch.setattr(run_eval.config, "LLM_PROVIDER", "google")
    metrics = answer_metrics(ITEMS, saved_rows())
    assert (metrics["judge_correct"], metrics["judged"]) == (1.0, 1)
    assert "judge_agreement" not in metrics


def test_the_report_has_a_separate_block_for_approved_items(tmp_path, monkeypatch):
    monkeypatch.setattr(run_eval, "REPORTS", tmp_path)
    write_answer_report("unit", ITEMS, saved_rows())
    report = json.loads((tmp_path / "answers_unit.json").read_text(encoding="utf-8"))
    assert report["approved_only"]["items"] == 1
    assert report["approved_only"]["facts_correct"] == 1.0
    write_answer_report("none_approved", ITEMS[1:2], saved_rows())
    assert json.loads((tmp_path / "answers_none_approved.json").read_text(encoding="utf-8"))["approved_only"] is None


def test_a_run_can_use_one_fixed_half_and_a_limit():
    items = [{"id": f"normal-{n:03d}"} for n in range(1, 11)]
    assert [i["id"] for i in select_items(items, "check")] == [f"normal-{n:03d}" for n in (1, 3, 5, 7, 9)]
    assert len(select_items(items, "tune", 2)) == 2
    assert select_items(items) == items


def test_only_the_attempt_that_worked_is_timed():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("503 busy")
        return "answer"

    result, seconds = run_eval.call_with_retry(lambda: run_eval.timed(flaky), wait=0.2)
    assert result == "answer" and len(calls) == 2
    assert seconds < 0.2  # the wait between attempts isn't counted


def test_step_times_are_summarised_per_step():
    rows = [{"step_ms": {"understand": 1000, "generate": 3000}}, {"step_ms": {"understand": 2000}},
            {"answer": "from a run before steps were timed"}]
    assert run_eval.step_percentiles(rows) == {
        "generate": {"p50": 3000, "p95": 3000, "count": 1},
        "understand": {"p50": 1000, "p95": 2000, "count": 2}}


def test_an_answer_where_a_model_call_fell_back_is_retried_not_measured():
    from types import SimpleNamespace

    from evaluation.run_eval import call_with_retry, complete

    answers = iter([SimpleNamespace(degraded=["understand"]), SimpleNamespace(degraded=[])])
    result = call_with_retry(lambda: complete(next(answers)), wait=0)
    assert result.degraded == []
