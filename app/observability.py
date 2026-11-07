"""Structured logs, per-answer usage and optional tracing.

Logs are one JSON object per line, tied together by a request id. They never contain question text:
log messages are fixed wording, and an exception is logged by its type and stack, not its message,
because a provider's error or a parsing failure can quote what the model was sent.
"""
import json
import logging
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from app import config
from app.pii import install_log_masking, mask_value

logger = logging.getLogger("hr_helpdesk")

request_id_var: ContextVar[str] = ContextVar("request_id", default="")
TEXT_FORMAT = "%(asctime)s %(levelname)s [%(request_id)s] %(message)s"
# Only these extra fields reach a log line, so a caller can't add something by accident
LOG_FIELDS = ("event", "method", "path", "status", "duration_ms", "tenant_id", "error_type", "error_stack")


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get() or "-"
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {"time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
                 "level": record.levelname, "logger": record.name, "message": record.getMessage()}
        request_id = getattr(record, "request_id", "") or request_id_var.get()
        if request_id and request_id != "-":
            entry["request_id"] = request_id
        if record.exc_info and record.exc_info[1]:  # only reached if the masking filter didn't run first
            record.error_type = record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
            record.error_stack = traceback.format_tb(record.exc_info[2])
        entry.update({name: getattr(record, name) for name in LOG_FIELDS if hasattr(record, name)})
        return json.dumps(entry, default=str)


def setup_logging() -> None:
    """Log in the configured format, with personal data masked, from every part of the app."""
    logging.basicConfig(level=config.LOG_LEVEL)
    logging.getLogger().setLevel(config.LOG_LEVEL)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # logs every model call's address, which adds nothing
    install_log_masking()
    formatter = JsonFormatter() if config.LOG_FORMAT == "json" else logging.Formatter(TEXT_FORMAT)
    for source in (logging.getLogger(), logging.getLogger("uvicorn")):
        for handler in source.handlers:
            if not any(isinstance(item, RequestIdFilter) for item in handler.filters):
                handler.addFilter(RequestIdFilter())
            handler.setFormatter(formatter)


def new_request_id() -> str:
    # The letter stops an id of all digits looking like a phone or Aadhaar number to the log masking
    return f"r{uuid.uuid4().hex[:11]}"


@dataclass(eq=False)  # LangChain keeps callbacks in a set, so each must be hashable
class UsageCallback(BaseCallbackHandler):
    """Add up the model calls of one answer. It records counts and times, never prompts or answers."""

    llm_ms: float = 0.0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    _started: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _start(self, run_id) -> None:
        with self._lock:
            self._started[run_id] = time.perf_counter()

    def _finish(self, run_id) -> None:
        with self._lock:
            started = self._started.pop(run_id, None)
            self.llm_calls += 1
            if started is not None:
                self.llm_ms += (time.perf_counter() - started) * 1000

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs) -> None:
        self._start(run_id)

    def on_llm_start(self, serialized, prompts, *, run_id, **kwargs) -> None:
        self._start(run_id)

    def on_llm_end(self, response, *, run_id, **kwargs) -> None:
        self._finish(run_id)
        for generations in response.generations:
            for generation in generations:
                usage = getattr(getattr(generation, "message", None), "usage_metadata", None) or {}
                with self._lock:
                    self.input_tokens += usage.get("input_tokens", 0)
                    self.output_tokens += usage.get("output_tokens", 0)

    def on_llm_error(self, error, *, run_id, **kwargs) -> None:
        self._finish(run_id)


def estimated_cost(input_tokens: int, output_tokens: int) -> float:
    """Work out a cost from the configured prices. Both default to 0, for a free tier."""
    return (input_tokens * config.LLM_PRICE_INPUT_PER_MILLION
            + output_tokens * config.LLM_PRICE_OUTPUT_PER_MILLION) / 1_000_000


def _mask_for_tracing(*, data: Any, **kwargs) -> Any:
    return mask_value(data)


def _langfuse_handler(load: Callable[[str], Any] | None = None):
    from importlib import import_module

    load = load or import_module
    client = load("langfuse").Langfuse(public_key=config.LANGFUSE_PUBLIC_KEY, secret_key=config.LANGFUSE_SECRET_KEY,
                                       base_url=config.LANGFUSE_BASE_URL or None, mask=_mask_for_tracing)
    return client, load("langfuse.langchain").CallbackHandler(public_key=config.LANGFUSE_PUBLIC_KEY)


class Tracing:
    """Send model calls to Langfuse if it's set up. This shares prompts, so it's off unless both keys are set.

    A tracing problem must never stop an answer, so a failure to start only switches it off.
    """

    def __init__(self, load: Callable[[str], Any] | None = None):
        self.client = None
        self.handler = None
        if not (config.LANGFUSE_PUBLIC_KEY and config.LANGFUSE_SECRET_KEY):
            return
        try:
            self.client, self.handler = _langfuse_handler(load)
        except Exception as error:
            logger.warning("Tracing was switched off, because Langfuse couldn't start: %s", type(error).__name__)

    @property
    def callbacks(self) -> list:
        return [self.handler] if self.handler else []

    def close(self) -> None:
        if self.client:
            try:
                self.client.shutdown()
            except Exception as error:
                logger.warning("Langfuse didn't close cleanly: %s", type(error).__name__)
