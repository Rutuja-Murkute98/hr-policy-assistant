"""Connect to the vector database. Used by both the indexing script and the API.

With DATABASE_URL set, this uses that hosted database (such as Neon).
Without it, PostgreSQL runs inside Python and keeps its data in the pgdata folder.
"""
import json
import re
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
FULL_TEXT = "to_tsvector('english', e.document)"
SPREAD = (
    "SELECT avg(1 - (e.embedding <=> CAST(:vector AS vector))) AS mean, "
    "stddev_pop(1 - (e.embedding <=> CAST(:vector AS vector))) AS spread "
)
# A chunk with no is_current value is current: only superseded copies carry it
IS_CURRENT = "COALESCE(e.cmetadata->>'is_current', 'true') = 'true' "
STORED_CHUNKS = text(
    "SELECT e.id, e.document, e.cmetadata FROM langchain_pg_embedding e "
    f"JOIN langchain_pg_collection c ON e.collection_id = c.uuid WHERE c.name = :collection AND {IS_CURRENT}"
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
SEARCH_INDEX = text(
    "CREATE INDEX IF NOT EXISTS ix_embedding_full_text ON langchain_pg_embedding "
    "USING gin (to_tsvector('english', document))"
)
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


def forget_embedding_model(collection_name: str, database_uri: str | None = None) -> None:
    """Clear a collection's recorded model, after a rebuild removes its vectors."""
    with psycopg.connect(database_uri or get_database_uri()) as conn:
        if conn.execute("SELECT to_regclass('embedding_models')").fetchone()[0]:  # type: ignore[index]
            conn.execute("DELETE FROM embedding_models WHERE collection_name = %s", (collection_name,))


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
    """The searches and changes the app needs from the vector database."""

    def __init__(self, store: PGVector):
        self.store = store
        with store.session_maker() as session:
            session.execute(SEARCH_INDEX)
            session.commit()
        self._check_embedding_model()

    def _check_embedding_model(self) -> None:
        """Refuse to run on a collection built with another embedding model."""
        with self.store.session_maker() as session:
            if not session.execute(text("SELECT to_regclass('embedding_models')")).scalar():
                return  # migrations haven't created the table yet, as in the first indexing run
            row = session.execute(MODEL_FOR_COLLECTION, {"tenant_id": config.TENANT_ID,
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
        session.execute(SAVE_MODEL, {"tenant_id": config.TENANT_ID, "collection": self.store.collection_name,
                                     "provider": config.EMBEDDING_PROVIDER, "model": config.EMBEDDING_MODEL,
                                     "dimension": dimension})
        session.commit()

    def _check_dimension(self, dimension: int) -> None:
        with self.store.session_maker() as session:
            if not session.execute(text("SELECT to_regclass('embedding_models')")).scalar():
                return
            row = session.execute(MODEL_FOR_COLLECTION, {"tenant_id": config.TENANT_ID,
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
        where, filter_params = filter_clause(filters or SearchFilters(tenant_id=config.TENANT_ID))
        params = {"vector": str(vector), "collection": self.store.collection_name, "k": k, **filter_params}
        keyword_sql = text(
            CHUNK_COLUMNS + CHUNK_JOIN + where + f"AND {FULL_TEXT} @@ {KEYWORD_QUERY} "
            f"ORDER BY ts_rank_cd({FULL_TEXT}, {KEYWORD_QUERY}) DESC, e.id LIMIT :k"  # the id makes ties repeatable
        )
        with self.store.session_maker() as session:
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
        """Return every stored chunk, without its embedding, by id."""
        with self.store.session_maker() as session:
            rows = session.execute(STORED_CHUNKS, {"collection": self.store.collection_name}).all()
        return {row.id: Document(id=row.id, page_content=row.document, metadata=row.cmetadata)
                for row in rows}

    def add(self, chunks: list[Document]) -> None:
        """Embed and store the chunks. A chunk whose id is already stored is replaced."""
        vectors = self.store.embeddings.embed_documents([chunk.page_content for chunk in chunks])
        self._check_dimension(len(vectors[0]))
        self.store.add_embeddings(texts=[chunk.page_content for chunk in chunks], embeddings=vectors,
                                  metadatas=[chunk.metadata for chunk in chunks],
                                  ids=[str(chunk.id) for chunk in chunks])

    def archive_chunks(self, ids: list[str], version_number: int, superseded_on: str) -> None:
        """Copy chunks as a superseded version, with their embeddings, so no quota is used."""
        extra = json.dumps({"is_current": "false", "version_number": version_number, "superseded_on": superseded_on})
        with self.store.session_maker() as session:
            session.execute(ARCHIVE_CHUNKS, {"ids": ids, "version": version_number, "extra": extra,
                                             "collection": self.store.collection_name})
            session.commit()

    def delete_archived(self, source_file: str) -> None:
        with self.store.session_maker() as session:
            session.execute(DELETE_ARCHIVED, {"collection": self.store.collection_name, "source": source_file})
            session.commit()

    def update_metadata(self, chunks: list[Document]) -> None:
        """Change stored details without embedding anything again."""
        with self.store.session_maker() as session:
            for chunk in chunks:
                session.execute(UPDATE_METADATA, {"id": chunk.id, "metadata": json.dumps(chunk.metadata),
                                                  "collection": self.store.collection_name})
            session.commit()

    def delete(self, ids: list[str]) -> None:
        self.store.delete(ids=ids)

    def document_hashes(self) -> dict[str, str]:
        with self.store.session_maker() as session:
            rows = session.execute(DOCUMENT_HASHES, {"tenant_id": config.TENANT_ID,
                                                     "collection": self.store.collection_name}).all()
        return {row.source_file: row.content_hash for row in rows}

    def save_document(self, record: DocumentRecord) -> None:
        with self.store.session_maker() as session:
            session.execute(SAVE_DOCUMENT, {**record.__dict__, "tenant_id": config.TENANT_ID,
                                            "collection": self.store.collection_name})
            session.commit()

    def delete_document(self, source_file: str) -> None:
        with self.store.session_maker() as session:
            session.execute(DELETE_DOCUMENT, {"tenant_id": config.TENANT_ID, "source_file": source_file,
                                              "collection": self.store.collection_name})
            session.commit()
