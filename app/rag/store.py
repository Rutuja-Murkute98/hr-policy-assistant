"""Connect to the vector database. Used by both the indexing script and the API.

With DATABASE_URL set, this uses that hosted database (such as Neon).
Without it, PostgreSQL runs inside Python and keeps its data in the pgdata folder.
"""
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import psycopg
from alembic import command
from alembic.config import Config
from langchain_core.documents import Document
from langchain_postgres import PGVector
from sqlalchemy import text

from app import config
from app.rag.providers import get_embeddings
from app.rag.retrieval import Candidate, SearchFilters, evidence_scores
from app.tenancy import check_tenant_id, enter_tenant, secure_library_tables

CHUNK_JOIN = (
    "FROM langchain_pg_embedding e JOIN langchain_pg_collection c ON e.collection_id = c.uuid "
    "WHERE c.name = :collection AND COALESCE(e.cmetadata->>'tenant_id', :default_tenant) = :tenant_id "
)
CHUNK_COLUMNS = (
    "SELECT e.id, e.document, e.cmetadata, e.embedding::text AS embedding, "
    "e.embedding <=> CAST(:vector AS vector) AS distance "
)
# plainto_tsquery requires every word to match, so its ANDs are swapped for ORs
KEYWORD_QUERY = "replace(plainto_tsquery('english', :question)::text, '&', '|')::tsquery"
FULL_TEXT = "e.search_text"
SPREAD = (
    "SELECT avg(1 - (e.embedding <=> CAST(:vector AS vector))) AS mean, "
    "stddev_pop(1 - (e.embedding <=> CAST(:vector AS vector))) AS spread "
)
# A chunk with no is_current value is current: only superseded copies carry it
IS_CURRENT = "COALESCE(e.cmetadata->>'is_current', 'true') = 'true' "
STORED_CHUNKS = text(
    "SELECT e.id, e.document, e.cmetadata " + CHUNK_JOIN + "AND " + IS_CURRENT
)
UPSERT_CHUNK = text(
    "INSERT INTO langchain_pg_embedding (id, collection_id, embedding, document, cmetadata) "
    "SELECT :id, c.uuid, CAST(:embedding AS vector), :document, CAST(:metadata AS jsonb) "
    "FROM langchain_pg_collection c WHERE c.name = :collection "
    "ON CONFLICT (id) DO UPDATE SET embedding = EXCLUDED.embedding, document = EXCLUDED.document, "
    "cmetadata = EXCLUDED.cmetadata"
)
DELETE_CHUNKS = text(
    "DELETE FROM langchain_pg_embedding e USING langchain_pg_collection c WHERE e.collection_id = c.uuid "
    "AND c.name = :collection AND e.id = ANY(:ids)"
)
ARCHIVE_CHUNKS = text(
    "INSERT INTO langchain_pg_embedding (id, collection_id, embedding, document, cmetadata) "
    "SELECT e.id || ' @v' || :version, e.collection_id, e.embedding, e.document, e.cmetadata || CAST(:extra AS jsonb) "
    "FROM langchain_pg_embedding e JOIN langchain_pg_collection c ON e.collection_id = c.uuid "
    "WHERE c.name = :collection AND e.id = ANY(:ids) ON CONFLICT (id) DO NOTHING"
)
DELETE_ARCHIVED = text(
    "DELETE FROM langchain_pg_embedding e USING langchain_pg_collection c WHERE e.collection_id = c.uuid "
    "AND c.name = :collection AND e.cmetadata->>'source' = :source AND e.cmetadata->>'is_current' = 'false'"
)
UPDATE_METADATA = text(
    "UPDATE langchain_pg_embedding SET cmetadata = CAST(:metadata AS jsonb) WHERE id = :id "
    "AND collection_id = (SELECT uuid FROM langchain_pg_collection WHERE name = :collection)"
)
# Row-Level Security stops PostgreSQL using a full-text index before it has checked the tenant, which meant parsing
# every chunk's text on every search. Keeping the parsed text in a column removes that cost.
HAS_SEARCH_TEXT = text(
    "SELECT 1 FROM information_schema.columns "
    "WHERE table_name = 'langchain_pg_embedding' AND column_name = 'search_text'"
)
ADD_SEARCH_TEXT = text(
    "ALTER TABLE langchain_pg_embedding ADD COLUMN search_text tsvector "
    "GENERATED ALWAYS AS (to_tsvector('english', document)) STORED"
)
DROP_OLD_SEARCH_INDEX = text("DROP INDEX IF EXISTS ix_embedding_full_text")
DOCUMENT_HASHES = text(
    "SELECT source_file, content_hash FROM policy_documents "
    "WHERE tenant_id = :tenant_id AND collection_name = :collection"
)
SAVE_DOCUMENT = text(
    "INSERT INTO policy_documents (tenant_id, collection_name, source_file, title, category, version, "
    "effective_date, content_hash, chunk_count) VALUES (:tenant_id, :collection, :source_file, :title, :category, "
    ":version, CAST(:effective_date AS date), :content_hash, :chunk_count) "
    "ON CONFLICT (tenant_id, collection_name, source_file) DO UPDATE SET title = EXCLUDED.title, "
    "category = EXCLUDED.category, version = EXCLUDED.version, effective_date = EXCLUDED.effective_date, "
    "content_hash = EXCLUDED.content_hash, chunk_count = EXCLUDED.chunk_count, updated_at = now()"
)
DELETE_DOCUMENT = text(
    "DELETE FROM policy_documents WHERE tenant_id = :tenant_id AND collection_name = :collection "
    "AND source_file = :source_file"
)


