"""Connect to the vector database. Used by both the indexing script and the API."""
import psycopg
import pixeltable_pgserver as pgserver
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_postgres import PGVector

from app import config


def get_database_uri() -> str:
    """Start PostgreSQL with pgvector, if needed, and return its address."""
    return pgserver.get_server(config.PGDATA_DIR).get_uri()


def get_embeddings() -> GoogleGenerativeAIEmbeddings:
    return GoogleGenerativeAIEmbeddings(model=config.EMBEDDING_MODEL)


def get_store(collection_name: str = config.COLLECTION_NAME) -> PGVector:
    connection = get_database_uri().replace("postgresql://", "postgresql+psycopg://", 1)
    return PGVector(get_embeddings(), collection_name=collection_name, connection=connection)


def count_chunks(collection_name: str = config.COLLECTION_NAME) -> int:
    """Count stored chunks. Makes no API calls, so /health can use it freely."""
    with psycopg.connect(get_database_uri()) as conn:
        row = conn.execute(
            "SELECT count(*) FROM langchain_pg_embedding e "
            "JOIN langchain_pg_collection c ON e.collection_id = c.uuid WHERE c.name = %s",
            (collection_name,),
        ).fetchone()
    return row[0]
