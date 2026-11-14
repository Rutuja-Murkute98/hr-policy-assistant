"""Apply a new embedding model to an organisation's chunks without stopping search. See docs/DECISIONS.md, D54.

While the job runs, searches use the old collection and the old model. Every chunk, current and superseded, is embedded
again into reindex_vectors. When all have a new vector, one transaction moves them to the new collection, with their
document records, versions and jobs, and records the new model. A failed job keeps what it embedded, so a retry
carries on.
"""
import hashlib
import logging
import time
from dataclasses import dataclass

from langchain_core.documents import Document
from sqlalchemy import Engine, text

from app import config
from app.rag.store import PolicyIndex
from app.tenancy import TenantEngine, tenant_scoped

logger = logging.getLogger("hr_helpdesk")

BATCH_SIZE = 50
STALE_MINUTES = 15  # a running job not heard from for this long was in a process that stopped

# The organisation's newest other collection, if the target has neither a recorded model nor any of its chunks
FIND_OLD = text(
    "SELECT m.collection_name, m.provider, m.model FROM embedding_models m WHERE m.tenant_id = :tenant_id "
    "AND m.collection_name <> :target AND NOT EXISTS (SELECT 1 FROM embedding_models n "
    "WHERE n.tenant_id = :tenant_id AND n.collection_name = :target) AND NOT EXISTS (SELECT 1 "
    "FROM langchain_pg_embedding e JOIN langchain_pg_collection c ON e.collection_id = c.uuid WHERE c.name = :target) "
    "ORDER BY m.created_at DESC LIMIT 1"
)
CLAIM = text(
    "INSERT INTO reindex_runs (tenant_id, to_collection, from_collection, from_model, to_model) "
    "VALUES (:tenant_id, :to_collection, :from_collection, :from_model, :to_model) "
    "ON CONFLICT (tenant_id, to_collection) DO UPDATE SET status = 'running', error = NULL, finished_at = NULL, "
    "from_collection = EXCLUDED.from_collection, from_model = EXCLUDED.from_model, updated_at = now(), "
    # A finished run is from an earlier change to this same model, so this is a new run
    "attempts = CASE WHEN reindex_runs.status = 'succeeded' THEN 1 ELSE reindex_runs.attempts + 1 END, "
    "created_at = CASE WHEN reindex_runs.status = 'succeeded' THEN now() ELSE reindex_runs.created_at END "
    "WHERE reindex_runs.status IN ('queued', 'succeeded') OR (reindex_runs.status = 'running' "
    f"AND reindex_runs.updated_at < now() - interval '{STALE_MINUTES} minutes') RETURNING status"
)
STAGE = text(
    "INSERT INTO reindex_vectors (tenant_id, to_collection, chunk_id, text_hash, embedding) "
    "VALUES (:tenant_id, :to_collection, :chunk_id, :text_hash, CAST(:embedding AS vector)) "
    "ON CONFLICT (tenant_id, to_collection, chunk_id) DO UPDATE SET text_hash = EXCLUDED.text_hash, "
    "embedding = EXCLUDED.embedding"
)


class ChunksChanged(Exception):
    """A chunk was added or changed while switching, so the switch is undone and tried again."""


def text_hash(chunk: Document) -> str:
    return hashlib.sha256(chunk.page_content.encode()).hexdigest()


@dataclass
class OldIndex:
    collection: str
    provider: str
    model: str


def find_old_index(engine: Engine | TenantEngine, tenant_id: str, target_collection: str) -> OldIndex | None:
    """The collection an organisation's chunks are still in, if they haven't been moved to the target yet."""
    with tenant_scoped(engine, tenant_id).connect() as connection:
        row = connection.execute(FIND_OLD, {"tenant_id": tenant_id, "target": target_collection}).first()
    return OldIndex(row.collection_name, row.provider, row.model) if row else None


def latest_run(engine: Engine | TenantEngine, tenant_id: str) -> dict | None:
    with tenant_scoped(engine, tenant_id).connect() as connection:
        row = connection.execute(text(
            "SELECT from_model, to_model, status, total, embedded, attempts, error, created_at, finished_at "
            "FROM reindex_runs WHERE tenant_id = :tenant_id ORDER BY created_at DESC LIMIT 1"
        ), {"tenant_id": tenant_id}).first()
    if not row:
        return None
    return {"from_model": row.from_model, "to_model": row.to_model, "status": row.status, "total": row.total,
            "embedded": row.embedded, "attempts": row.attempts, "error": row.error,
            "started_at": row.created_at.isoformat(),
            "finished_at": row.finished_at.isoformat() if row.finished_at else None}


def queue_retry(engine: Engine | TenantEngine, tenant_id: str) -> bool:
    """Let a failed re-index run again. Returns False if there's none to retry."""
    with tenant_scoped(engine, tenant_id).begin() as connection:
        return connection.execute(text(
            "UPDATE reindex_runs SET status = 'queued', updated_at = now() WHERE tenant_id = :tenant_id "
            "AND status = 'failed'"
        ), {"tenant_id": tenant_id}).rowcount > 0


