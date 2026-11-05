"""Let HR manage the policies without a developer. Every route needs the admin key.

Chunks live in the database, so an uploaded policy keeps working when a hosted server
restarts. The file saved in the policy folder is lost at that point, but it's only a copy.
"""
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app import config
from app.security import verify_admin_key
from pipeline.chunk_documents import HEADING, POLICY_DIR, chunk_text
from pipeline.embed_and_store import BATCH_SIZE, plan_sync, run_sync

logger = logging.getLogger("hr_helpdesk")
router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(verify_admin_key)])

SAFE_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\.md")


class Upload(BaseModel):
    filename: str
    content: str = Field(min_length=1, max_length=config.MAX_UPLOAD_CHARS)


def check_file_name(filename: str) -> None:
    # Only letters, numbers, _ and - are allowed, so a name can't point outside the policy folder
    if not SAFE_FILE_NAME.fullmatch(filename):
        raise HTTPException(422, "Use a name like Leave_Policy.md: letters, numbers, _ and -, ending in .md")


def check_content(content: str) -> None:
    if "\x00" in content:
        raise HTTPException(422, "The file isn't plain text")
    first_heading = HEADING.search(content)
    if not first_heading or len(first_heading[1]) != 1:
        raise HTTPException(422, "The file must start with a title heading, such as: # Leave Policy")


def policy_index(request: Request):
    return request.app.state.service.store


@router.get("/documents")
def list_documents(request: Request):
    """List the policies in the database and how many chunks each has."""
    documents: dict[str, dict] = {}
    for chunk in policy_index(request).stored_chunks().values():
        entry = documents.setdefault(chunk.metadata["source"],
                                     {"file": chunk.metadata["source"], "policy": chunk.metadata["policy"], "chunks": 0})
        entry["chunks"] += 1
    return sorted(documents.values(), key=lambda entry: entry["file"])


@router.post("/documents")
def upload_document(body: Upload, request: Request):
    """Add a policy or replace one with the same name. Only changed sections are embedded."""
    check_file_name(body.filename)
    check_content(body.content)
    chunks = chunk_text(body.content, body.filename)
    if not chunks:
        raise HTTPException(422, "No sections were found under the headings in this file")

    index = policy_index(request)
    plan = plan_sync(index.stored_chunks(), chunks, {body.filename})
    if len(plan.to_embed) > BATCH_SIZE:
        raise HTTPException(413, f"This needs {len(plan.to_embed)} sections embedded, and the limit for "
                                 f"one upload is {BATCH_SIZE}. Use: python -m pipeline.embed_and_store")
    try:
        run_sync(index, plan)
    except Exception:
        logger.exception("request_id=%s failed to index %s", request.state.request_id, body.filename)
        raise HTTPException(503, "The AI service is unavailable right now, so nothing was changed.")

    try:
        (POLICY_DIR / body.filename).write_text(body.content, encoding="utf-8")
    except OSError:
        logger.exception("Could not save a copy of %s", body.filename)
    return {"file": body.filename, **plan.summary()}


@router.delete("/documents/{filename}")
def delete_document(filename: str, request: Request):
    """Remove a policy, so the bot can no longer answer from it."""
    check_file_name(filename)
    index = policy_index(request)
    plan = plan_sync(index.stored_chunks(), [], {filename})
    if not plan.stale_ids:
        raise HTTPException(404, f"{filename} isn't in the knowledge base")
    run_sync(index, plan)
    (POLICY_DIR / filename).unlink(missing_ok=True)
    return {"file": filename, **plan.summary()}
