"""Structured logs, usage counting and optional tracing, with no database or model."""
import json
import logging
import sys
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.callbacks import CallbackManager
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from app import config, observability, security
from app.main import app
from app.observability import JsonFormatter, RequestIdFilter, Tracing, UsageCallback, estimated_cost, request_id_var
from app.pii import MaskingFilter

KEY = {"X-API-Key": "test-key", "X-User-Id": "E1001"}
SECRET_QUESTION = "Is my salary of ninety lakhs safe, zebrafinch?"


def format_record(message="hello %s", args=("there",), exc_info=None, **extra) -> dict:
    record = logging.LogRecord("hr_helpdesk", logging.INFO, "f", 1, message, args, exc_info)
    record.__dict__.update(extra)
    MaskingFilter().filter(record)
    RequestIdFilter().filter(record)
    return json.loads(JsonFormatter().format(record))


def test_a_log_line_is_one_json_object():
    entry = format_record()
    assert entry["message"] == "hello there"
    assert entry["level"] == "INFO"
    assert entry["logger"] == "hr_helpdesk"
    assert "T" in entry["time"]


def test_a_log_line_carries_the_request_id():
    token = request_id_var.set("r123")
    try:
        assert format_record()["request_id"] == "r123"
    finally:
        request_id_var.reset(token)
    assert "request_id" not in format_record()


def test_only_known_fields_reach_the_log():
    entry = format_record(event="request", status=200, question="my secret")
    assert entry["event"] == "request"
    assert entry["status"] == 200
    assert "question" not in entry


def test_an_exception_is_logged_without_its_message():
    try:
        raise ValueError(f"The model was sent: {SECRET_QUESTION}")
    except ValueError:
        entry = format_record(exc_info=sys.exc_info())
    assert entry["error_type"] == "ValueError"
    assert any("test_observability" in frame for frame in entry["error_stack"])
    assert "zebrafinch" not in json.dumps(entry)


def test_an_exception_is_safe_even_without_the_masking_filter():
    try:
        raise ValueError(SECRET_QUESTION)
    except ValueError:
        record = logging.LogRecord("x", logging.ERROR, "f", 1, "failed", (), sys.exc_info())
    entry = json.loads(JsonFormatter().format(record))
    assert entry["error_type"] == "ValueError"
    assert "zebrafinch" not in json.dumps(entry)


def test_personal_data_in_a_message_is_masked():
    assert format_record("Call %s", ("9876543210",))["message"] == "Call [PHONE]"


class FailingService:
    def ask(self, *args, **kwargs):
        raise RuntimeError(f"The provider refused: {SECRET_QUESTION}")


class WorkingService:
    def ask(self, question, *args, **kwargs):
        return SimpleNamespace(answer="Yes", sources=[], chunks=[], conversation_id=None, intent="", pending_action=None,
                               message_id=None)


@pytest.fixture
def logs(monkeypatch):
    """Capture what would really be written, in the real format."""
    lines: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            lines.append(self.format(record))

    handler = Capture()
    handler.addFilter(MaskingFilter())
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("hr_helpdesk")
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.INFO)
    monkeypatch.setattr(config, "API_KEY", "test-key")
    security.rate_limit.reset()
    yield lines
    logger.removeHandler(handler)
    logger.setLevel(previous)
    if hasattr(app.state, "service"):
        del app.state.service


def client_for(service) -> TestClient:
    app.state.service = service
    app.state.count_chunks = lambda: 1
    return TestClient(app)


def test_a_request_is_logged_with_its_id_and_without_the_question(logs):
    with client_for(WorkingService()) as client:
        response = client.post("/ask", headers=KEY, json={"question": SECRET_QUESTION})
    assert response.status_code == 200
    entries = [json.loads(line) for line in logs]
    request = next(entry for entry in entries if entry.get("event") == "request")
    assert request["request_id"] == response.headers["X-Request-ID"]
    assert (request["method"], request["path"], request["status"]) == ("POST", "/ask", 200)
    assert request["tenant_id"] == config.TENANT_ID
    assert isinstance(request["duration_ms"], int)
    assert "zebrafinch" not in "".join(logs)


