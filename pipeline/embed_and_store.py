"""Stage B: the offline indexing step. Run it after adding or editing a policy.

    python -m pipeline.embed_and_store            update what changed
    python -m pipeline.embed_and_store --prune    also remove policies with no file
    python -m pipeline.embed_and_store --rebuild  embed everything again

Each document has a content hash, so an unchanged document is skipped. In a changed document,
only new and edited sections use embedding quota, and sections that were renamed or removed
are deleted. The free tier allows 100 embeddings a minute and 1,000 a day. If a run stops,
run it again: it carries on from the sections not stored yet.
"""
import argparse
import hashlib
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from langchain_core.documents import Document

BATCH_SIZE = 80      # stays under the limit of 100 per minute
WAIT_SECONDS = 65


@dataclass
class SyncPlan:
    new: list[Document] = field(default_factory=list)
    edited: list[Document] = field(default_factory=list)
    relabelled: list[Document] = field(default_factory=list)  # same text, different details
    stale_ids: list[str] = field(default_factory=list)
    unchanged: int = 0

    @property
    def to_embed(self) -> list[Document]:
        return self.new + self.edited

    def summary(self) -> dict[str, int]:
        return {"added": len(self.new), "updated": len(self.edited), "relabelled": len(self.relabelled),
                "removed": len(self.stale_ids), "unchanged": self.unchanged}


def plan_sync(stored: dict[str, Document], chunks: list[Document], sources: Iterable[str]) -> SyncPlan:
    """Work out what has to change. Only stored chunks from the given source files can be removed.

    The stored text is what was embedded, so comparing it shows exactly what needs embedding again.
    """
    plan = SyncPlan()
    current_ids = {chunk.id for chunk in chunks}
    for chunk in chunks:
        assert chunk.id is not None
        if chunk.id not in stored:
            plan.new.append(chunk)
        elif stored[chunk.id].page_content != chunk.page_content:
            plan.edited.append(chunk)
        elif stored[chunk.id].metadata != chunk.metadata:
            plan.relabelled.append(chunk)
        else:
            plan.unchanged += 1
    scope = set(sources)
    plan.stale_ids = [chunk_id for chunk_id, chunk in stored.items()
                      if chunk.metadata["source"] in scope and chunk_id not in current_ids]
    return plan


def run_sync(index, plan: SyncPlan, wait_seconds: int = WAIT_SECONDS,
             progress: Callable[[int], None] | None = None) -> None:
    """Apply a plan. New text is stored first, so a failure can't lose what's already there."""
    to_embed = plan.to_embed
    for start in range(0, len(to_embed), BATCH_SIZE):
        if start > 0:
            print(f"Waiting {wait_seconds} seconds for the rate limit...", flush=True)
            time.sleep(wait_seconds)
        batch = to_embed[start:start + BATCH_SIZE]
        index.add(batch)
        print(f"Embedded {start + len(batch)}/{len(to_embed)}", flush=True)
        if progress:
            progress(start + len(batch))
    if plan.relabelled:
        index.update_metadata(plan.relabelled)
    if plan.stale_ids:
        index.delete(plan.stale_ids)


def content_hash(text: str, category: str) -> str:
    """Identify a document's content. The category is included so changing it counts as a change."""
    return hashlib.sha256(f"{category}\n{text}".encode()).hexdigest()


@dataclass
class DocumentChange:
    """One document's text and chunks, ready to compare with what's stored."""

    source_file: str
    text: str
    chunks: list[Document]

    def record(self):
        from app.rag.store import DocumentRecord

        details = self.chunks[0].metadata
        return DocumentRecord(
            source_file=self.source_file, title=details["policy"], category=details["category"],
            version=details["version"], effective_date=details["effective_date"],
            content_hash=content_hash(self.text, details["category"]), chunk_count=len(self.chunks),
        )


def plan_documents(index, documents: list[DocumentChange]) -> tuple[SyncPlan, list[DocumentChange]]:
    """Skip documents whose hash hasn't changed, and plan the rest. Returns (plan, changed documents)."""
    hashes = index.document_hashes()
    stored = index.stored_chunks()
    changed = [d for d in documents if hashes.get(d.source_file) != d.record().content_hash]
    plan = plan_sync(stored, [chunk for d in changed for chunk in d.chunks], {d.source_file for d in changed})
    plan.unchanged += sum(len(d.chunks) for d in documents if d not in changed)
    return plan, changed


def apply_documents(index, plan: SyncPlan, changed: list[DocumentChange], wait_seconds: int = WAIT_SECONDS) -> None:
    run_sync(index, plan, wait_seconds)
    for document in changed:  # recorded last, so an interrupted run is picked up next time
        index.save_document(document.record())


def sources_without_a_file(stored: dict[str, Document], chunks: list[Document]) -> list[str]:
    on_disk = {chunk.metadata["source"] for chunk in chunks}
    return sorted({chunk.metadata["source"] for chunk in stored.values()} - on_disk)


def main() -> None:
    from app import config
    from app.db import get_engine
    from app.knowledge import KnowledgeBase
    from app.rag.store import PolicyIndex, forget_embedding_model, get_store, run_migrations
    from pipeline.chunk_documents import POLICY_DIR, chunk_text

    parser = argparse.ArgumentParser(description="Index the HR policies into pgvector")
    parser.add_argument("--rebuild", action="store_true",
                        help="delete the stored chunks first (uses 539 embeddings of your daily quota)")
    parser.add_argument("--prune", action="store_true",
                        help="remove stored policies with no file in the policy folder")
    args = parser.parse_args()

    run_migrations()
    vector_store = get_store()
    if args.rebuild:
        vector_store.delete_collection()
        vector_store.create_collection()
        forget_embedding_model(vector_store.collection_name)

    index = PolicyIndex(vector_store)
    knowledge = KnowledgeBase(get_engine(), index, config.TENANT_ID)
    documents = []
    for file in sorted(POLICY_DIR.glob("*.md")):
        text = file.read_text(encoding="utf-8")
        documents.append(DocumentChange(file.name, text, chunk_text(text, file.name)))

    all_chunks = [chunk for document in documents for chunk in document.chunks]
    orphans = sources_without_a_file(index.stored_chunks(), all_chunks)
    plan, changed = plan_documents(index, documents)
    print(f"{len(documents)} documents, {len(changed)} changed: {plan.summary()}")
    if orphans and not args.prune:
        # A policy uploaded on the admin screen may exist only in the database, so it's kept
        print(f"Kept {len(orphans)} stored policies with no file here: {', '.join(orphans)}")
        print("Use --prune to remove them.")
    elif orphans:
        stored = index.stored_chunks()
        plan.stale_ids += [chunk_id for chunk_id, chunk in stored.items() if chunk.metadata["source"] in orphans]
    knowledge.archive_before_change(plan, index.stored_chunks())
    apply_documents(index, plan, changed)
    for document in documents:  # gives each policy a version, which uses no embeddings
        if document in changed or not knowledge.current_number(document.source_file):
            knowledge.record_baseline(document.source_file, document.chunks, document.text)
    if args.prune:
        for orphan in orphans:
            index.delete_document(orphan)
    print(f"Indexing finished for {config.TENANT_ID}")


if __name__ == "__main__":
    main()
