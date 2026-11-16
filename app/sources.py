"""An organisation's sources of policies, and syncing them through the same path as an upload. See D59.

A sync lists the source's files, fetches only new or changed ones, and gives each to the knowledge base as an upload, so
extraction, versions, indexing and only embedding changed sections all work as usual. A file gone from the source stops
being answered from, unless the source keeps removed files.
"""
import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import Engine, text

from app import config
from app.connectors.base import Connector, ConnectorError, changes, policy_name
from app.connectors.folder import FolderConnector, organisation_folder
from app.connectors.google_drive import GoogleDriveConnector
from app.connectors.sharepoint import SharePointConnector
from app.knowledge import DuplicateDocument, JobConflict, KnowledgeBase, NotFound, UploadDetails, UploadRejected
from app.policy_checks import after_indexing
from app.tenancy import TenantEngine, tenant_scoped
from pipeline.extract import ExtractionError

logger = logging.getLogger("hr_helpdesk")

Kind = Literal["folder", "google_drive", "sharepoint"]
DRIVE_ID = re.compile(r"[A-Za-z0-9_-]{10,100}")
GRAPH_ID = re.compile(r"[A-Za-z0-9.,_!-]{1,200}")  # site and drive ids are letters, numbers and these marks
RUNNING_MINUTES = 30  # a sync still "running" after this was cut off, such as by a restart
MAX_FILES = 200  # files synced from one source at a time


class SourceSettings(BaseModel):
    """Where a source's files are. Which fields are needed depends on its kind."""

    path: str = Field(default="", max_length=200)  # folder: inside the organisation's sources folder
    folder_id: str = Field(default="", max_length=100)  # Google Drive
    site_id: str = Field(default="", max_length=200)  # SharePoint
    drive_id: str = Field(default="", max_length=200)
    folder: str = Field(default="", max_length=300)


class NewSource(BaseModel):
    kind: Kind
    name: str = Field(min_length=1, max_length=100)
    settings: SourceSettings = Field(default_factory=SourceSettings)
    every_minutes: int = Field(default=60, ge=15, le=7 * 24 * 60)
    keep_removed: bool = False

    @field_validator("name")
    @classmethod
    def tidy(cls, name: str) -> str:
        return " ".join(name.split())


