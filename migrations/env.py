"""How Alembic connects. A caller can pass its own database address; otherwise the app's settings decide."""
from alembic import context
from sqlalchemy import create_engine, pool

from app.rag.store import get_database_uri, sqlalchemy_url


def run_migrations() -> None:
    url = context.config.attributes.get("database_uri") or get_database_uri()
    engine = create_engine(sqlalchemy_url(url), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


run_migrations()