def test_a_failure_is_logged_with_the_request_id_and_without_the_question(logs):
    with client_for(FailingService()) as client:
        response = client.post("/ask", headers=KEY, json={"question": SECRET_QUESTION})
    assert response.status_code == 503
    entries = [json.loads(line) for line in logs]
    failure = next(entry for entry in entries if entry["level"] == "ERROR")
    assert failure["error_type"] == "RuntimeError"
    assert failure["request_id"] == response.headers["X-Request-ID"]
    assert "zebrafinch" not in "".join(logs)


def test_a_request_id_can_never_look_like_a_phone_number():
    assert all(observability.new_request_id().startswith("r") for _ in range(20))


# Usage

def generation(input_tokens=0, output_tokens=0):
    return LLMResult(generations=[[ChatGeneration(message=AIMessage(content="x", usage_metadata={
        "input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens}))]])


def test_usage_adds_up_calls_tokens_and_time():
    usage = UsageCallback()
    first, second = uuid.uuid4(), uuid.uuid4()
    usage.on_chat_model_start({}, [], run_id=first)
    usage.on_llm_end(generation(100, 20), run_id=first)
    usage.on_chat_model_start({}, [], run_id=second)
    usage.on_llm_end(generation(50, 10), run_id=second)
    assert (usage.llm_calls, usage.input_tokens, usage.output_tokens) == (2, 150, 30)
    assert usage.llm_ms >= 0


def test_langchain_can_merge_usage_with_other_callbacks():
    """A real model's structured output merges callbacks into a set, which failed on every question without this."""
    first, second = UsageCallback(), UsageCallback()
    merged = CallbackManager([first]).merge(CallbackManager([second]))
    assert set(merged.handlers) == {first, second}


def test_a_failed_call_counts_with_no_tokens():
    usage = UsageCallback()
    run = uuid.uuid4()
    usage.on_chat_model_start({}, [], run_id=run)
    usage.on_llm_error(RuntimeError("quota"), run_id=run)
    assert (usage.llm_calls, usage.input_tokens) == (1, 0)


def test_a_model_that_reports_no_usage_is_fine():
    usage = UsageCallback()
    run = uuid.uuid4()
    usage.on_llm_start({}, [], run_id=run)
    usage.on_llm_end(LLMResult(generations=[[ChatGeneration(message=AIMessage(content="x"))]]), run_id=run)
    assert usage.input_tokens == 0


def test_cost_uses_the_configured_prices(monkeypatch):
    assert estimated_cost(1_000_000, 1_000_000) == 0.0  # a free tier by default
    monkeypatch.setattr(config, "LLM_PRICE_INPUT_PER_MILLION", 0.5)
    monkeypatch.setattr(config, "LLM_PRICE_OUTPUT_PER_MILLION", 2.0)
    assert estimated_cost(1_000_000, 500_000) == pytest.approx(1.5)


# Tracing

class FakeLangfuse:
    """Stands in for the langfuse package, so no account or network is needed."""

    def __init__(self):
        self.client_options = {}
        self.closed = False

    def __call__(self, name):
        fake = self

        class Client:
            def __init__(self, **options):
                fake.client_options = options

            def shutdown(self):
                fake.closed = True

        class Handler:
            def __init__(self, public_key):
                self.public_key = public_key

        return SimpleNamespace(Langfuse=Client) if name == "langfuse" else SimpleNamespace(CallbackHandler=Handler)


def test_tracing_is_off_unless_both_keys_are_set(monkeypatch):
    def must_not_load(name):
        raise AssertionError("Langfuse shouldn't be loaded")

    assert Tracing(must_not_load).callbacks == []
    monkeypatch.setattr(config, "LANGFUSE_PUBLIC_KEY", "pk")
    assert Tracing(must_not_load).callbacks == []


def test_tracing_masks_personal_data_before_sending(monkeypatch):
    monkeypatch.setattr(config, "LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setattr(config, "LANGFUSE_SECRET_KEY", "sk")
    fake = FakeLangfuse()
    tracing = Tracing(fake)
    assert len(tracing.callbacks) == 1
    assert fake.client_options["public_key"] == "pk"
    assert fake.client_options["mask"](data={"text": "Call 9876543210"}) == {"text": "Call [PHONE]"}
    tracing.close()
    assert fake.closed


def test_a_broken_tracing_setup_only_switches_tracing_off(monkeypatch):
    monkeypatch.setattr(config, "LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setattr(config, "LANGFUSE_SECRET_KEY", "sk")

    def missing(name):
        raise ImportError("not installed")

    tracing = Tracing(missing)
    assert tracing.callbacks == []
    tracing.close()