class SourceChange(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    every_minutes: int | None = Field(default=None, ge=15, le=7 * 24 * 60)
    keep_removed: bool | None = None
    enabled: bool | None = None


def available_kinds() -> list[str]:
    """The kinds of source this deployment can read, from the settings its owner filled in."""
    kinds = []
    if config.SOURCES_ROOT:
        kinds.append("folder")
    if config.GOOGLE_DRIVE_CREDENTIALS_FILE:
        kinds.append("google_drive")
    if config.SHAREPOINT_TENANT_ID and config.SHAREPOINT_CLIENT_ID and config.SHAREPOINT_CLIENT_SECRET:
        kinds.append("sharepoint")
    return kinds


def check_settings(kind: str, settings: SourceSettings, tenant_id: str) -> SourceSettings:
    """Refuse a source this deployment can't read, or one that names somewhere it shouldn't."""
    if kind not in available_kinds():
        raise ConnectorError(f"This server isn't set up to read {kind.replace('_', ' ')} sources. "
                             "Ask its owner, or choose another kind.")
    if kind == "folder":
        organisation_folder(Path(config.SOURCES_ROOT), tenant_id, settings.path)
        return SourceSettings(path=settings.path.strip().strip("/\\"))
    if kind == "google_drive":
        if not DRIVE_ID.fullmatch(settings.folder_id):
            raise ConnectorError("Paste the folder's id from its Drive address, the part after /folders/.")
        return SourceSettings(folder_id=settings.folder_id)
    if not (GRAPH_ID.fullmatch(settings.site_id) and GRAPH_ID.fullmatch(settings.drive_id)):
        raise ConnectorError("Give the SharePoint site id and document library (drive) id.")
    if ".." in settings.folder.split("/"):
        raise ConnectorError("The folder can't go up a level.")
    return SourceSettings(site_id=settings.site_id, drive_id=settings.drive_id, folder=settings.folder.strip("/"))


def connector_for(kind: str, settings: dict, tenant_id: str) -> Connector:
    chosen = SourceSettings.model_validate(settings)
    if kind == "folder":
        return FolderConnector(organisation_folder(Path(config.SOURCES_ROOT), tenant_id, chosen.path))
    if kind == "google_drive":
        credentials = json.loads(Path(config.GOOGLE_DRIVE_CREDENTIALS_FILE).read_text(encoding="utf-8"))
        return GoogleDriveConnector(chosen.folder_id, credentials)
    return SharePointConnector(config.SHAREPOINT_TENANT_ID, config.SHAREPOINT_CLIENT_ID,
                               config.SHAREPOINT_CLIENT_SECRET, chosen.site_id, chosen.drive_id, chosen.folder)


def view(row) -> dict:
    return {"id": row.id, "kind": row.kind, "name": row.name, "settings": row.settings,
            "every_minutes": row.every_minutes, "keep_removed": row.keep_removed, "enabled": row.enabled,
            "status": row.status, "last_error": row.last_error, "last_summary": row.last_summary,
            "last_synced_at": row.last_synced_at.isoformat() if row.last_synced_at else None}


class SourceStore:
    """One organisation's sources. Row-Level Security keeps each organisation's apart."""

    def __init__(self, engine: Engine | TenantEngine, tenant_id: str):
        self.engine = tenant_scoped(engine, tenant_id)
        self.tenant_id = tenant_id

    def add(self, source: NewSource, actor: str) -> dict:
        settings = check_settings(source.kind, source.settings, self.tenant_id)
        source_id = uuid.uuid4().hex
        with self.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO knowledge_sources (id, tenant_id, kind, name, settings, every_minutes, keep_removed, "
                "created_by) VALUES (:id, :tenant_id, :kind, :name, CAST(:settings AS jsonb), :every, :keep, :actor)"
            ), {"id": source_id, "tenant_id": self.tenant_id, "kind": source.kind, "name": source.name,
                "settings": settings.model_dump_json(exclude_defaults=True), "every": source.every_minutes,
                "keep": source.keep_removed, "actor": actor})
        return self.get(source_id)

    def get(self, source_id: str) -> dict:
        with self.engine.connect() as connection:
            row = connection.execute(text(
                "SELECT * FROM knowledge_sources WHERE id = :id AND tenant_id = :tenant_id"
            ), {"id": source_id, "tenant_id": self.tenant_id}).first()
        if row is None:
            raise NotFound(source_id)
        return view(row)

    def all(self) -> list[dict]:
        with self.engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT * FROM knowledge_sources WHERE tenant_id = :tenant_id ORDER BY created_at"
            ), {"tenant_id": self.tenant_id}).all()
        return [view(row) for row in rows]

    def change(self, source_id: str, change: SourceChange) -> dict:
        fields = change.model_dump(exclude_none=True)
        if fields:
            assignments = ", ".join(f"{name} = :{name}" for name in fields)
            with self.engine.begin() as connection:
                found = connection.execute(text(
                    f"UPDATE knowledge_sources SET {assignments}, updated_at = now() "
                    "WHERE id = :id AND tenant_id = :tenant_id"
                ), {**fields, "id": source_id, "tenant_id": self.tenant_id}).rowcount
            if not found:
                raise NotFound(source_id)
        return self.get(source_id)

    def remove(self, source_id: str) -> None:
        """Forget a source. Its policies stay, as HR may still want them: they can be removed one by one."""
        with self.engine.begin() as connection:
            if not connection.execute(text("DELETE FROM knowledge_sources WHERE id = :id AND tenant_id = :tenant_id"),
                                      {"id": source_id, "tenant_id": self.tenant_id}).rowcount:
                raise NotFound(source_id)

    def files(self, source_id: str) -> list[dict]:
        with self.engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT name, policy_file, synced_at FROM source_files WHERE tenant_id = :tenant_id "
                "AND source_id = :source_id ORDER BY name"
            ), {"tenant_id": self.tenant_id, "source_id": source_id}).all()
        return [{"name": row.name, "policy_file": row.policy_file, "synced_at": row.synced_at.isoformat()}
                for row in rows]

    def due(self, now: datetime | None = None) -> list[dict]:
        """Enabled sources whose time has come, oldest first."""
        now = now or datetime.now(UTC)
        return [source for source in self.all() if source["enabled"] and source["status"] != "running"
                and (source["last_synced_at"] is None or datetime.fromisoformat(source["last_synced_at"])
                     + timedelta(minutes=source["every_minutes"]) <= now)]

    def claim(self, source_id: str) -> bool:
        """Mark a sync as started, unless one is running already. A run cut off by a restart can be claimed again."""
        with self.engine.begin() as connection:
            return connection.execute(text(
                "UPDATE knowledge_sources SET status = 'running', updated_at = now() WHERE id = :id "
                "AND tenant_id = :tenant_id AND (status <> 'running' "
                f"OR updated_at < now() - interval '{RUNNING_MINUTES} minutes')"
            ), {"id": source_id, "tenant_id": self.tenant_id}).rowcount > 0

    def known(self, source_id: str) -> dict[str, tuple[str, str]]:
        with self.engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT file_key, version_tag, policy_file FROM source_files WHERE tenant_id = :tenant_id "
                "AND source_id = :source_id"
            ), {"tenant_id": self.tenant_id, "source_id": source_id}).all()
        return {row.file_key: (row.version_tag, row.policy_file) for row in rows}

    def remember(self, source_id: str, key: str, name: str, version: str, policy_file: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO source_files (tenant_id, source_id, file_key, name, version_tag, policy_file) "
                "VALUES (:tenant_id, :source_id, :key, :name, :version, :policy) "
                "ON CONFLICT (tenant_id, source_id, file_key) DO UPDATE SET name = EXCLUDED.name, "
                "version_tag = EXCLUDED.version_tag, policy_file = EXCLUDED.policy_file, synced_at = now()"
            ), {"tenant_id": self.tenant_id, "source_id": source_id, "key": key, "name": name, "version": version,
                "policy": policy_file})

    def forget(self, source_id: str, key: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(text(
                "DELETE FROM source_files WHERE tenant_id = :tenant_id AND source_id = :source_id AND file_key = :key"
            ), {"tenant_id": self.tenant_id, "source_id": source_id, "key": key})

    def finish(self, source_id: str, summary: dict | None, error: str | None = None) -> None:
        with self.engine.begin() as connection:
            connection.execute(text(
                "UPDATE knowledge_sources SET status = :status, last_error = :error, "
                "last_summary = CAST(:summary AS jsonb), last_synced_at = now(), updated_at = now() "
                "WHERE id = :id AND tenant_id = :tenant_id"
            ), {"status": "failed" if error else "ok", "error": error, "summary": json.dumps(summary),
                "id": source_id, "tenant_id": self.tenant_id})


def sync_source(store: SourceStore, knowledge: KnowledgeBase, source: dict, connector: Connector | None = None,
                llm=None) -> dict:
    """Bring one source's policies up to date. Returns what happened, file by file in counts and failures.

    With a model, each new or changed policy then gets the checks that follow an upload (D60).
    """
    summary: dict = {"added": 0, "updated": 0, "unchanged": 0, "removed": 0, "failed": []}
    try:
        connector = connector or connector_for(source["kind"], source["settings"], store.tenant_id)
        files = connector.list_files()[:MAX_FILES]
        known = store.known(source["id"])
        changed, gone = changes(files, {key: version for key, (version, _) in known.items()})
        summary["unchanged"] = len(files) - len(changed)
        for file in changed:
            name = policy_name(file.name)
            try:
                result = knowledge.add_upload(connector.fetch(file), name,
                                              UploadDetails(uploaded_by=f"sync: {source['name']}"))
            except (ConnectorError, ExtractionError, UploadRejected, DuplicateDocument, JobConflict) as error:
                summary["failed"].append({"file": file.name, "reason": str(error)})
                continue
            if result["job_id"]:
                knowledge.run_job(result["job_id"])
                if llm is not None:
                    after_indexing(knowledge, llm, result["file"])
            store.remember(source["id"], file.key, file.name, file.version, result["file"])
            if result["status"] == "unchanged":  # touched at the source, but its text is the same
                summary["unchanged"] += 1
            else:
                summary["added" if file.key not in known else "updated"] += 1
        for key in gone:
            if not source["keep_removed"]:
                try:
                    knowledge.remove(known[key][1])
                    summary["removed"] += 1
                except NotFound:
                    pass  # already removed by hand
            store.forget(source["id"], key)
    except ConnectorError as error:
        store.finish(source["id"], summary, str(error))
        return summary
    except Exception:
        logger.exception("Syncing source %s failed", source["id"])
        store.finish(source["id"], summary, "The sync stopped unexpectedly. It will try again at its next time.")
        return summary
    store.finish(source["id"], summary)
    return summary
