"""Connect to the vector database. Used by both the indexing script and the API.

With DATABASE_URL set, this uses that hosted database (such as Neon).
Without it, PostgreSQL runs inside Python and keeps its data in the pgdata folder.
"""
import re

import psycopg
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_postgres import PGVector

from app import config


def embedded_database_uri() -> str:
    """Start the local PostgreSQL with pgvector, if needed, and return its address."""
    # Imported here because the hosted API doesn't install this package
    import pixeltable_pgserver as pgserver

    return pgserver.get_server(config.PGDATA_DIR).get_uri()


def get_database_uri() -> str:
    return config.DATABASE_URL or embedded_database_uri()


def get_embeddings() -> GoogleGenerativeAIEmbeddings:
    return GoogleGenerativeAIEmbeddings(model=config.EMBEDDING_MODEL)


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