class EmbeddingModelMismatch(RuntimeError):
    """This collection was built with another embedding model, and vectors can't be mixed."""


@dataclass
class DocumentRecord:
    source_file: str
    title: str
    category: str
    version: str | None
    effective_date: str | None
    content_hash: str
    chunk_count: int


MODEL_FOR_COLLECTION = text(
    "SELECT provider, model, dimension FROM embedding_models "
    "WHERE tenant_id = :tenant_id AND collection_name = :collection"
)
SAVE_MODEL = text(
    "INSERT INTO embedding_models (tenant_id, collection_name, provider, model, dimension) "
    "VALUES (:tenant_id, :collection, :provider, :model, :dimension) ON CONFLICT DO NOTHING"
)
EXISTING_DIMENSION = text(
    "SELECT vector_dims(e.embedding) FROM langchain_pg_embedding e "
    "JOIN langchain_pg_collection c ON e.collection_id = c.uuid WHERE c.name = :collection LIMIT 1"
)


def embedded_database_uri() -> str:
    """Start the local PostgreSQL with pgvector, if needed, and return its address."""
    # Imported here because the hosted API doesn't install this package
    import pixeltable_pgserver as pgserver

    return pgserver.get_server(config.PGDATA_DIR).get_uri()


def get_database_uri() -> str:
    return config.DATABASE_URL or embedded_database_uri()


def sqlalchemy_url(uri: str) -> str:
    return re.sub(r"^postgres(ql)?://", "postgresql+psycopg://", uri)


