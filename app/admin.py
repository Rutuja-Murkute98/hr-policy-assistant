"""Let HR manage the policies without a developer. Every route needs an HR admin, for their own organisation.

Uploading returns straight away and indexing runs as a job, which the admin screen can
watch and retry. Chunks and versions live in the database, so they survive a hosted server
restarting, unlike the copy saved in the policy folder.
"""
import logging
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Body, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import ValidationError

from app import config
from app.identity import Principal
from app.knowledge import DuplicateDocument, JobConflict, KnowledgeBase, NotFound, UploadDetails, UploadRejected
from app.org_settings import SettingsError, effective_settings
from app.security import admin_principal, audit, service_for
from pipeline.extract import ExtractionError

logger = logging.getLogger("hr_helpdesk")
router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(admin_principal)])


def knowledge(request: Request, principal: Principal = Depends(admin_principal)) -> KnowledgeBase:
    return service_for(request, principal).knowledge


@router.get("/documents")
def list_documents(base: KnowledgeBase = Depends(knowledge)):
    """List the policies in the knowledge base, with their current version."""
    return base.documents()


@router.post("/documents", status_code=202)
def upload_document(
    request: Request,
    background: BackgroundTasks,
    principal: Principal = Depends(admin_principal),
    base: KnowledgeBase = Depends(knowledge),
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
    # A signed-in admin is who their sign-in says. The form's name is only for demo mode's shared key.
    details = UploadDetails(title or None, category or None, version_label or None, effective_date,
                            principal.email or uploaded_by)
    try:
        result = base.add_upload(data, file.filename or "", details)
    except DuplicateDocument as duplicate:
        raise HTTPException(409, str(duplicate)) from None
    except JobConflict as conflict:
        raise HTTPException(409, str(conflict)) from None
    except (ExtractionError, UploadRejected) as rejected:
        raise HTTPException(422, str(rejected)) from None
    audit(request, principal, "document_uploaded", result["file"], status=result["status"],
          version=result["version"], injection_signs=result["warnings"])
    if result["job_id"]:
        background.add_task(base.run_job, result["job_id"])
    return result


@router.get("/documents/{filename}/versions")
def document_versions(filename: str, base: KnowledgeBase = Depends(knowledge)):
    try:
        return base.versions(filename)
    except NotFound:
        raise HTTPException(404, f"{filename} isn't in the knowledge base") from None


@router.delete("/documents/{filename}")
def delete_document(filename: str, request: Request, purge: bool = False,
                    principal: Principal = Depends(admin_principal), base: KnowledgeBase = Depends(knowledge)):
    """Stop answering from a policy. Old versions stay as history unless purge is set."""
    try:
        result = base.remove(filename, purge)
    except NotFound:
        raise HTTPException(404, f"{filename} isn't in the knowledge base") from None
    audit(request, principal, "document_removed", filename, purged=purge)
    return result


@router.get("/jobs")
def list_jobs(base: KnowledgeBase = Depends(knowledge)):
    return base.jobs()


@router.get("/jobs/{job_id}")
def get_job(job_id: str, base: KnowledgeBase = Depends(knowledge)):
    try:
        return base.job(job_id)
    except NotFound:
        raise HTTPException(404, "Job not found") from None


@router.post("/jobs/{job_id}/retry", status_code=202)
def retry_job(job_id: str, request: Request, background: BackgroundTasks,
              principal: Principal = Depends(admin_principal), base: KnowledgeBase = Depends(knowledge)):
    """Run a failed job again. It carries on from what's already stored."""
    try:
        base.retry_job(job_id)
    except NotFound:
        raise HTTPException(404, "Job not found") from None
    except JobConflict as conflict:
        raise HTTPException(409, str(conflict)) from None
    audit(request, principal, "job_retried", job_id)
    background.add_task(base.run_job, job_id)
    return {"job_id": job_id, "status": "queued"}


@router.get("/unanswered")
def unanswered_questions(base: KnowledgeBase = Depends(knowledge)):
    """List what employees asked that the bot couldn't answer, most asked first."""
    return base.unanswered_report()


@router.get("/feedback")
def feedback_summary(base: KnowledgeBase = Depends(knowledge)):
    return base.feedback_summary()


@router.get("/test-candidates")
def test_candidates(base: KnowledgeBase = Depends(knowledge)):
    """Unanswered questions and answers rated down, to become test items once a reviewer has labelled them."""
    return base.test_candidates()


def registry_of(request: Request):
    registry = request.app.state.service
    if not hasattr(registry, "settings"):
        raise HTTPException(404, "Organisation settings need the server that holds every organisation")
    return registry


def settings_view(registry, tenant_id: str) -> dict:
    profile = registry.profile_for(tenant_id)
    if profile is None:
        raise HTTPException(403, "This organisation isn't available")
    return {"changed": profile.overrides.model_dump(exclude_none=True), "effective": effective_settings(profile),
            "allowed_chat_models": config.ALLOWED_CHAT_MODELS, "max_excerpts": config.FETCH_K,
            "version": profile.version}


def readable(error: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(part) for part in problem['loc']) or 'settings'}: {problem['msg']}"
                     for problem in error.errors())


@router.get("/settings")
def get_settings(request: Request, principal: Principal = Depends(admin_principal)):
    """This organisation's settings: what it changed, and the value answers use for each one."""
    return settings_view(registry_of(request), principal.tenant_id)


@router.patch("/settings")
def change_settings(request: Request, changes: Annotated[dict[str, Any], Body()],
                    principal: Principal = Depends(admin_principal)):
    """Change some settings. A value of null puts that setting back to the deployment's. Takes effect at once."""
    registry = registry_of(request)
    try:
        _, changed = registry.settings.save(principal.tenant_id, changes, principal.user_id or principal.source)
    except ValidationError as error:
        raise HTTPException(422, readable(error)) from None
    except SettingsError as error:
        raise HTTPException(422, str(error)) from None
    if changed:
        audit(request, principal, "settings_changed", ", ".join(sorted(changed)), changes=changed)
    return settings_view(registry, principal.tenant_id)
