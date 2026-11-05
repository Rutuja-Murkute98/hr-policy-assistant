"""Stage B: the offline indexing step. Run it after adding or editing a policy.

    venv\\Scripts\\python -m pipeline.embed_and_store            update what changed
    venv\\Scripts\\python -m pipeline.embed_and_store --prune    also remove policies with no file
    venv\\Scripts\\python -m pipeline.embed_and_store --rebuild  embed everything again

It compares each section with what's stored, so only new and edited sections use
embedding quota, and sections removed from a file are deleted. The free tier allows 100
embeddings a minute and 1,000 a day. If a run stops, run it again: it carries on from
the sections not stored yet.
"""
import argparse
import time
from collections.abc import Iterable
from dataclasses import dataclass, field

from langchain_core.documents import Document

BATCH_SIZE = 80      # stays under the limit of 100 per minute
WAIT_SECONDS = 65


@dataclass
class SyncPlan:
    new: list[Document] = field(default_factory=list)
    edited: list[Document] = field(default_factory=list)
    stale_ids: list[str] = field(default_factory=list)
    unchanged: int = 0

    @property
    def to_embed(self) -> list[Document]:
        return self.new + self.edited

    def summary(self) -> dict[str, int]:
        return {"added": len(self.new), "updated": len(self.edited),
                "removed": len(self.stale_ids), "unchanged": self.unchanged}


def plan_sync(stored: dict[str, Document], chunks: list[Document], sources: Iterable[str]) -> SyncPlan:
    """Work out what has to change. Only stored chunks from the given source files can be removed.

    The stored text is what was embedded, so comparing it shows exactly what needs embedding again.
    """
    plan = SyncPlan()
    current_ids = {chunk.id for chunk in chunks}
    for chunk in chunks:
        if chunk.id not in stored:
            plan.new.append(chunk)
        elif stored[chunk.id].page_content != chunk.page_content:
            plan.edited.append(chunk)
        else:
            plan.unchanged += 1
    scope = set(sources)
    plan.stale_ids = [chunk_id for chunk_id, chunk in stored.items()
                      if chunk.metadata["source"] in scope and chunk_id not in current_ids]
    return plan


def run_sync(index, plan: SyncPlan, wait_seconds: int = WAIT_SECONDS) -> None:
    """Apply a plan. New text is stored first, so a failure can't lose what's already there."""
    to_embed = plan.to_embed
    for start in range(0, len(to_embed), BATCH_SIZE):
        if start > 0:
            print(f"Waiting {wait_seconds} seconds for the rate limit...", flush=True)
            time.sleep(wait_seconds)
        batch = to_embed[start:start + BATCH_SIZE]
        index.add(batch)
        print(f"Embedded {start + len(batch)}/{len(to_embed)}", flush=True)
    if plan.stale_ids:
        index.delete(plan.stale_ids)


def sources_without_a_file(stored: dict[str, Document], chunks: list[Document]) -> list[str]:
    on_disk = {chunk.metadata["source"] for chunk in chunks}
    return sorted({chunk.metadata["source"] for chunk in stored.values()} - on_disk)


def main() -> None:
    from app.rag.store import PolicyIndex, get_store
    from pipeline.chunk_documents import load_chunks

    parser = argparse.ArgumentParser(description="Index the HR policies into pgvector")
    parser.add_argument("--rebuild", action="store_true",
                        help="delete the stored chunks first (uses 539 embeddings of your daily quota)")
    parser.add_argument("--prune", action="store_true",
                        help="remove stored policies with no file in the policy folder")
    args = parser.parse_args()

    vector_store = get_store()
    if args.rebuild:
        vector_store.delete_collection()
        vector_store.create_collection()

    index = PolicyIndex(vector_store)
    chunks = load_chunks()
    stored = index.stored_chunks()
    orphans = sources_without_a_file(stored, chunks)
    sources = {chunk.metadata["source"] for chunk in chunks} | (set(orphans) if args.prune else set())
    plan = plan_sync(stored, chunks, sources)

    print(f"{len(chunks)} chunks: {plan.summary()}")
    if orphans and not args.prune:
        # A policy uploaded on the admin screen may exist only in the database, so it's kept
        print(f"Kept {len(orphans)} stored policies with no file here: {', '.join(orphans)}")
        print("Use --prune to remove them.")
    run_sync(index, plan)
    print("Indexing finished")


if __name__ == "__main__":
    main()
