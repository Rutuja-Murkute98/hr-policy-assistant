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

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app import config
from app.admin import router as admin_router
from app.agent.tools import ActionNotFound, ActionNotPending
from app.memory import ConversationNotFound
from app.rag.retrieval import SearchFilters
from app.rag.store import count_chunks, run_migrations
from app.security import rate_limit, verify_api_key
from app.workflow import create_workflow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("hr_helpdesk")

UNAVAILABLE = "The AI service is unavailable right now. Please try again later."


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Connect once at startup. Indexing is a separate script, so this is fast.
    if not hasattr(app.state, "service"):
        run_migrations()
        app.state.service = create_workflow()
        app.state.count_chunks = count_chunks
    yield
    close = getattr(app.state.service, "close", None)
    if close:
        close()


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


class AskResponse(BaseModel):
    answer: str
    sources: list[Source]
    request_id: str
    conversation_id: str | None = None
    intent: str = ""
    pending_action: dict | None = None  # an action waiting for approval, with its id and summary


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
    """Check the database without using any Gemini quota."""
    try:
        chunks = request.app.state.count_chunks()
    except Exception:
        logger.exception("request_id=%s health check failed", request.state.request_id)
        raise HTTPException(503, "Database unavailable") from None
    if chunks == 0:
        raise HTTPException(503, "No policies indexed. Run: python -m pipeline.embed_and_store")
    return {"status": "ok", "chunks_indexed": chunks}


def user_id_header(x_user_id: str = Header(default="", max_length=64, pattern=r"^[A-Za-z0-9_-]*$")) -> str | None:
    """Say who this request is for. Only callers with the API key get this far, so it's trusted.

    Phase 4 replaces this with real sign-in. Until then, anyone with the API key can name any user.
    """
    return x_user_id or None


def required_user(user_id: str | None = Depends(user_id_header)) -> str:
    if not user_id:
        raise HTTPException(422, "This needs an X-User-Id header")
    return user_id


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
def ask(body: AskRequest, request: Request, user_id: str | None = Depends(user_id_header)):
    question, history, filters = parse_request(body)
    try:
        result = request.app.state.service.ask(question, history, filters, user_id=user_id,
                                               conversation_id=body.conversation_id)
    except ConversationNotFound:
        raise HTTPException(404, "Conversation not found") from None
    except Exception:
        # Usually a Gemini quota or network problem. The details stay in the log.
        logger.exception("request_id=%s failed to answer", request.state.request_id)
        raise HTTPException(503, UNAVAILABLE) from None
    return AskResponse(answer=result.answer, sources=result.sources, request_id=request.state.request_id,
                       conversation_id=result.conversation_id, intent=result.intent,
                       pending_action=result.pending_action)


def server_sent_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.post("/ask/stream", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
def ask_stream(body: AskRequest, request: Request, user_id: str | None = Depends(user_id_header)):
    """Send the answer as it's written, as server-sent events: token, then done or error."""
    question, history, filters = parse_request(body)
    service = request.app.state.service
    request_id = request.state.request_id

    def events():
        try:
            for kind, value in service.stream(question, history, filters, user_id=user_id,
                                              conversation_id=body.conversation_id):
                if kind == "token":
                    yield server_sent_event("token", {"text": value})
                else:
                    # The final text can differ from the tokens if a citation check failed, so it's sent too
                    yield server_sent_event("done", {"answer": value.answer, "sources": value.sources,
                                                     "request_id": request_id, "intent": value.intent,
                                                     "conversation_id": value.conversation_id,
                                                     "pending_action": value.pending_action})
        except ConversationNotFound:
            yield server_sent_event("error", {"detail": "Conversation not found"})
        except Exception:
            # The response has already started, so the error has to be sent as an event
            logger.exception("request_id=%s failed to answer", request_id)
            yield server_sent_event("error", {"detail": UNAVAILABLE})

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class Decision(BaseModel):
    approve: bool


@app.get("/conversations", dependencies=[Depends(verify_api_key)])
def list_conversations(request: Request, user_id: str = Depends(required_user)):
    return request.app.state.service.conversations.conversations(user_id)


@app.get("/conversations/{conversation_id}/messages", dependencies=[Depends(verify_api_key)])
def conversation_messages(conversation_id: str, request: Request, user_id: str = Depends(required_user)):
    try:
        return request.app.state.service.conversations.messages(user_id, conversation_id)
    except ConversationNotFound:
        raise HTTPException(404, "Conversation not found") from None


@app.delete("/conversations/{conversation_id}", dependencies=[Depends(verify_api_key)])
def delete_conversation(conversation_id: str, request: Request, user_id: str = Depends(required_user)):
    try:
        request.app.state.service.delete_conversation(user_id, conversation_id)
    except ConversationNotFound:
        raise HTTPException(404, "Conversation not found") from None
    return {"deleted": conversation_id}


@app.delete("/conversations", dependencies=[Depends(verify_api_key)])
def delete_all_conversations(request: Request, user_id: str = Depends(required_user)):
    """Delete this user's whole chat history."""
    return {"deleted": request.app.state.service.delete_user_history(user_id)}


@app.get("/actions", dependencies=[Depends(verify_api_key)])
def pending_actions(request: Request, user_id: str = Depends(required_user)):
    return request.app.state.service.actions.list_pending(user_id)


@app.post("/actions/{action_id}", dependencies=[Depends(verify_api_key), Depends(rate_limit)])
def decide_action(action_id: str, body: Decision, request: Request, user_id: str = Depends(required_user)):
    """Approve or decline an action. Only the person it's for can decide, and only once."""
    try:
        result = request.app.state.service.decide(user_id, action_id, body.approve)
    except ActionNotFound:
        raise HTTPException(404, "Action not found") from None
    except ActionNotPending:
        raise HTTPException(409, "This was already decided, or it expired") from None
    except Exception:
        logger.exception("request_id=%s failed to finish an action", request.state.request_id)
        raise HTTPException(503, UNAVAILABLE) from None
    return {"answer": result.answer, "status": result.tool_status, "conversation_id": result.conversation_id}
