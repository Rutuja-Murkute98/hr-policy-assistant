"""Stage E: the web API.

Run from the project folder:  venv\\Scripts\\uvicorn app.main:app --port 8000
Then open http://127.0.0.1:8000/docs for a testing page.
"""
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from app import config
from app.rag.service import create_service
from app.rag.store import count_chunks
from app.security import rate_limit, verify_api_key

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("hr_helpdesk")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Connect once at startup. Indexing is a separate script, so this is fast.
    if not hasattr(app.state, "service"):
        app.state.service = create_service()
        app.state.count_chunks = count_chunks
    yield


app = FastAPI(title="HR Helpdesk RAG", lifespan=lifespan)


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=config.MAX_QUESTION_CHARS)


class Source(BaseModel):
    policy: str
    section: str
    file: str


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


@app.get("/health")
def health(request: Request):
    """Check the database without using any Gemini quota."""
    try:
        chunks = request.app.state.count_chunks()
    except Exception:
        logger.exception("request_id=%s health check failed", request.state.request_id)
        raise HTTPException(503, "Database unavailable")
    if chunks == 0:
        raise HTTPException(503, "No policies indexed. Run: python -m pipeline.embed_and_store")
    return {"status": "ok", "chunks_indexed": chunks}


@app.post("/ask", response_model=AskResponse,
          dependencies=[Depends(verify_api_key), Depends(rate_limit)])
def ask(body: AskRequest, request: Request):
    question = body.question.strip()
    if len(question) < 2:
        raise HTTPException(422, "Please type a question")
    try:
        result = request.app.state.service.ask(question)
    except Exception:
        # Usually a Gemini quota or network problem. The details stay in the log.
        logger.exception("request_id=%s failed to answer", request.state.request_id)
        raise HTTPException(503, "The AI service is unavailable right now. Please try again later.")
    return AskResponse(answer=result.answer, sources=result.sources,
                       request_id=request.state.request_id)