class Reindex:
    """Re-embed one organisation's chunks from old_index into new_store's collection, then switch."""

    def __init__(self, engine: Engine | TenantEngine, tenant_id: str, old_index: PolicyIndex, new_store,
                 new_model: tuple[str, str] | None = None, wait_seconds: float | None = None):
        self.engine = tenant_scoped(engine, tenant_id)
        self.tenant_id = tenant_id
        self.old = old_index
        self.new_store = new_store
        self.new_model = new_model or (config.EMBEDDING_PROVIDER, config.EMBEDDING_MODEL)
        self.wait_seconds = config.EMBEDDING_WAIT_SECONDS if wait_seconds is None else wait_seconds
        self.scope = {"tenant_id": tenant_id, "to_collection": new_store.collection_name}

    def claim(self) -> bool:
        """Start the job here, unless it's running in another process, has failed, or has finished."""
        with self.engine.begin() as connection:
            return connection.execute(CLAIM, {
                **self.scope, "from_collection": self.old.store.collection_name,
                "from_model": "/".join(self.old.model), "to_model": "/".join(self.new_model),
            }).first() is not None

    def _update(self, **fields) -> None:
        assignments = ", ".join(f"{name} = :{name}" for name in fields)
        with self.engine.begin() as connection:
            connection.execute(text(
                f"UPDATE reindex_runs SET {assignments}, updated_at = now() "
                "WHERE tenant_id = :tenant_id AND to_collection = :to_collection"
            ), {**self.scope, **fields})

    def _staged(self) -> dict[str, str]:
        """Each staged chunk's id, and a hash of the text its vector was made from."""
        with self.engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT chunk_id, text_hash FROM reindex_vectors WHERE tenant_id = :tenant_id "
                "AND to_collection = :to_collection"
            ), self.scope).all()
        return {row.chunk_id: row.text_hash for row in rows}

    def _stage(self, chunks: list[Document]) -> None:
        vectors = self.new_store.embeddings.embed_documents([chunk.page_content for chunk in chunks])
        with self.engine.begin() as connection:
            connection.execute(STAGE, [{**self.scope, "chunk_id": chunk.id, "text_hash": text_hash(chunk),
                                        "embedding": str([float(value) for value in vector])}
                                       for chunk, vector in zip(chunks, vectors, strict=True)])

    def run(self) -> bool:
        """Embed what's missing, then switch. Returns True once the organisation searches the new collection."""
        try:
            for _ in range(3):  # an upload while switching adds chunks, so look again
                chunks = self.old.all_chunks()
                staged = self._staged()
                # A chunk whose text changed since it was staged needs a new vector too
                missing = [chunk for chunk_id, chunk in chunks.items() if staged.get(chunk_id) != text_hash(chunk)]
                self._update(total=len(chunks), embedded=len(chunks) - len(missing))
                for start in range(0, len(missing), BATCH_SIZE):
                    if start:
                        time.sleep(self.wait_seconds)  # the free tier limits embedding calls per minute
                    self._stage(missing[start:start + BATCH_SIZE])
                    self._update(embedded=len(chunks) - len(missing) + min(start + BATCH_SIZE, len(missing)))
                try:
                    self._switch()
                    return True
                except ChunksChanged:
                    continue
            raise RuntimeError("chunks kept changing during the switch")
        except Exception as error:
            # Only the kind of failure is kept: its text could contain anything
            logger.exception("Re-indexing for %s failed", self.tenant_id)
            self._update(status="failed", error=f"{type(error).__name__}: re-indexing stopped. Retry to continue.")
            return False

    def _switch(self) -> None:
        """Move every chunk to the new collection with its new vector, in one transaction, or nothing."""
        old_collection, new_collection = self.old.store.collection_name, self.new_store.collection_name
        names = {**self.scope, "old": old_collection, "new": new_collection}
        with self.engine.begin() as connection:
            connection.execute(text(
                "SELECT 1 FROM reindex_runs WHERE tenant_id = :tenant_id AND to_collection = :to_collection FOR UPDATE"
            ), self.scope)
            connection.execute(text(
                "UPDATE langchain_pg_embedding e SET embedding = r.embedding, "
                "collection_id = (SELECT uuid FROM langchain_pg_collection WHERE name = :new) "
                "FROM reindex_vectors r, langchain_pg_collection c WHERE r.tenant_id = :tenant_id "
                "AND r.to_collection = :to_collection AND r.chunk_id = e.id AND e.collection_id = c.uuid "
                "AND c.name = :old AND r.text_hash = encode(sha256(convert_to(e.document, 'UTF8')), 'hex')"
            ), names)
            # Row-Level Security limits this to the organisation's own chunks
            left = connection.execute(text(
                "SELECT count(*) FROM langchain_pg_embedding e JOIN langchain_pg_collection c "
                "ON e.collection_id = c.uuid WHERE c.name = :old"
            ), names).scalar()
            if left:
                raise ChunksChanged()  # leaving the block undoes the moves above
            for table in ("policy_documents", "document_versions", "indexing_jobs"):
                connection.execute(text(f"UPDATE {table} SET collection_name = :new "
                                        "WHERE tenant_id = :tenant_id AND collection_name = :old"), names)
            dimension = connection.execute(text(
                "SELECT vector_dims(embedding) FROM reindex_vectors WHERE tenant_id = :tenant_id "
                "AND to_collection = :to_collection LIMIT 1"
            ), self.scope).scalar()
            connection.execute(text("DELETE FROM embedding_models WHERE tenant_id = :tenant_id "
                                    "AND collection_name = :old"), names)
            if dimension:
                connection.execute(text(
                    "INSERT INTO embedding_models (tenant_id, collection_name, provider, model, dimension) "
                    "VALUES (:tenant_id, :new, :provider, :model, :dimension) ON CONFLICT DO NOTHING"
                ), {**names, "provider": self.new_model[0], "model": self.new_model[1], "dimension": dimension})
            connection.execute(text("DELETE FROM reindex_vectors WHERE tenant_id = :tenant_id "
                                    "AND to_collection = :to_collection"), self.scope)
            connection.execute(text(
                "UPDATE reindex_runs SET status = 'succeeded', error = NULL, finished_at = now(), updated_at = now() "
                "WHERE tenant_id = :tenant_id AND to_collection = :to_collection"
            ), self.scope)
