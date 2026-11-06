"""Let HR manage the policies without a developer. Every route needs the admin key.

Uploading returns straight away and indexing runs as a job, which the admin screen can
watch and retry. Chunks and versions live in the database, so they survive a hosted server
restarting, unlike the copy saved in the policy folder.
"""
import logging
from datetime import date

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile

from app import config
from app.knowledge import DuplicateDocument, JobConflict, KnowledgeBase, NotFound, UploadDetails, UploadRejected
from app.security import verify_admin_key
from pipeline.extract import ExtractionError

logger = logging.getLogger("hr_helpdesk")
router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(verify_admin_key)])


def knowledge(request: Request) -> KnowledgeBase:
    return request.app.state.service.knowledge


@router.get("/documents")
def list_documents(request: Request):
    """List the policies in the knowledge base, with their current version."""
    return knowledge(request).documents()


@router.post("/documents", status_code=202)
def upload_document(
    request: Request,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    title: str | None = Form(default=None, max_length=200),
    category: str | None = Form(default=None, max_length=50),
    version_label: str | None = Form(default=None, max_length=50),
    effective_date: date | None = Form(default=None),
    uploaded_by: str = Form(default="unknown", min_length=1, max_length=100),
):
    """Add a policy or a new version of one. PDF, DOCX, TXT, HTML and Markdown are accepted."""
    # Read one byte past the limit, so a huge upload isn't held in memory to find out it's too big
    data = file.file.read(config.MAX_UPLOAD_BYTES + 1)
    details = UploadDetails(title or None, category or None, version_label or None, effective_date, uploaded_by)
    try:
        result = knowledge(request).add_upload(data, file.filename or "", details)
    except DuplicateDocument as duplicate:
        raise HTTPException(409, str(duplicate)) from None
    except JobConflict as conflict:
        raise HTTPException(409, str(conflict)) from None
    except (ExtractionError, UploadRejected) as rejected:
        raise HTTPException(422, str(rejected)) from None
    if result["job_id"]:
        background.add_task(knowledge(request).run_job, result["job_id"])
    return result


@router.get("/documents/{filename}/versions")
def document_versions(filename: str, request: Request):
    try:
        return knowledge(request).versions(filename)
    except NotFound:
        raise HTTPException(404, f"{filename} isn't in the knowledge base") from None


@router.delete("/documents/{filename}")
def delete_document(filename: str, request: Request, purge: bool = False):
    """Stop answering from a policy. Old versions stay as history unless purge is set."""
    try:
        return knowledge(request).remove(filename, purge)
    except NotFound:
        raise HTTPException(404, f"{filename} isn't in the knowledge base") from None


@router.get("/jobs")
def list_jobs(request: Request):
    return knowledge(request).jobs()


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    try:
        return knowledge(request).job(job_id)
    except NotFound:
        raise HTTPException(404, "Job not found") from None


@router.post("/jobs/{job_id}/retry", status_code=202)
def retry_job(job_id: str, request: Request, background: BackgroundTasks):
    """Run a failed job again. It carries on from what's already stored."""
    try:
        knowledge(request).retry_job(job_id)
    except NotFound:
        raise HTTPException(404, "Job not found") from None
    except JobConflict as conflict:
        raise HTTPException(409, str(conflict)) from None
    background.add_task(knowledge(request).run_job, job_id)
    return {"job_id": job_id, "status": "queued"}


@router.get("/unanswered")
def unanswered_questions(request: Request):
    """List what employees asked that the bot couldn't answer, most asked first."""
    return knowledge(request).unanswered_report()


@router.get("/feedback")
def feedback_summary(request: Request):
    return knowledge(request).feedback_summary()
