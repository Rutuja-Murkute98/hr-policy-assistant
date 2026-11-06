"""Manage the knowledge base: versions of each policy, indexing jobs, duplicates and reports.

Each upload becomes a new version and a job. A job can fail and be retried, and because it only
embeds what isn't stored yet, a retry carries on where it stopped. When text changes, the old
chunks are kept as superseded copies, with their embeddings, so old versions stay searchable.
"""
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from langchain_core.documents import Document
from sqlalchemy import Engine, text

from app import config
from app.rag.retrieval import body_of
from pipeline.chunk_documents import chunk_text
from pipeline.embed_and_store import SyncPlan, content_hash, plan_sync, run_sync
from pipeline.extract import extract_document

logger = logging.getLogger("hr_helpdesk")

SAFE_STEM = r"[A-Za-z0-9][A-Za-z0-9_-]*"
ACTIVE_JOB_STATES = ("queued", "running")


class UploadRejected(Exception):
    """The upload can't be accepted. The message is safe to show the person uploading it."""


class DuplicateDocument(UploadRejected):
    pass


class JobConflict(UploadRejected):
    pass


class NotFound(Exception):
    pass


@dataclass
class UploadDetails:
    title: str | None = None
    category: str | None = None
    version_label: str | None = None
    effective_date: date | None = None
    uploaded_by: str = "unknown"


def text_hash(content: str) -> str:
    return hashlib.sha256(content.strip().encode()).hexdigest()


def known_categories() -> set[str]:
    return {*config.tenant.document_categories.values(), config.tenant.default_category}


def overrides_of(row) -> dict:
    return {"policy": row.title, "category": row.category, "version": row.version_label,
            "effective_date": row.effective_date.isoformat() if row.effective_date else None}


