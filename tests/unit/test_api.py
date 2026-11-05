"""API behaviour, using a fake service so no database or Gemini call is needed."""
import pytest
from fastapi.testclient import TestClient

from app import config, security
from app.main import app
from app.rag.service import RagAnswer

KEY = {"X-API-Key": "test-key"}


class FakeService:
    def __init__(self, error=None):
        self.error = error

    def ask(self, question):
        if self.error:
            raise self.error
        return RagAnswer("18 days.", [{"policy": "Leave Policy", "section": "4.1", "file": "Leave_Policy.md"}])


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "test-key")
    security.rate_limit.reset()
    app.state.service = FakeService()
    app.state.count_chunks = lambda: 539
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_health_needs_no_key_and_counts_chunks(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "chunks_indexed": 539}


def test_health_reports_empty_index(client):
    app.state.count_chunks = lambda: 0
    assert client.get("/health").status_code == 503


def test_ask_returns_answer_sources_and_request_id(client):
    response = client.post("/ask", json={"question": "How much leave?"}, headers=KEY)
    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "18 days."
    assert body["sources"][0]["policy"] == "Leave Policy"
    assert response.headers["X-Request-ID"] == body["request_id"]


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}])
def test_missing_or_wrong_key_is_rejected(client, headers):
    assert client.post("/ask", json={"question": "How much leave?"}, headers=headers).status_code == 401


def test_server_without_a_key_refuses_every_request(client, monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "")
    assert client.post("/ask", json={"question": "How much leave?"}, headers=KEY).status_code == 503


@pytest.mark.parametrize("question", ["", "a", "   ", "x" * (config.MAX_QUESTION_CHARS + 1)])
def test_bad_questions_are_rejected(client, question):
    assert client.post("/ask", json={"question": question}, headers=KEY).status_code == 422


def test_rate_limit_blocks_after_the_limit(client):
    for _ in range(config.RATE_LIMIT_PER_MINUTE):
        assert client.post("/ask", json={"question": "How much leave?"}, headers=KEY).status_code == 200
    response = client.post("/ask", json={"question": "How much leave?"}, headers=KEY)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"


def test_each_chat_session_has_its_own_limit(client):
    first = {**KEY, "X-Client-Id": "session-1"}
    second = {**KEY, "X-Client-Id": "session-2"}
    for _ in range(config.RATE_LIMIT_PER_MINUTE):
        assert client.post("/ask", json={"question": "How much leave?"}, headers=first).status_code == 200
    assert client.post("/ask", json={"question": "How much leave?"}, headers=first).status_code == 429
    assert client.post("/ask", json={"question": "How much leave?"}, headers=second).status_code == 200


def test_total_limit_applies_across_all_sessions(client):
    for number in range(config.RATE_LIMIT_TOTAL_PER_MINUTE):
        headers = {**KEY, "X-Client-Id": f"session-{number}"}
        assert client.post("/ask", json={"question": "How much leave?"}, headers=headers).status_code == 200
    headers = {**KEY, "X-Client-Id": "one-more"}
    response = client.post("/ask", json={"question": "How much leave?"}, headers=headers)
    assert response.status_code == 429
    assert "busy" in response.text


def test_rejected_key_does_not_use_up_the_limit(client):
    bad = {"X-API-Key": "wrong", "X-Client-Id": "session-1"}
    for _ in range(config.RATE_LIMIT_PER_MINUTE + 1):
        assert client.post("/ask", json={"question": "How much leave?"}, headers=bad).status_code == 401
    good = {**KEY, "X-Client-Id": "session-1"}
    assert client.post("/ask", json={"question": "How much leave?"}, headers=good).status_code == 200


def test_live_needs_no_database(client):
    def database_must_not_be_used():
        raise AssertionError("/live used the database")

    app.state.count_chunks = database_must_not_be_used
    assert client.get("/live").json() == {"status": "ok"}


def test_gemini_failure_gives_a_clean_error(client):
    app.state.service = FakeService(RuntimeError("secret internal detail"))
    response = client.post("/ask", json={"question": "How much leave?"}, headers=KEY)
    assert response.status_code == 503
    assert "secret" not in response.text
