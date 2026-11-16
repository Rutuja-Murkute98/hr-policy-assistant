"""Let HR manage the policies without a developer. Every route needs an HR admin, for their own organisation.

Uploading returns straight away and indexing runs as a job, which the admin screen can
watch and retry. Chunks and versions live in the database, so they survive a hosted server
restarting, unlike the copy saved in the policy folder.
"""
import logging
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Body, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field, ValidationError

from app import config
from app.audience import Audience
from app.connectors.base import ConnectorError
from app.identity import Principal
from app.knowledge import DuplicateDocument, JobConflict, KnowledgeBase, NotFound, UploadDetails, UploadRejected
from app.org_settings import SettingsError, effective_settings
from app.policy_checks import PolicyChecks, after_indexing
from app.reindex import latest_run
from app.security import admin_principal, audit, service_for
from app.sources import NewSource, SourceChange, SourceStore, available_kinds, sync_source
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
    review_date: date | None = Form(default=None),
):
    """Add a policy or a new version of one. PDF, DOCX, TXT, HTML and Markdown are accepted."""
    # Read one byte past the limit, so a huge upload isn't held in memory to find out it's too big
    data = file.file.read(config.MAX_UPLOAD_BYTES + 1)
    # A signed-in admin is who their sign-in says. The form's name is only for demo mode's shared key.
    details = UploadDetails(title or None, category or None, version_label or None, effective_date,
                            principal.email or uploaded_by, review_date)
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
        llm = getattr(service_for(request, principal), "llm", None)
        background.add_task(index_then_tag, base, llm, result["job_id"], result["file"])
    return result


def index_then_tag(base: KnowledgeBase, llm, job_id: str, source_file: str) -> None:
    """Index an upload, then run the checks that follow: who sections are for, what changed, and clashes (D60)."""
    base.run_job(job_id)
    if llm is not None:
        after_indexing(base, llm, source_file)


@router.post("/documents/preview")
def preview_document(principal: Principal = Depends(admin_principal), base: KnowledgeBase = Depends(knowledge),
                     file: UploadFile = File(...), title: str | None = Form(default=None, max_length=200)):
    """Show how a file would be read, section by section, next to its first pages. Nothing is stored."""
    data = file.file.read(config.MAX_UPLOAD_BYTES + 1)
    try:
        return base.preview_upload(data, file.filename or "", title or None)
    except (ExtractionError, UploadRejected) as rejected:
        raise HTTPException(422, str(rejected)) from None


@router.post("/documents/suggest-category")
def suggest_document_category(request: Request, principal: Principal = Depends(admin_principal),
                              file: UploadFile = File(...), title: str | None = Form(default=None, max_length=200)):
    """Suggest one of this organisation's categories for a file, for HR to confirm. Nothing is stored."""
    workflow = service_for(request, principal)
    data = file.file.read(config.MAX_UPLOAD_BYTES + 1)
    try:
        return workflow.knowledge.suggest_category(workflow.llm, data, file.filename or "", title or None)
    except (ExtractionError, UploadRejected) as rejected:
        raise HTTPException(422, str(rejected)) from None
    except Exception:
        logger.exception("The model couldn't suggest a category")
        raise HTTPException(503, "The model couldn't suggest a category just now. "
                                 "Please choose one yourself.") from None


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


@router.get("/reindex")
def reindex_status(request: Request, principal: Principal = Depends(admin_principal)):
    """The latest re-index for a new embedding model, if there was one. Searches use the old model until it's done."""
    return {"run": latest_run(registry_of(request).engine, principal.tenant_id)}


@router.post("/reindex/retry", status_code=202)
def retry_reindex(request: Request, principal: Principal = Depends(admin_principal)):
    """Run a failed re-index again. It carries on from the chunks it already embedded."""
    if not registry_of(request).retry_reindex(principal.tenant_id):
        raise HTTPException(409, "There's no failed re-index to retry")
    audit(request, principal, "reindex_retried")
    return latest_run(registry_of(request).engine, principal.tenant_id)



@router.get("/audiences")
def list_audiences(file: str | None = None, base: KnowledgeBase = Depends(knowledge)):
    """Who each section applies to, from the model or HR, and how many sections aren't tagged yet."""
    return base.audiences(file)


@router.post("/audiences/tag", status_code=202)
def tag_audiences(request: Request, background: BackgroundTasks, principal: Principal = Depends(admin_principal),
                  base: KnowledgeBase = Depends(knowledge)):
    """Ask the model who every untagged section is for, one call per document, in the background."""
    untagged = base.audiences()["untagged"]
    if untagged:
        background.add_task(base.tag_audiences, service_for(request, principal).llm)
        audit(request, principal, "audiences_tagged", untagged=untagged)
    return {"untagged": untagged}


