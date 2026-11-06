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
from app.accounts import admin_router as accounts_admin_router
from app.accounts import platform_router
from app.accounts import router as accounts_router
from app.admin import router as admin_router
from app.agent.tools import ActionNotFound, ActionNotPending
from app.identity import Principal, create_identity
from app.knowledge import NotFound
from app.memory import ConversationNotFound
from app.pii import install_log_masking
from app.rag.retrieval import SearchFilters
from app.rag.store import count_chunks, run_migrations
from app.security import audit, current_principal, get_rate_store, rate_limit, required_user, service_for
from app.workflow import create_registry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
install_log_masking()
logger = logging.getLogger("hr_helpdesk")

UNAVAILABLE = "The AI service is unavailable right now. Please try again later."
AI_FAILURE_MEMORY_SECONDS = 300  # how long /health reports a failed AI call, if nothing has worked since


def record_ai_failure(app: FastAPI) -> None:
    app.state.last_ai_failure = time.monotonic()


def record_ai_result(app: FastAPI, result) -> None:
    """Clear a failure once the AI really answered. A greeting or an empty search doesn't call it."""
    if result.chunks or result.intent in ("out_of_scope", "personal_data_request", "action_request"):
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
        registry = create_registry()
        app.state.service = registry
        app.state.identity = create_identity(registry.engine)
        app.state.count_chunks = count_chunks
        app.state.identity.audit.purge_older_than(config.AUDIT_RETENTION_DAYS)
        rate_limit.store = get_rate_store(registry.engine)
    yield
    close = getattr(app.state.service, "close", None)
    if close:
        close()


app = FastAPI(title="HR Helpdesk RAG", lifespan=lifespan)
app.include_router(admin_router)
app.include_router(accounts_router)
app.include_router(accounts_admin_router)
app.include_router(platform_router)


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
    conversation_id: str | None = Field(default=None, max_length=64)  # continue a stored chat
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
    superseded: bool = False  # from an old version of the policy


class AskResponse(BaseModel):
    answer: str
    sources: list[Source]
    request_id: str
    conversation_id: str | None = None
    intent: str = ""
    pending_action: dict | None = None  # an action waiting for approval, with its id and summary
    message_id: int | None = None  # the stored answer, for feedback


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Give each request an id and log it. Questions aren't logged, as they may be personal."""
    # The letter stops an id of all digits looking like a phone or Aadhaar number to the log masking
    request.state.request_id = f"r{uuid.uuid4().hex[:11]}"
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


def parse_request(body: AskRequest, principal: Principal) -> tuple[str, list[dict], SearchFilters]:
    question = body.question.strip()
    if len(question) < 2:
        raise HTTPException(422, "Please type a question")
    effective_on = body.filters.effective_on
    filters = SearchFilters(tenant_id=principal.tenant_id, policy=body.filters.policy, category=body.filters.category,
                            effective_on=effective_on.isoformat() if effective_on else None)
    return question, [message.model_dump() for message in body.history], filters


@app.post("/ask", response_model=AskResponse, dependencies=[Depends(rate_limit)])
def ask(body: AskRequest, request: Request, principal: Principal = Depends(current_principal)):
    question, history, filters = parse_request(body, principal)
    service = service_for(request, principal)
    try:
        result = service.ask(question, history, filters, user_id=principal.user_id,
                             conversation_id=body.conversation_id)
    except ConversationNotFound:
        raise HTTPException(404, "Conversation not found") from None
    except Exception:
        # Usually a Gemini quota or network problem. The details stay in the log.
        logger.exception("request_id=%s failed to answer", request.state.request_id)
        record_ai_failure(request.app)
        raise HTTPException(503, UNAVAILABLE) from None
    record_ai_result(request.app, result)
    return AskResponse(answer=result.answer, sources=result.sources, request_id=request.state.request_id,
                       conversation_id=result.conversation_id, intent=result.intent,
                       pending_action=result.pending_action, message_id=result.message_id)


