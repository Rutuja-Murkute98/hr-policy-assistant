"""Copy the indexed chunks from this PC to a hosted database, such as Neon.

This copies the stored embeddings, so it uses none of your Gemini quota.

Run this, then paste your database's direct connection string when asked:
    venv\\Scripts\\python -m pipeline.copy_to_cloud

The string stays hidden and isn't saved. To skip the question, you can set
CLOUD_DATABASE_URL in .env instead.

It's safe to run again: chunks with the same id are updated, not duplicated.
"""
import getpass
import json
import os
import sys

import psycopg

from app import config
from app.rag.store import count_chunks, embedded_database_uri, get_store

BATCH_SIZE = 100


def read_local_chunks() -> list[tuple]:
    with psycopg.connect(embedded_database_uri()) as conn:
        return conn.execute(
            "SELECT e.id, e.document, e.cmetadata, e.embedding::text FROM langchain_pg_embedding e "
            "JOIN langchain_pg_collection c ON e.collection_id = c.uuid WHERE c.name = %s ORDER BY e.id",
            (config.COLLECTION_NAME,),
        ).fetchall()


def copy_chunks(rows: list[tuple], target_uri: str) -> None:
    store = get_store(database_uri=target_uri)
    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start:start + BATCH_SIZE]
        store.add_embeddings(
            texts=[row[1] for row in batch],
            embeddings=[json.loads(row[3]) for row in batch],
            metadatas=[row[2] for row in batch],
            ids=[row[0] for row in batch],
        )
        print(f"Copied {start + len(batch)}/{len(rows)}", flush=True)


def get_target_uri() -> str:
    """Use CLOUD_DATABASE_URL if it's set. Otherwise ask, keeping the answer hidden."""
    uri = os.getenv("CLOUD_DATABASE_URL", "").strip()
    if not uri:
        if not sys.stdin.isatty():
            sys.exit("Set CLOUD_DATABASE_URL, or run this in a terminal to paste it.")
        uri = getpass.getpass("Paste your Neon connection string (it stays hidden), then press Enter: ").strip()
    if not uri.startswith(("postgresql://", "postgres://")):
        sys.exit("That isn't a connection string. It should start with postgresql://")
    return uri


if __name__ == "__main__":
    target = get_target_uri()
    chunks = read_local_chunks()
    if not chunks:
        sys.exit("No local chunks found. Run: python -m pipeline.embed_and_store")

    copy_chunks(chunks, target)
    stored = count_chunks(database_uri=target)
    if stored != len(chunks):
        sys.exit(f"Check failed: the hosted database has {stored} chunks, expected {len(chunks)}.")
    print(f"Done: the hosted database has all {stored} chunks.")
