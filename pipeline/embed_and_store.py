"""Stage B: the offline indexing step. Run it once, separately from the app.

    venv\\Scripts\\python -m pipeline.embed_and_store            embed new chunks only
    venv\\Scripts\\python -m pipeline.embed_and_store --rebuild  start again (after editing a policy)

The free tier allows 100 embeddings a minute and 1,000 a day. If a run stops,
run it again: it carries on from the chunks not stored yet.
"""
import argparse
import time

from app.rag.store import get_store
from pipeline.chunk_documents import load_chunks

BATCH_SIZE = 80      # stays under the limit of 100 per minute
WAIT_SECONDS = 65


def index_chunks(store, chunks, wait_seconds: int = WAIT_SECONDS) -> int:
    """Embed and store the chunks that aren't stored yet. Returns how many were added."""
    stored_ids = {doc.id for doc in store.get_by_ids([chunk.id for chunk in chunks])}
    missing = [chunk for chunk in chunks if chunk.id not in stored_ids]
    print(f"{len(chunks)} chunks: {len(stored_ids)} already stored, {len(missing)} to embed")

    for start in range(0, len(missing), BATCH_SIZE):
        if start > 0:
            print(f"Waiting {wait_seconds} seconds for the rate limit...", flush=True)
            time.sleep(wait_seconds)
        batch = missing[start:start + BATCH_SIZE]
        store.add_documents(batch, ids=[chunk.id for chunk in batch])
        print(f"Embedded {start + len(batch)}/{len(missing)}", flush=True)
    return len(missing)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Index the HR policies into pgvector")
    parser.add_argument("--rebuild", action="store_true",
                        help="delete the stored chunks first (uses 539 embeddings of your daily quota)")
    args = parser.parse_args()

    vector_store = get_store()
    if args.rebuild:
        vector_store.delete_collection()
        vector_store.create_collection()
    index_chunks(vector_store, load_chunks())
    print("Indexing finished")
