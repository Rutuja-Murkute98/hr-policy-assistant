"""Change detection, using a fake index so no API or database is used."""
import pytest
from langchain_core.documents import Document

from pipeline.chunk_documents import chunk_text
from pipeline.embed_and_store import (
    BATCH_SIZE,
    DocumentChange,
    apply_documents,
    content_hash,
    plan_documents,
    plan_sync,
    run_sync,
    sources_without_a_file,
)

POLICY = """# Sample Policy

**At a Glance:** A short summary of the sample policy.

## 1. Purpose

Why this policy exists.

## 2. Rules

Follow the rules.
"""


class FakeIndex:
    def __init__(self, chunks=()):
        self.chunks = {chunk.id: chunk for chunk in chunks}
        self.embedded = []
        self.relabelled = []
        self.documents = {}
        self.fail = False

    def stored_chunks(self):
        return dict(self.chunks)

    def add(self, chunks):
        if self.fail:
            raise RuntimeError("quota")
        self.embedded += [chunk.id for chunk in chunks]
        self.chunks.update({chunk.id: chunk for chunk in chunks})

    def delete(self, ids):
        for chunk_id in ids:
            del self.chunks[chunk_id]

    def update_metadata(self, chunks):
        self.relabelled += [chunk.id for chunk in chunks]
        self.chunks.update({chunk.id: chunk for chunk in chunks})

    def document_hashes(self):
        return {name: record.content_hash for name, record in self.documents.items()}

    def save_document(self, record):
        self.documents[record.source_file] = record

    def delete_document(self, source_file):
        self.documents.pop(source_file, None)


def chunk(chunk_id, text="text", source="A.md"):
    return Document(id=chunk_id, page_content=text, metadata={"source": source, "policy": source, "section": chunk_id})


def test_plan_finds_new_edited_unchanged_and_stale_chunks():
    stored = {c.id: c for c in [chunk("same"), chunk("edited", "old"), chunk("gone")]}
    plan = plan_sync(stored, [chunk("same"), chunk("edited", "new"), chunk("added")], {"A.md"})
    assert [c.id for c in plan.new] == ["added"]
    assert [c.id for c in plan.edited] == ["edited"]
    assert plan.stale_ids == ["gone"]
    assert plan.summary() == {"added": 1, "updated": 1, "relabelled": 0, "removed": 1, "unchanged": 1}


def test_plan_leaves_other_policies_alone():
    stored = {"other": chunk("other", source="B.md")}
    assert plan_sync(stored, [chunk("a")], {"A.md"}).stale_ids == []


def test_run_sync_embeds_only_what_changed():
    index = FakeIndex([chunk("same"), chunk("edited", "old"), chunk("gone")])
    run_sync(index, plan_sync(index.stored_chunks(), [chunk("same"), chunk("edited", "new")], {"A.md"}))
    assert index.embedded == ["edited"]
    assert index.chunks["edited"].page_content == "new"
    assert set(index.chunks) == {"same", "edited"}


def test_failed_embedding_keeps_the_old_text_and_removes_nothing():
    index = FakeIndex([chunk("edited", "old"), chunk("gone")])
    index.fail = True
    with pytest.raises(RuntimeError):
        run_sync(index, plan_sync(index.stored_chunks(), [chunk("edited", "new")], {"A.md"}))
    assert index.chunks["edited"].page_content == "old"
    assert "gone" in index.chunks


def test_run_sync_waits_between_batches(monkeypatch):
    waits = []
    monkeypatch.setattr("pipeline.embed_and_store.time.sleep", waits.append)
    chunks = [chunk(f"c{number}") for number in range(BATCH_SIZE + 1)]
    run_sync(FakeIndex(), plan_sync({}, chunks, {"A.md"}), wait_seconds=7)
    assert waits == [7]


def test_same_text_with_new_details_is_relabelled_without_embedding():
    old = chunk("a", "same")
    new = Document(id="a", page_content="same", metadata={**old.metadata, "category": "leave"})
    index = FakeIndex([old])
    plan = plan_sync(index.stored_chunks(), [new], {"A.md"})
    run_sync(index, plan)
    assert index.embedded == []
    assert index.relabelled == ["a"]
    assert index.chunks["a"].metadata["category"] == "leave"


def document(text=POLICY, name="Doc.md"):
    return DocumentChange(name, text, chunk_text(text, name))


def test_unchanged_document_is_skipped_by_its_hash():
    index = FakeIndex()
    plan, changed = plan_documents(index, [document()])
    apply_documents(index, plan, changed)
    assert index.embedded and index.documents["Doc.md"].content_hash == content_hash(POLICY, "general")

    index.embedded.clear()
    plan, changed = plan_documents(index, [document()])
    assert changed == []
    assert plan.to_embed == []
    assert plan.unchanged == 3


def test_renamed_section_replaces_the_old_chunk():
    index = FakeIndex()
    plan, changed = plan_documents(index, [document()])
    apply_documents(index, plan, changed)

    renamed = POLICY.replace("## 2. Rules", "## 2. New Rules")
    plan, changed = plan_documents(index, [document(renamed)])
    apply_documents(index, plan, changed)
    assert "Doc > 2. New Rules > 0" in index.chunks
    assert "Doc > 2. Rules > 0" not in index.chunks
    assert index.documents["Doc.md"].content_hash == content_hash(renamed, "general")


def test_interrupted_run_is_not_recorded_so_it_runs_again():
    index = FakeIndex()
    index.fail = True
    plan, changed = plan_documents(index, [document()])
    with pytest.raises(RuntimeError):
        apply_documents(index, plan, changed)
    assert index.documents == {}


def test_policies_without_a_file_are_found():
    stored = {"a": chunk("a"), "b": chunk("b", source="Uploaded.md")}
    assert sources_without_a_file(stored, [chunk("a")]) == ["Uploaded.md"]


def test_chunk_text_uses_the_file_name_for_ids_and_source():
    chunks = chunk_text(POLICY, "Sample.md")
    assert chunks[1].id == "Sample > 1. Purpose > 0"
    assert chunks[1].metadata["source"] == "Sample.md"
