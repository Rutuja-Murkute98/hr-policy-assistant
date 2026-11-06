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
STORED_CHUNKS = text(
    "SELECT e.id, e.document, e.cmetadata FROM langchain_pg_embedding e "
    "JOIN langchain_pg_collection c ON e.collection_id = c.uuid WHERE c.name = :collection"
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


@dataclass
class DocumentRecord:
    source_file: str
    title: str
    category: str
    version: str | None
    effective_date: str | None
    content_hash: str
    chunk_count: int


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


def count_chunks(collection_name: str = config.COLLECTION_NAME, database_uri: str | None = None) -> int:
    """Count stored chunks. Makes no API calls, so /health can use it freely."""
    with psycopg.connect(database_uri or get_database_uri()) as conn:
        row = conn.execute(
            "SELECT count(*) FROM langchain_pg_embedding e "
            "JOIN langchain_pg_collection c ON e.collection_id = c.uuid WHERE c.name = %s",
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
        clause += "AND e.cmetadata->>'effective_date' <= :effective_on "  # ISO dates sort as text
        params["effective_on"] = filters.effective_on
    return clause, params


class PolicyIndex:
    """The searches and changes the app needs from the vector database."""

    def __init__(self, store: PGVector):
        self.store = store
        with store.session_maker() as session:
            session.execute(SEARCH_INDEX)
            session.commit()

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
            f"ORDER BY ts_rank_cd({FULL_TEXT}, {KEYWORD_QUERY}) DESC LIMIT :k"
        )
        with self.store.session_maker() as session:
            vector_rows = session.execute(text(CHUNK_COLUMNS + CHUNK_JOIN + where + "ORDER BY distance LIMIT :k"),
                                          params).all()
            keyword_rows = session.execute(keyword_sql, {**params, "question": question}).all() if use_keywords else []
            spread_params = {key: value for key, value in params.items() if key != "k"}
            stats = session.execute(text(SPREAD + CHUNK_JOIN + where), spread_params).one()

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
            scores = evidence_scores(similarities, float(stats.mean), float(stats.spread or 0))
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
        self.store.add_documents(chunks, ids=[chunk.id for chunk in chunks])

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
