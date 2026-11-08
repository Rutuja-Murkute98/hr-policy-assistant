"""The deployment check, against a pretend API. No server or model is used."""
import json

import httpx
import pytest

from ops import check_deployment
from ops.check_deployment import percentile, run

KEY = "first-key"
SECOND_KEY = "second-key"
GOLDEN = {"items": [
    {"id": "normal-001", "category": "normal", "question": "How much earned leave?", "history": [],
     "must_contain": ["18"]},
    {"id": "normal-002", "category": "normal", "question": "Notice period?", "history": [], "must_contain": ["60"]},
    {"id": "follow-001", "category": "normal", "question": "And on probation?",
     "history": [{"role": "user", "content": "x"}], "must_contain": []},
    {"id": "unanswerable-001", "category": "unanswerable", "question": "Weather on Mars?", "history": []},
]}
SOURCE = {"ref": 1, "policy": "Leave Policy", "section": "4.1", "file": "Leave_Policy.md", "chunk_id": "c1"}


@pytest.fixture
def golden(tmp_path):
    path = tmp_path / "golden.json"
    path.write_text(json.dumps(GOLDEN), encoding="utf-8")
    return path


def fake_api(*, chunks=539, health_status=200, key_required=True, answers=None, refuse_unknown_key=True):
    """A pretend API. answers maps a question to (status, body)."""
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/live":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/health":
            if health_status != 200:
                return httpx.Response(health_status, json={"detail": "No policies indexed. Run: ..."})
            return httpx.Response(200, json={"status": "ok", "chunks_indexed": chunks, "ai": "ok"})
        if request.url.path == "/ask":
            key = request.headers.get("X-API-Key", "")
            if key_required and not key:
                return httpx.Response(401, json={"detail": "Invalid or missing API key"})
            if refuse_unknown_key and key not in (KEY, SECOND_KEY):
                return httpx.Response(401, json={"detail": "Invalid or missing API key"})
            question = json.loads(request.content)["question"]
            asked.append((key, question))
            if question == "hello":
                return httpx.Response(200, json={"answer": "Hello!", "sources": [], "intent": "greeting"})
            status, body = (answers or {}).get((key, question)) or (answers or {}).get(question) or (
                200, {"answer": "I don't know.", "sources": []})
            return httpx.Response(status, json=body)
        return httpx.Response(404)

    return httpx.Client(base_url="http://api.test", transport=httpx.MockTransport(handler)), asked


def names(report):
    return {check.name: check.passed for check in report.checks}


def test_without_the_model_only_free_checks_run_and_nothing_is_asked_of_the_model(golden):
    client, asked = fake_api()
    report = run(client, KEY, use_model=False, golden_set=golden)
    assert report.passed
    assert list(names(report)) == ["API is up", "Database and index", "Refuses callers without a key",
                                   "Greets without using the model"]
    assert asked == [(KEY, "hello")]


def test_an_empty_index_fails_unless_allowed_as_in_ci(golden):
    client, _ = fake_api(health_status=503)
    assert not names(run(client, KEY, use_model=False, golden_set=golden))["Database and index"]
    assert names(run(client, KEY, use_model=False, allow_empty_index=True, golden_set=golden))["Database and index"]


def test_an_api_that_answers_without_a_key_fails(golden):
    client, _ = fake_api(key_required=False, refuse_unknown_key=False)
    assert not names(run(client, KEY, use_model=False, golden_set=golden))["Refuses callers without a key"]


def test_policy_answers_need_citations_and_only_cited_answers_are_timed(golden):
    answers = {"How much earned leave?": (200, {"answer": "18 days [1].", "sources": [SOURCE]}),
               "Notice period?": (200, {"answer": "I don't know.", "sources": []})}
    client, asked = fake_api(answers=answers)
    report = run(client, KEY, questions=5, refusals=1, pause_seconds=0, golden_set=golden)
    result = names(report)
    assert result["Answers normal-001 with citations"]
    assert not result["Answers normal-002 with citations"]
    assert result["Refuses unanswerable-001"]
    assert len(report.answer_seconds) == 1
    assert "expected facts found 1 of 1" in next(c.detail for c in report.checks if c.name.endswith("normal-001 "
                                                                                                    "with citations"))
    # Questions with chat history aren't asked on their own
    assert (KEY, "And on probation?") not in asked


def test_a_refusal_that_cites_sources_fails(golden):
    client, _ = fake_api(answers={"Weather on Mars?": (200, {"answer": "Sunny [1].", "sources": [SOURCE]})})
    report = run(client, KEY, questions=0, refusals=1, pause_seconds=0, golden_set=golden)
    assert not names(report)["Refuses unanswerable-001"]


def test_a_failed_answer_is_reported_not_raised(golden):
    client, _ = fake_api(answers={"How much earned leave?": (503, {"detail": "busy"})})
    report = run(client, KEY, questions=1, refusals=0, pause_seconds=0, golden_set=golden)
    check = report.checks[-1]
    assert not check.passed and "503" in check.detail


def test_isolation_passes_only_when_the_document_is_cited_for_its_own_organisation(golden):
    other_source = {**SOURCE, "file": "Other_Only.md"}
    question = "What is the canteen allowance?"
    good = {(SECOND_KEY, question): (200, {"answer": "500 [1].", "sources": [other_source]}),
            (KEY, question): (200, {"answer": "I don't know.", "sources": []})}
    client, _ = fake_api(answers=good)
    report = run(client, KEY, questions=0, refusals=0, pause_seconds=0, second_key=SECOND_KEY,
                 isolation_question=question, isolation_file="Other_Only.md", golden_set=golden)
    assert report.passed

    leaky = {**good, (KEY, question): (200, {"answer": "500 [1].", "sources": [other_source]})}
    client, _ = fake_api(answers=leaky)
    report = run(client, KEY, questions=0, refusals=0, pause_seconds=0, second_key=SECOND_KEY,
                 isolation_question=question, isolation_file="Other_Only.md", golden_set=golden)
    assert not names(report)["Isolation: Other_Only.md is not cited for the first organisation"]


def test_a_down_api_stops_after_the_first_check(golden):
    def handler(request):
        raise httpx.ConnectError("refused")

    client = httpx.Client(base_url="http://api.test", transport=httpx.MockTransport(handler))
    report = run(client, KEY, golden_set=golden)
    assert [check.name for check in report.checks] == ["API is up"]
    assert not report.passed


def test_percentiles_use_measured_values():
    assert percentile([], 95) is None
    assert percentile([3.0], 95) == 3.0
    assert percentile([1.0, 2.0, 3.0, 4.0, 10.0], 50) == 3.0
    assert percentile([1.0, 2.0, 3.0, 4.0, 10.0], 95) == 10.0


def test_the_key_comes_from_the_environment_not_the_command_line(monkeypatch):
    monkeypatch.delenv("CHECK_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        check_deployment.main(["--api-url", "http://api.test"])


def test_the_isolation_options_need_each_other_and_a_second_key(monkeypatch):
    monkeypatch.setenv("CHECK_API_KEY", KEY)
    monkeypatch.delenv("CHECK_SECOND_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        check_deployment.main(["--api-url", "http://api.test", "--isolation-question", "q"])
    with pytest.raises(SystemExit):
        check_deployment.main(["--api-url", "http://api.test", "--isolation-question", "q", "--isolation-file", "f"])