def run_migrations(database_uri: str | None = None) -> None:
    """Bring this project's own tables up to date. It's safe to run on every start."""
    alembic_config = Config(str(config.PROJECT_ROOT / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(Path(config.PROJECT_ROOT) / "migrations"))
    alembic_config.attributes["database_uri"] = database_uri or get_database_uri()
    command.upgrade(alembic_config, "head")


def get_store(collection_name: str = config.COLLECTION_NAME, database_uri: str | None = None,
              embeddings=None) -> PGVector:
    uri = database_uri or get_database_uri()
    return PGVector(
        embeddings or get_embeddings(),
        collection_name=collection_name,
        connection=sqlalchemy_url(uri),
        # A hosted database can sleep and drop idle connections. Test each one before use.
        engine_args={"pool_pre_ping": True, "pool_recycle": 240},
    )


def clear_tenant_chunks(collection_name: str, tenant_id: str, database_uri: str | None = None) -> None:
    """Remove one organisation's chunks and recorded model, for a rebuild. Others in the collection stay."""
    with psycopg.connect(database_uri or get_database_uri()) as conn:
        conn.execute(
            "DELETE FROM langchain_pg_embedding e USING langchain_pg_collection c WHERE e.collection_id = c.uuid "
            "AND c.name = %s AND COALESCE(e.cmetadata->>'tenant_id', %s) = %s",
            (collection_name, config.TENANT_ID, tenant_id),
        )
        conn.execute("DELETE FROM embedding_models WHERE collection_name = %s AND tenant_id = %s",
                     (collection_name, tenant_id))


def count_chunks(collection_name: str = config.COLLECTION_NAME, database_uri: str | None = None) -> int:
    """Count stored chunks. Makes no API calls, so /health can use it freely."""
    with psycopg.connect(database_uri or get_database_uri()) as conn:
        row = conn.execute(
            "SELECT count(*) FROM langchain_pg_embedding e "
            f"JOIN langchain_pg_collection c ON e.collection_id = c.uuid WHERE c.name = %s AND {IS_CURRENT}",
            (collection_name,),
        ).fetchone()
    return row[0] if row else 0


def filter_clause(filters: SearchFilters) -> tuple[str, dict]:
    """Build the SQL for a search's filters. Values are always passed as parameters."""
    params: dict = {"tenant_id": filters.tenant_id, "default_tenant": config.TENANT_ID}
    clause = ""
    if filters.policy:
        clause += "AND e.cmetadata->>'policy' = :policy "
        params["policy"] = filters.policy
    if filters.category:
        clause += "AND e.cmetadata->>'category' = :category "
        params["category"] = filters.category
    if filters.categories:
        clause += "AND e.cmetadata->>'category' = ANY(:categories) "
        params["categories"] = filters.categories
    if filters.effective_on:
        # ISO dates sort as text. A superseded copy counts until the day it was replaced.
        clause += ("AND e.cmetadata->>'effective_date' <= :effective_on "
                   "AND (" + IS_CURRENT + "OR e.cmetadata->>'superseded_on' > :effective_on) ")
        params["effective_on"] = filters.effective_on
    elif not filters.include_history:
        clause += "AND " + IS_CURRENT
    return clause, params


class PolicyIndex:
    """One organisation's view of the vector database. It can't see or change anyone else's chunks.

    Every statement runs inside the tenant, so PostgreSQL's Row-Level Security applies as well as
    the filters here. The library's own write methods run as the table owner, so they aren't used.
    """

    def __init__(self, store: PGVector, tenant_id: str = config.TENANT_ID):
        self.store = store
        self.tenant_id = check_tenant_id(tenant_id)
        with store.session_maker() as session:
            secure_library_tables(session, config.TENANT_ID)  # also makes other server processes wait for this step
            if not session.execute(HAS_SEARCH_TEXT).first():
                session.execute(ADD_SEARCH_TEXT)
                session.execute(DROP_OLD_SEARCH_INDEX)
            session.commit()
        self._check_embedding_model()

    @contextmanager
    def _session(self) -> Iterator:
        with self.store.session_maker() as session:
            enter_tenant(session, self.tenant_id)
            yield session
            session.commit()

    def _check_owner(self, chunks: list[Document]) -> None:
        for chunk in chunks:
            if chunk.metadata.get("tenant_id", config.TENANT_ID) != self.tenant_id:
                raise ValueError("A chunk for another tenant can't be stored here")

    def _check_embedding_model(self) -> None:
        """Refuse to run on a collection built with another embedding model."""
        with self._session() as session:
            row = session.execute(MODEL_FOR_COLLECTION, {"tenant_id": self.tenant_id,
                                                         "collection": self.store.collection_name}).first()
            if row:
                if (row.provider, row.model) != (config.EMBEDDING_PROVIDER, config.EMBEDDING_MODEL):
                    raise EmbeddingModelMismatch(
                        f"Collection {self.store.collection_name} uses {row.provider}/{row.model}, but the settings "
                        f"ask for {config.EMBEDDING_PROVIDER}/{config.EMBEDDING_MODEL}. Change the settings back, "
                        "or re-index with: python -m pipeline.embed_and_store --rebuild")
                return
            dimension = session.execute(EXISTING_DIMENSION, {"collection": self.store.collection_name}).scalar()
            if dimension:  # built before this record existed
                self._record_embedding_model(session, int(dimension))

    def _record_embedding_model(self, session, dimension: int) -> None:
        session.execute(SAVE_MODEL, {"tenant_id": self.tenant_id, "collection": self.store.collection_name,
                                     "provider": config.EMBEDDING_PROVIDER, "model": config.EMBEDDING_MODEL,
                                     "dimension": dimension})

    def _check_dimension(self, dimension: int) -> None:
        with self._session() as session:
            row = session.execute(MODEL_FOR_COLLECTION, {"tenant_id": self.tenant_id,
                                                         "collection": self.store.collection_name}).first()
            if not row:
                self._record_embedding_model(session, dimension)
            elif row.dimension != dimension:
                raise EmbeddingModelMismatch(
                    f"Collection {self.store.collection_name} holds {row.dimension}-dimension vectors, and this model "
                    f"returned {dimension}. Re-index with: python -m pipeline.embed_and_store --rebuild")

    @property
    def embeddings(self):
        return self.store.embeddings

    def candidates(self, vector: list[float], question: str, k: int, use_keywords: bool = True,
                   filters: SearchFilters | None = None) -> list[Candidate]:
        """Return the k closest chunks, plus the k best keyword matches, within the filters."""
        filters = filters or SearchFilters(tenant_id=self.tenant_id)
        if filters.tenant_id != self.tenant_id:
            raise ValueError("A search can't ask for another tenant's chunks")
        where, filter_params = filter_clause(filters)
        params = {"vector": str(vector), "collection": self.store.collection_name, "k": k, **filter_params}
        keyword_sql = text(
            CHUNK_COLUMNS + CHUNK_JOIN + where + f"AND {FULL_TEXT} @@ {KEYWORD_QUERY} "
            f"ORDER BY ts_rank_cd({FULL_TEXT}, {KEYWORD_QUERY}) DESC, e.id LIMIT :k"  # the id makes ties repeatable
        )
        with self._session() as session:
            vector_rows = session.execute(text(CHUNK_COLUMNS + CHUNK_JOIN + where + "ORDER BY distance, e.id LIMIT :k"),
                                          params).all()
            keyword_rows = session.execute(keyword_sql, {**params, "question": question}).all() if use_keywords else []
            # Measured across the whole tenant, so a topic filter can't make a strong chunk look weak
            stats = session.execute(text(SPREAD + CHUNK_JOIN + "AND " + IS_CURRENT), {
                "vector": params["vector"], "collection": params["collection"],
                "tenant_id": filter_params["tenant_id"], "default_tenant": filter_params["default_tenant"],
            }).one()

        found: dict[str, Candidate] = {}
        for row in [*vector_rows, *keyword_rows]:
            found.setdefault(row.id, Candidate(
                chunk=Document(id=row.id, page_content=row.document, metadata=row.cmetadata),
                distance=row.distance,
                embedding=json.loads(row.embedding),
            ))
        for rank, row in enumerate(vector_rows, start=1):
            found[row.id].vector_rank = rank
        for rank, row in enumerate(keyword_rows, start=1):
            found[row.id].keyword_rank = rank

        results = list(found.values())
        if results:
            similarities = np.array([1 - candidate.distance for candidate in results])
            scores = evidence_scores(similarities, float(stats.mean or 0), float(stats.spread or 0))
            for candidate, score in zip(results, scores, strict=True):
                candidate.confidence = float(score)
        return results

    def stored_chunks(self) -> dict[str, Document]:
        """Return every current chunk of this tenant, without its embedding, by id."""
        with self._session() as session:
            rows = session.execute(STORED_CHUNKS, {"collection": self.store.collection_name,
                                                   "tenant_id": self.tenant_id,
                                                   "default_tenant": config.TENANT_ID}).all()
        return {row.id: Document(id=row.id, page_content=row.document, metadata=row.cmetadata)
                for row in rows}

    def add(self, chunks: list[Document]) -> None:
        """Embed and store the chunks. A chunk whose id is already stored is replaced."""
        self._check_owner(chunks)
        vectors = self.store.embeddings.embed_documents([chunk.page_content for chunk in chunks])
        self._check_dimension(len(vectors[0]))
        with self._session() as session:
            # If an id belonged to another tenant, Row-Level Security would refuse this rather than overwrite it
            session.execute(UPSERT_CHUNK, [
                {"id": str(chunk.id), "embedding": str(vector), "document": chunk.page_content,
                 "metadata": json.dumps(chunk.metadata), "collection": self.store.collection_name}
                for chunk, vector in zip(chunks, vectors, strict=True)
            ])

    def archive_chunks(self, ids: list[str], version_number: int, superseded_on: str) -> None:
        """Copy chunks as a superseded version, with their embeddings, so no quota is used."""
        extra = json.dumps({"is_current": "false", "version_number": version_number, "superseded_on": superseded_on})
        with self._session() as session:
            session.execute(ARCHIVE_CHUNKS, {"ids": ids, "version": version_number, "extra": extra,
                                             "collection": self.store.collection_name})

    def delete_archived(self, source_file: str) -> None:
        with self._session() as session:
            session.execute(DELETE_ARCHIVED, {"collection": self.store.collection_name, "source": source_file})

    def update_metadata(self, chunks: list[Document]) -> None:
        """Change stored details without embedding anything again."""
        self._check_owner(chunks)
        with self._session() as session:
            for chunk in chunks:
                session.execute(UPDATE_METADATA, {"id": chunk.id, "metadata": json.dumps(chunk.metadata),
                                                  "collection": self.store.collection_name})

    def delete(self, ids: list[str]) -> None:
        with self._session() as session:
            session.execute(DELETE_CHUNKS, {"ids": ids, "collection": self.store.collection_name})

    def document_hashes(self) -> dict[str, str]:
        with self._session() as session:
            rows = session.execute(DOCUMENT_HASHES, {"tenant_id": self.tenant_id,
                                                     "collection": self.store.collection_name}).all()
        return {row.source_file: row.content_hash for row in rows}

    def save_document(self, record: DocumentRecord) -> None:
        with self._session() as session:
            session.execute(SAVE_DOCUMENT, {**record.__dict__, "tenant_id": self.tenant_id,
                                            "collection": self.store.collection_name})

    def delete_document(self, source_file: str) -> None:
        with self._session() as session:
            session.execute(DELETE_DOCUMENT, {"tenant_id": self.tenant_id, "source_file": source_file,
                                              "collection": self.store.collection_name})