def server_sent_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.post("/ask/stream", dependencies=[Depends(rate_limit)])
def ask_stream(body: AskRequest, request: Request, principal: Principal = Depends(current_principal)):
    """Send the answer as it's written, as server-sent events: token, then done or error."""
    question, history, filters = parse_request(body, principal)
    service = service_for(request, principal)
    request_id = request.state.request_id
    api = request.app

    def events():
        try:
            for kind, value in service.stream(question, history, filters, user_id=principal.user_id,
                                              conversation_id=body.conversation_id):
                if kind == "token":
                    yield server_sent_event("token", {"text": value})
                elif kind == "status":
                    yield server_sent_event("status", {"text": value})
                else:
                    record_ai_result(api, value)
                    # The final text can differ from the tokens if a citation check failed, so it's sent too
                    yield server_sent_event("done", {"answer": value.answer, "sources": value.sources,
                                                     "request_id": request_id, "intent": value.intent,
                                                     "conversation_id": value.conversation_id,
                                                     "pending_action": value.pending_action,
                                                     "message_id": value.message_id})
        except ConversationNotFound:
            yield server_sent_event("error", {"detail": "Conversation not found"})
        except Exception:
            # The response has already started, so the error has to be sent as an event
            logger.exception("request_id=%s failed to answer", request_id)
            record_ai_failure(api)
            yield server_sent_event("error", {"detail": UNAVAILABLE})

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class Decision(BaseModel):
    approve: bool


class Feedback(BaseModel):
    message_id: int
    rating: Literal[1, -1]
    comment: str = Field(default="", max_length=1000)


@app.post("/feedback", dependencies=[Depends(rate_limit)])
def give_feedback(body: Feedback, request: Request, principal: Principal = Depends(required_user)):
    """Rate an answer. Rating it again replaces the earlier rating."""
    try:
        service_for(request, principal).knowledge.save_feedback(principal.user_id, body.message_id, body.rating,
                                                                body.comment)
    except NotFound:
        raise HTTPException(404, "Answer not found") from None
    return {"saved": True}


@app.get("/conversations")
def list_conversations(request: Request, principal: Principal = Depends(required_user)):
    return service_for(request, principal).conversations.conversations(principal.user_id)


@app.get("/conversations/{conversation_id}/messages")
def conversation_messages(conversation_id: str, request: Request, principal: Principal = Depends(required_user)):
    try:
        return service_for(request, principal).conversations.messages(principal.user_id, conversation_id)
    except ConversationNotFound:
        raise HTTPException(404, "Conversation not found") from None


@app.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, request: Request, principal: Principal = Depends(required_user)):
    try:
        service_for(request, principal).delete_conversation(principal.user_id, conversation_id)
    except ConversationNotFound:
        raise HTTPException(404, "Conversation not found") from None
    audit(request, principal, "conversation_deleted", conversation_id)
    return {"deleted": conversation_id}


@app.delete("/conversations")
def delete_all_conversations(request: Request, principal: Principal = Depends(required_user)):
    """Delete this user's whole chat history."""
    removed = service_for(request, principal).delete_user_history(principal.user_id)
    audit(request, principal, "conversation_history_deleted", count=removed)
    return {"deleted": removed}


@app.get("/actions")
def pending_actions(request: Request, principal: Principal = Depends(required_user)):
    return service_for(request, principal).actions.list_pending(principal.user_id)


@app.post("/actions/{action_id}", dependencies=[Depends(rate_limit)])
def decide_action(action_id: str, body: Decision, request: Request, principal: Principal = Depends(required_user)):
    """Approve or decline an action. Only the person it's for can decide, and only once."""
    try:
        result = service_for(request, principal).decide(principal.user_id, action_id, body.approve)
    except ActionNotFound:
        raise HTTPException(404, "Action not found") from None
    except ActionNotPending:
        raise HTTPException(409, "This was already decided, or it expired") from None
    except Exception:
        logger.exception("request_id=%s failed to finish an action", request.state.request_id)
        raise HTTPException(503, UNAVAILABLE) from None
    audit(request, principal, "action_approved" if body.approve else "action_declined", action_id,
          status=result.tool_status)
    return {"answer": result.answer, "status": result.tool_status, "conversation_id": result.conversation_id}