class KnowledgeBase:
    def __init__(self, engine: Engine, index, tenant_id: str, policy_dir: Path | None = None):
        self.engine = engine
        self.index = index
        self.tenant_id = tenant_id
        self.policy_dir = policy_dir if policy_dir is not None else config.POLICY_DIR

    @property
    def _scope(self) -> dict:
        return {"tenant_id": self.tenant_id, "collection": self.index.store.collection_name}

    def current_number(self, source_file: str) -> int:
        with self.engine.connect() as connection:
            return connection.execute(text(
                "SELECT COALESCE(max(version_number), 0) FROM document_versions WHERE tenant_id = :tenant_id "
                "AND collection_name = :collection AND source_file = :file AND status = 'current'"
            ), {**self._scope, "file": source_file}).scalar() or 0

    def _insert_version(self, connection, source_file: str, details: dict, original_name: str, content: str,
                        status: str, uploaded_by: str) -> tuple[int, int]:
        number = (connection.execute(text(
            "SELECT COALESCE(max(version_number), 0) FROM document_versions WHERE tenant_id = :tenant_id "
            "AND collection_name = :collection AND source_file = :file"
        ), {**self._scope, "file": source_file}).scalar() or 0) + 1
        version_id = connection.execute(text(
            "INSERT INTO document_versions (tenant_id, collection_name, source_file, version_number, title, category, "
            "version_label, effective_date, original_name, text_hash, content_hash, content, status, uploaded_by) "
            "VALUES (:tenant_id, :collection, :file, :number, :title, :category, :label, CAST(:effective AS date), "
            ":original, :text_hash, :content_hash, :content, :status, :by) RETURNING id"
        ), {**self._scope, "file": source_file, "number": number, "title": details["policy"],
            "category": details["category"], "label": details.get("version"), "effective": details.get("effective_date"),
            "original": original_name, "text_hash": text_hash(content), "content_hash": details["content_hash"],
            "content": content, "status": status, "by": uploaded_by}).scalar_one()
        return version_id, number

    def record_baseline(self, source_file: str, chunks: list[Document], content: str, uploaded_by: str = "indexing") -> int:
        """Record a document indexed without a version yet, as version 1. It uses no embeddings."""
        details = dict(chunks[0].metadata)
        details["content_hash"] = content_hash(content, details["category"])
        with self.engine.begin() as connection:
            _, number = self._insert_version(connection, source_file, details, source_file, content, "current", uploaded_by)
        return number

    def _ensure_baseline(self, source_file: str) -> None:
        """A policy indexed before versions existed has no version yet, so give it one before replacing it."""
        if self.current_number(source_file):
            return
        chunks = [c for c in self.index.stored_chunks().values() if c.metadata["source"] == source_file]
        if chunks:
            ordered = sorted(chunks, key=lambda c: c.id or "")
            self.record_baseline(source_file, ordered, "\n\n".join(c.page_content for c in ordered))

    def add_upload(self, data: bytes, file_name: str, details: UploadDetails) -> dict:
        """Validate an upload and queue it for indexing. Raises UploadRejected if it can't be used."""
        if len(data) > config.MAX_UPLOAD_BYTES:
            raise UploadRejected(f"The file is larger than the {config.MAX_UPLOAD_BYTES // 1_000_000} MB limit.")
        stem = Path(file_name).stem
        if not re.fullmatch(SAFE_STEM, stem) or Path(file_name).name != file_name:
            raise UploadRejected("Use a file name like Leave_Policy.pdf: letters, numbers, _ and - only.")
        if details.category and details.category not in known_categories():
            raise UploadRejected(f"Category must be one of: {', '.join(sorted(known_categories()))}.")

        extracted = extract_document(data, file_name, details.title)
        if len(extracted.markdown) > config.MAX_UPLOAD_CHARS:
            raise UploadRejected(f"This has more than {config.MAX_UPLOAD_CHARS:,} characters of text.")
        source_file = f"{stem}.md"  # one name per policy, whatever format it arrives in
        overrides = {"policy": details.title, "category": details.category, "version": details.version_label,
                     "effective_date": details.effective_date.isoformat() if details.effective_date else None}
        chunks = chunk_text(extracted.markdown, source_file, overrides)
        if not chunks:
            raise UploadRejected("No sections with text were found in this file.")

        with self.engine.connect() as connection:
            other = connection.execute(text(
                "SELECT source_file FROM document_versions WHERE tenant_id = :tenant_id AND collection_name = :collection "
                "AND text_hash = :hash AND source_file <> :file AND status = 'current' LIMIT 1"
            ), {**self._scope, "hash": text_hash(extracted.markdown), "file": source_file}).scalar()
            active = connection.execute(text(
                "SELECT id FROM indexing_jobs WHERE tenant_id = :tenant_id AND collection_name = :collection "
                "AND source_file = :file AND status IN ('queued', 'running') LIMIT 1"
            ), {**self._scope, "file": source_file}).scalar()
        if other:
            raise DuplicateDocument(f"The same text is already in the knowledge base as {other}.")
        if active:
            raise JobConflict("This policy is already being indexed. Please wait for that to finish.")

        metadata = dict(chunks[0].metadata)
        metadata["content_hash"] = self._version_hash(extracted.markdown, metadata)
        self._ensure_baseline(source_file)
        with self.engine.connect() as connection:
            latest = connection.execute(text(
                "SELECT content_hash FROM document_versions WHERE tenant_id = :tenant_id AND collection_name = :collection "
                "AND source_file = :file AND status = 'current'"
            ), {**self._scope, "file": source_file}).scalar()
        if latest == metadata["content_hash"]:
            return {"file": source_file, "status": "unchanged", "job_id": None, "version": self.current_number(source_file)}

        job_id = uuid.uuid4().hex
        with self.engine.begin() as connection:
            connection.execute(text(
                "UPDATE document_versions SET status = 'abandoned' WHERE tenant_id = :tenant_id "
                "AND collection_name = :collection AND source_file = :file AND status = 'pending'"
            ), {**self._scope, "file": source_file})
            version_id, number = self._insert_version(connection, source_file, metadata, file_name, extracted.markdown,
                                                      "pending", details.uploaded_by)
            connection.execute(text(
                "INSERT INTO indexing_jobs (id, tenant_id, collection_name, source_file, version_id, status, created_by) "
                "VALUES (:id, :tenant_id, :collection, :file, :version_id, 'queued', :by)"
            ), {**self._scope, "id": job_id, "file": source_file, "version_id": version_id, "by": details.uploaded_by})
        # Search skips very short sections as lead-ins, so say so rather than let them vanish quietly
        too_short = [c.metadata["section"] for c in chunks if len(body_of(c)) < config.MIN_BODY_CHARS]
        return {"file": source_file, "status": "queued", "job_id": job_id, "version": number, "too_short": too_short}

    @staticmethod
    def _version_hash(content: str, metadata: dict) -> str:
        details = {key: metadata.get(key) for key in ("policy", "category", "version", "effective_date")}
        return hashlib.sha256(f"{json.dumps(details, sort_keys=True)}\n{content}".encode()).hexdigest()

    def archive_before_change(self, plan: SyncPlan, stored: dict[str, Document]) -> None:
        """Keep a superseded copy of each chunk about to change or go, with its embedding."""
        leaving: dict[str, list[str]] = {}
        for chunk in plan.edited:
            leaving.setdefault(chunk.metadata["source"], []).append(str(chunk.id))
        for chunk_id in plan.stale_ids:
            leaving.setdefault(stored[chunk_id].metadata["source"], []).append(chunk_id)
        for source_file, ids in leaving.items():
            self.index.archive_chunks(ids, self.current_number(source_file) or 1, datetime.now(UTC).date().isoformat())

    def _update_job(self, job_id: str, **fields) -> None:
        assignments = ", ".join(f"{name} = :{name}" for name in fields)
        with self.engine.begin() as connection:
            connection.execute(text(
                f"UPDATE indexing_jobs SET {assignments}, updated_at = now() WHERE id = :id AND tenant_id = :tenant_id"
            ), {"id": job_id, "tenant_id": self.tenant_id, **fields})

    def run_job(self, job_id: str, wait_seconds: int | None = None) -> None:
        """Index one version. On failure the job is marked failed, and a retry carries on from there."""
        with self.engine.begin() as connection:
            job = connection.execute(text(
                "UPDATE indexing_jobs SET status = 'running', attempts = attempts + 1, error = NULL, updated_at = now() "
                "WHERE id = :id AND tenant_id = :tenant_id AND status IN ('queued', 'failed') RETURNING version_id"
            ), {"id": job_id, "tenant_id": self.tenant_id}).first()
            if not job:
                return
            version = connection.execute(text(
                "SELECT * FROM document_versions WHERE id = :id"
            ), {"id": job.version_id}).one()
        try:
            chunks = chunk_text(version.content, version.source_file, overrides_of(version))
            stored = self.index.stored_chunks()
            plan = plan_sync(stored, chunks, {version.source_file})
            self._update_job(job_id, to_embed=len(plan.to_embed))
            self.archive_before_change(plan, stored)

            def progress(done: int) -> None:
                self._update_job(job_id, embedded=done)

            wait = config.EMBEDDING_WAIT_SECONDS if wait_seconds is None else wait_seconds
            run_sync(self.index, plan, wait, progress)
            self._finish_version(version, chunks)
        except Exception as error:
            # Only the kind of failure is kept: its text could contain anything
            logger.exception("Indexing job %s failed", job_id)
            self._update_job(job_id, status="failed", error=f"{type(error).__name__}: indexing stopped. Retry to continue.")
            return
        self._update_job(job_id, status="succeeded")

    def _finish_version(self, version, chunks: list[Document]) -> None:
        from app.rag.store import DocumentRecord

        details = chunks[0].metadata
        self.index.save_document(DocumentRecord(
            source_file=version.source_file, title=details["policy"], category=details["category"],
            version=details["version"], effective_date=details["effective_date"],
            content_hash=content_hash(version.content, details["category"]), chunk_count=len(chunks),
        ))
        with self.engine.begin() as connection:
            connection.execute(text(
                "UPDATE document_versions SET status = 'superseded', superseded_at = now() WHERE tenant_id = :tenant_id "
                "AND collection_name = :collection AND source_file = :file AND status = 'current'"
            ), {**self._scope, "file": version.source_file})
            connection.execute(text("UPDATE document_versions SET status = 'current' WHERE id = :id"), {"id": version.id})
        try:
            (Path(self.policy_dir) / version.source_file).write_text(version.content, encoding="utf-8")
        except OSError:
            logger.exception("Could not save a copy of %s", version.source_file)

    def retry_job(self, job_id: str) -> None:
        """Check that a failed job can run again. The caller then runs it."""
        with self.engine.connect() as connection:
            job = connection.execute(text(
                "SELECT j.status, v.status AS version_status FROM indexing_jobs j "
                "JOIN document_versions v ON v.id = j.version_id WHERE j.id = :id AND j.tenant_id = :tenant_id"
            ), {"id": job_id, "tenant_id": self.tenant_id}).first()
        if not job:
            raise NotFound(job_id)
        if job.status != "failed":
            raise JobConflict("Only a failed job can be retried.")
        if job.version_status != "pending":
            raise JobConflict("A newer upload has replaced this one, so it can't be retried.")

    def fail_interrupted(self) -> int:
        """Mark jobs cut off by a restart as failed, so they can be retried from the admin page."""
        with self.engine.begin() as connection:
            return connection.execute(text(
                "UPDATE indexing_jobs SET status = 'failed', error = 'The server restarted. Retry to continue.', "
                "updated_at = now() WHERE tenant_id = :tenant_id AND status IN ('queued', 'running')"
            ), {"tenant_id": self.tenant_id}).rowcount

    def remove(self, source_file: str, purge: bool = False) -> dict:
        """Stop answering from a policy. Old versions stay for history unless purge is set."""
        stored = self.index.stored_chunks()
        plan = plan_sync(stored, [], {source_file})
        with self.engine.connect() as connection:
            known = connection.execute(text(
                "SELECT 1 FROM document_versions WHERE tenant_id = :tenant_id AND collection_name = :collection "
                "AND source_file = :file LIMIT 1"
            ), {**self._scope, "file": source_file}).first()
        if not plan.stale_ids and not known:
            raise NotFound(source_file)
        self.archive_before_change(plan, stored)
        run_sync(self.index, plan)
        self.index.delete_document(source_file)
        with self.engine.begin() as connection:
            connection.execute(text(
                "UPDATE document_versions SET status = 'removed', superseded_at = now() WHERE tenant_id = :tenant_id "
                "AND collection_name = :collection AND source_file = :file AND status IN ('current', 'pending')"
            ), {**self._scope, "file": source_file})
            if purge:
                connection.execute(text(
                    "DELETE FROM document_versions WHERE tenant_id = :tenant_id AND collection_name = :collection "
                    "AND source_file = :file"
                ), {**self._scope, "file": source_file})
        if purge:
            self.index.delete_archived(source_file)
        (Path(self.policy_dir) / source_file).unlink(missing_ok=True)
        return {"file": source_file, "removed": len(plan.stale_ids), "purged": purge}

    def documents(self) -> list[dict]:
        """List current policies with their latest details."""
        counts: dict[str, int] = {}
        titles: dict[str, str] = {}
        for chunk in self.index.stored_chunks().values():
            counts[chunk.metadata["source"]] = counts.get(chunk.metadata["source"], 0) + 1
            titles[chunk.metadata["source"]] = chunk.metadata["policy"]
        with self.engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT source_file, version_number, category, version_label, effective_date, uploaded_by "
                "FROM document_versions WHERE tenant_id = :tenant_id AND collection_name = :collection "
                "AND status = 'current'"
            ), self._scope).all()
        versions = {row.source_file: row for row in rows}
        result = []
        for file, chunks in sorted(counts.items()):
            row = versions.get(file)
            result.append({
                "file": file, "policy": titles[file], "chunks": chunks,
                "version": row.version_number if row else None, "category": row.category if row else None,
                "version_label": row.version_label if row else None,
                "effective_date": row.effective_date.isoformat() if row and row.effective_date else None,
                "uploaded_by": row.uploaded_by if row else None,
            })
        return result

    def versions(self, source_file: str) -> list[dict]:
        with self.engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT version_number, title, category, version_label, effective_date, original_name, status, "
                "uploaded_by, created_at, superseded_at FROM document_versions WHERE tenant_id = :tenant_id "
                "AND collection_name = :collection AND source_file = :file ORDER BY version_number DESC"
            ), {**self._scope, "file": source_file}).all()
        if not rows:
            raise NotFound(source_file)
        return [{"version": row.version_number, "title": row.title, "category": row.category,
                 "version_label": row.version_label,
                 "effective_date": row.effective_date.isoformat() if row.effective_date else None,
                 "original_name": row.original_name, "status": row.status, "uploaded_by": row.uploaded_by,
                 "created_at": row.created_at.isoformat(),
                 "superseded_at": row.superseded_at.isoformat() if row.superseded_at else None} for row in rows]

    def jobs(self, limit: int = 50) -> list[dict]:
        with self.engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT j.id, j.source_file, j.status, j.to_embed, j.embedded, j.attempts, j.error, j.created_by, "
                "j.created_at, j.updated_at, v.version_number, v.status AS version_status FROM indexing_jobs j "
                "JOIN document_versions v ON v.id = j.version_id WHERE j.tenant_id = :tenant_id "
                "ORDER BY j.created_at DESC LIMIT :limit"
            ), {"tenant_id": self.tenant_id, "limit": limit}).all()
        return [{"id": row.id, "file": row.source_file, "version": row.version_number, "status": row.status,
                 "to_embed": row.to_embed, "embedded": row.embedded, "attempts": row.attempts, "error": row.error,
                 "created_by": row.created_by, "created_at": row.created_at.isoformat(),
                 "updated_at": row.updated_at.isoformat(),
                 "can_retry": row.status == "failed" and row.version_status == "pending"} for row in rows]

    def job(self, job_id: str) -> dict:
        for row in self.jobs(500):
            if row["id"] == job_id:
                return row
        raise NotFound(job_id)

    def record_unanswered(self, question: str, reason: str) -> None:
        """Note a question the bot couldn't answer. No user is stored, so HR sees the gap, not the person."""
        with self.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO unanswered_questions (tenant_id, question, reason) VALUES (:tenant_id, :question, :reason)"
            ), {"tenant_id": self.tenant_id, "question": question[:config.MAX_QUESTION_CHARS], "reason": reason})

    def unanswered_report(self, limit: int = 50) -> list[dict]:
        with self.engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT lower(trim(question)) AS question, count(*) AS asked, max(created_at) AS last_asked "
                "FROM unanswered_questions WHERE tenant_id = :tenant_id GROUP BY lower(trim(question)) "
                "ORDER BY asked DESC, last_asked DESC LIMIT :limit"
            ), {"tenant_id": self.tenant_id, "limit": limit}).all()
        return [{"question": row.question, "asked": row.asked, "last_asked": row.last_asked.isoformat()} for row in rows]

    def purge_unanswered_older_than(self, days: int) -> None:
        with self.engine.begin() as connection:
            connection.execute(text(
                "DELETE FROM unanswered_questions WHERE tenant_id = :tenant_id "
                "AND created_at < now() - make_interval(days => :days)"
            ), {"tenant_id": self.tenant_id, "days": days})

    def save_feedback(self, user_id: str, message_id: int, rating: int, comment: str) -> None:
        """Store one rating per answer per person. Only the person who got the answer can rate it."""
        with self.engine.begin() as connection:
            owned = connection.execute(text(
                "SELECT 1 FROM messages WHERE id = :id AND tenant_id = :tenant_id AND user_id = :user_id "
                "AND role = 'assistant'"
            ), {"id": message_id, "tenant_id": self.tenant_id, "user_id": user_id}).first()
            if not owned:
                raise NotFound(str(message_id))
            connection.execute(text(
                "INSERT INTO feedback (tenant_id, user_id, message_id, rating, comment) "
                "VALUES (:tenant_id, :user_id, :message_id, :rating, :comment) "
                "ON CONFLICT (tenant_id, user_id, message_id) DO UPDATE SET rating = EXCLUDED.rating, "
                "comment = EXCLUDED.comment"
            ), {"tenant_id": self.tenant_id, "user_id": user_id, "message_id": message_id, "rating": rating,
                "comment": comment})

    def feedback_summary(self, limit: int = 20) -> dict:
        with self.engine.connect() as connection:
            totals = connection.execute(text(
                "SELECT count(*) FILTER (WHERE rating > 0) AS up, count(*) FILTER (WHERE rating < 0) AS down "
                "FROM feedback WHERE tenant_id = :tenant_id"
            ), {"tenant_id": self.tenant_id}).one()
            rows = connection.execute(text(
                "SELECT m.content AS answer, f.comment, f.created_at FROM feedback f JOIN messages m ON m.id = f.message_id "
                "WHERE f.tenant_id = :tenant_id AND f.rating < 0 ORDER BY f.created_at DESC LIMIT :limit"
            ), {"tenant_id": self.tenant_id, "limit": limit}).all()
        return {"thumbs_up": totals.up, "thumbs_down": totals.down,
                "recent_negative": [{"answer": row.answer, "comment": row.comment,
                                     "created_at": row.created_at.isoformat()} for row in rows]}
