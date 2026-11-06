"""The database connection for this project's own tables."""
from sqlalchemy import Engine, create_engine

from app.rag.store import get_database_uri, sqlalchemy_url


def get_engine(database_uri: str | None = None) -> Engine:
    # A hosted database can sleep and drop idle connections, so test each one before use
    return create_engine(sqlalchemy_url(database_uri or get_database_uri()), pool_pre_ping=True, pool_recycle=240)
