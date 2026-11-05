"""Connect to the vector database. Used by both the indexing script and the API.

With DATABASE_URL set, this uses that hosted database (such as Neon).
Without it, PostgreSQL runs inside Python and keeps its data in the pgdata folder.
"""
import json
import re

import psycopg
from langchain_core.documents import Document
from langchain_postgres import PGVector
from sqlalchemy import text

from app import config
from app.rag.providers import get_embeddings
from app.rag.retrieval import Candidate

CHUNK_COLUMNS = (
    "SELECT e.id, e.document, e.cmetadata, e.embedding::text AS embedding, "
    "e.embedding <=> CAST(:vector AS vector) AS distance "
    "FROM langchain_pg_embedding e JOIN langchain_pg_collection c ON e.collection_id = c.uuid "
    "WHERE c.name = :collection "
)
VECTOR_SEARCH = text(CHUNK_COLUMNS + "ORDER BY distance LIMIT :k")
# plainto_tsquery requires every word to match, so its ANDs are swapped for ORs
KEYWORD_QUERY = "replace(plainto_tsquery('english', :question)::text, '&', '|')::tsquery"
KEYWORD_SEARCH = text(
    CHUNK_COLUMNS + f"AND to_tsvector('english', e.document) @@ {KEYWORD_QUERY} "
    f"ORDER BY ts_rank_cd(to_tsvector('english', e.document), {KEYWORD_QUERY}) DESC LIMIT :k"
)
STORED_CHUNKS = text(
    "SELECT e.id, e.document, e.cmetadata FROM langchain_pg_embedding e "
    "JOIN langchain_pg_collection c ON e.collection_id = c.uuid WHERE c.name = :collection"
)


def embedded_database_uri() -> str:
    """Start the local PostgreSQL with pgvector, if needed, and return its address."""
    # Imported here because the hosted API doesn't install this package
    import pixeltable_pgserver as pgserver

    return pgserver.get_server(config.PGDATA_DIR).get_uri()


def get_database_uri() -> str:
    return config.DATABASE_URL or embedded_database_uri()


def get_store(collection_name: str = config.COLLECTION_NAME, database_uri: str | None = None) -> PGVector:
    uri = database_uri or get_database_uri()
    return PGVector(
        get_embeddings(),
        collection_name=collection_name,
        connection=re.sub(r"^postgres(ql)?://", "postgresql+psycopg://", uri),
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
    return row[0]


class PolicyIndex:
    """The searches and changes the app needs from the vector database."""

    def __init__(self, store: PGVector):
        self.store = store

    @property
    def embeddings(self):
        return self.store.embeddings

    def candidates(self, vector: list[float], question: str, k: int,
                   use_keywords: bool = True) -> list[Candidate]:
        """Return the k closest chunks, plus the k best keyword matches."""
        params = {"vector": str(vector), "collection": self.store.collection_name, "k": k}
        with self.store.session_maker() as session:
            vector_rows = session.execute(VECTOR_SEARCH, params).all()
            keyword_rows = session.execute(KEYWORD_SEARCH, {**params, "question": question}).all() \
                if use_keywords else []

        found: dict[str, Candidate] = {}
        for row in [*vector_rows, *keyword_rows]:
            found.setdefault(row.id, Candidate(
                chunk=Document(id=row.id, page_content=row.document, metadata=row.cmetadata),
                distance=row.distance,
                embedding=json.loads(row.embedding),
            ))
        for rank, row in enumerate(keyword_rows, start=1):
            found[row.id].keyword_rank = rank
        return list(found.values())

    def stored_chunks(self) -> dict[str, Document]:
        """Return every stored chunk, without its embedding, by id."""
        with self.store.session_maker() as session:
            rows = session.execute(STORED_CHUNKS, {"collection": self.store.collection_name}).all()
        return {row.id: Document(id=row.id, page_content=row.document, metadata=row.cmetadata)
                for row in rows}

    def add(self, chunks: list[Document]) -> None:
        """Embed and store the chunks. A chunk whose id is already stored is replaced."""
        self.store.add_documents(chunks, ids=[chunk.id for chunk in chunks])

    def delete(self, ids: list[str]) -> None:
        self.store.delete(ids=ids)