@router.put("/audiences/{chunk_id:path}")
def set_audience(chunk_id: str, audience: Audience, request: Request, principal: Principal = Depends(admin_principal),
                 base: KnowledgeBase = Depends(knowledge)):
    """Correct who a section is for. An empty body means everyone. The model never changes it back."""
    try:
        result = base.set_audience(chunk_id, audience, principal.user_id or principal.source)
    except NotFound:
        raise HTTPException(404, "That section isn't in the knowledge base") from None
    audit(request, principal, "audience_set", chunk_id, applies_to=result["applies_to"])
    return result


def sources_of(base: KnowledgeBase) -> SourceStore:
    return SourceStore(base.engine, base.tenant_id)


@router.get("/sources")
def list_sources(base: KnowledgeBase = Depends(knowledge)):
    """This organisation's sources, and which kinds this server can read."""
    return {"sources": sources_of(base).all(), "available_kinds": available_kinds()}


@router.post("/sources", status_code=201)
def add_source(new: NewSource, request: Request, principal: Principal = Depends(admin_principal),
               base: KnowledgeBase = Depends(knowledge)):
    """Add a folder, Google Drive or SharePoint source. It syncs at its first scheduled time, or when asked."""
    try:
        source = sources_of(base).add(new, principal.user_id or principal.source)
    except ConnectorError as error:
        raise HTTPException(422, str(error)) from None
    audit(request, principal, "source_added", source["name"], kind=new.kind)
    return source


@router.patch("/sources/{source_id}")
def change_source(source_id: str, change: SourceChange, request: Request,
                  principal: Principal = Depends(admin_principal), base: KnowledgeBase = Depends(knowledge)):
    try:
        source = sources_of(base).change(source_id, change)
    except NotFound:
        raise HTTPException(404, "Source not found") from None
    audit(request, principal, "source_changed", source["name"], **change.model_dump(exclude_none=True))
    return source


@router.delete("/sources/{source_id}")
def remove_source(source_id: str, request: Request, principal: Principal = Depends(admin_principal),
                  base: KnowledgeBase = Depends(knowledge)):
    """Stop syncing a source. Its policies stay, and can be removed one by one."""
    try:
        sources_of(base).remove(source_id)
    except NotFound:
        raise HTTPException(404, "Source not found") from None
    audit(request, principal, "source_removed", source_id)
    return {"removed": source_id}


@router.post("/sources/{source_id}/sync", status_code=202)
def sync_now(source_id: str, request: Request, background: BackgroundTasks,
             principal: Principal = Depends(admin_principal), base: KnowledgeBase = Depends(knowledge)):
    """Sync a source now, in the background, unless it's syncing already."""
    store = sources_of(base)
    try:
        source = store.get(source_id)
    except NotFound:
        raise HTTPException(404, "Source not found") from None
    if not store.claim(source_id):
        raise HTTPException(409, "This source is syncing already")
    audit(request, principal, "source_synced", source["name"])
    background.add_task(sync_source, store, base, source, llm=getattr(service_for(request, principal), "llm", None))
    return {"id": source_id, "status": "running"}


@router.get("/sources/{source_id}/files")
def source_files(source_id: str, base: KnowledgeBase = Depends(knowledge)):
    try:
        sources_of(base).get(source_id)
    except NotFound:
        raise HTTPException(404, "Source not found") from None
    return sources_of(base).files(source_id)


class ClashDecision(BaseModel):
    chunk_a: str = Field(min_length=1, max_length=400)
    chunk_b: str = Field(min_length=1, max_length=400)
    status: Literal["open", "resolved", "dismissed"]


@router.get("/clashes")
def list_clashes(status: Literal["open", "resolved", "dismissed"] = "open",
                 base: KnowledgeBase = Depends(knowledge)):
    """Sections that seem to contradict each other, for HR to settle. See docs/DECISIONS.md, D60."""
    return PolicyChecks(base).clashes(status)


@router.patch("/clashes")
def decide_clash(decision: ClashDecision, request: Request, principal: Principal = Depends(admin_principal),
                 base: KnowledgeBase = Depends(knowledge)):
    """Mark a clash resolved, as a policy was fixed, or dismissed. It's raised again if either text changes."""
    try:
        PolicyChecks(base).decide(decision.chunk_a, decision.chunk_b, decision.status,
                                  principal.user_id or principal.source)
    except NotFound:
        raise HTTPException(404, "Clash not found") from None
    audit(request, principal, "clash_decided", f"{decision.chunk_a} | {decision.chunk_b}", status=decision.status)
    return {"status": decision.status}


@router.post("/clashes/check", status_code=202)
def check_all_clashes(request: Request, background: BackgroundTasks, principal: Principal = Depends(admin_principal),
                      base: KnowledgeBase = Depends(knowledge)):
    """Check every policy for clashes, in the background. Pairs already checked, with the same text, are skipped."""
    llm = getattr(service_for(request, principal), "llm", None)
    if llm is None:
        raise HTTPException(503, "The model isn't available to check for clashes")
    background.add_task(PolicyChecks(base).check_clashes, llm, None, 5.0)
    audit(request, principal, "clashes_checked")
    return {"status": "checking"}
