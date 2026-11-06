"""Stage E: the web API.

Run from the project folder:  venv\\Scripts\\uvicorn app.main:app --port 8000
Then open http://127.0.0.1:8000/docs for a testing page.
"""
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app import config
from app.admin import router as admin_router
from app.rag.retrieval import SearchFilters
from app.rag.service import create_service
from app.rag.store import count_chunks, run_migrations
from app.security import rate_limit, verify_api_key

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("hr_helpdesk")

UNAVAILABLE = "The AI service is unavailable right now. Please try again later."
AI_FAILURE_MEMORY_SECONDS = 300  # how long /health reports a failed AI call, if nothing has worked since


def record_ai_failure(app: FastAPI) -> None:
    app.state.last_ai_failure = time.monotonic()


def record_ai_result(app: FastAPI, result) -> None:
    """Clear a failure once the AI really answered. A greeting or an empty search doesn't call it."""
    if result.chunks:
        app.state.last_ai_failure = None


def ai_status(app: FastAPI) -> str:
    failed_at = getattr(app.state, "last_ai_failure", None)
    if failed_at is None or time.monotonic() - failed_at > AI_FAILURE_MEMORY_SECONDS:
        return "ok"
    return "unavailable"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Connect once at startup. Indexing is a separate script, so this is fast.
    if not hasattr(app.state, "service"):
        run_migrations()
        app.state.service = create_service()
        app.state.count_chunks = count_chunks
    yield


app = FastAPI(title="HR Helpdesk RAG", lifespan=lifespan)
app.include_router(admin_router)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=config.MAX_MESSAGE_CHARS)


class Filters(BaseModel):
    """Optionally limit the search. The tenant always comes from the server, never from the caller."""

    model_config = ConfigDict(extra="forbid")
    policy: str | None = None
    category: str | None = None
    effective_on: date | None = None  # only policies in effect on this date


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=config.MAX_QUESTION_CHARS)
    # Earlier messages in this chat, oldest first. The server keeps no chat history itself.
    history: list[ChatMessage] = Field(default_factory=list, max_length=config.MAX_HISTORY_MESSAGES)
    filters: Filters = Field(default_factory=Filters)


class Source(BaseModel):
    ref: int  # the number used in the answer text, such as [1]
    policy: str
    section: str
    file: str
    chunk_id: str
    text: str = ""  # the policy wording this citation points to


class AskResponse(BaseModel):
    answer: str
    sources: list[Source]
    request_id: str


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Give each request an id and log it. Questions aren't logged, as they may be personal."""
    request.state.request_id = uuid.uuid4().hex[:12]
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    logger.info("request_id=%s %s %s status=%s duration_ms=%.0f", request.state.request_id,
                request.method, request.url.path, response.status_code,
                (time.perf_counter() - started) * 1000)
    return response


@app.get("/live")
def live():
    """Say the server is running. Render checks this often, so it must not wake the database."""
    return {"status": "ok"}


@app.get("/health")
def health(request: Request):
    """Check the database, and report recent AI failures, without using any Gemini quota."""
    try:
        chunks = request.app.state.count_chunks()
    except Exception:
        logger.exception("request_id=%s health check failed", request.state.request_id)
        raise HTTPException(503, "Database unavailable") from None
    if chunks == 0:
        raise HTTPException(503, "No policies indexed. Run: python -m pipeline.embed_and_store")
    return {"status": "ok", "chunks_indexed": chunks, "ai": ai_status(request.app)}


def parse_request(body: AskRequest) -> tuple[str, list[dict], SearchFilters]:
    question = body.question.strip()
    if len(question) < 2:
        raise HTTPException(422, "Please type a question")
    effective_on = body.filters.effective_on
    filters = SearchFilters(tenant_id=config.TENANT_ID, policy=body.filters.policy, category=body.filters.category,
                            effective_on=effective_on.isoformat() if effective_on else None)
    return question, [message.model_dump() for message in body.history], filters


@app.post("/ask", response_model=AskResponse,
          dependencies=[Depends(verify_api_key), Depends(rate_limit)])
def ask(body: AskRequest, request: Request):
    question, history, filters = parse_request(body)
    try:
        result = request.app.state.service.ask(question, history, filters)
    except Exception:
        # Usually a Gemini quota or network problem. The details stay in the log.
        logger.exception("request_id=%s failed to answer", request.state.request_id)
        record_ai_failure(request.app)
        raise HTTPException(503, UNAVAILABLE) from None
    record_ai_result(request.app, result)
    return AskResponse(answer=result.answer, sources=result.sources,
                       request_id=request.state.request_id)


def server_sent_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.post("/ask/stream", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
def ask_stream(body: AskRequest, request: Request):
    """Send the answer as it's written, as server-sent events: token, then done or error."""
    question, history, filters = parse_request(body)
    service = request.app.state.service
    request_id = request.state.request_id
    api = request.app

    def events():
        try:
            for kind, value in service.stream(question, history, filters):
                if kind == "token":
                    yield server_sent_event("token", {"text": value})
                else:
                    record_ai_result(api, value)
                    # The final text can differ from the tokens if a citation check failed, so it's sent too
                    yield server_sent_event("done", {"answer": value.answer, "sources": value.sources,
                                                     "request_id": request_id})
        except Exception:
            # The response has already started, so the error has to be sent as an event
            logger.exception("request_id=%s failed to answer", request_id)
            record_ai_failure(api)
            yield server_sent_event("error", {"detail": UNAVAILABLE})

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
