"""Change detection and the admin endpoints, using a fake index so no API or database is used."""
import pytest
from fastapi.testclient import TestClient
from langchain_core.documents import Document

from app import admin, config
from app.main import app
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
HEADERS = {"X-Admin-Key": "admin-key"}


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


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ADMIN_KEY", "admin-key")
    monkeypatch.setattr(admin, "POLICY_DIR", tmp_path)
    index = FakeIndex(chunk_text(POLICY, "Existing.md"))
    app.state.service = type("Service", (), {"store": index})()
    app.state.count_chunks = lambda: 3
    with TestClient(app) as test_client:
        test_client.index = index
        test_client.folder = tmp_path
        yield test_client


@pytest.mark.parametrize("method,path", [("get", "/admin/documents"), ("delete", "/admin/documents/Existing.md")])
def test_admin_needs_the_admin_key(client, method, path):
    assert getattr(client, method)(path).status_code == 401
    assert getattr(client, method)(path, headers={"X-Admin-Key": "wrong"}).status_code == 401
    assert getattr(client, method)(path, headers={"X-API-Key": "admin-key"}).status_code == 401
    assert len(client.index.chunks) == 3


def test_admin_is_off_without_a_key_set(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_KEY", "")
    assert client.get("/admin/documents", headers=HEADERS).status_code == 503


def test_list_documents_counts_chunks(client):
    assert client.get("/admin/documents", headers=HEADERS).json() == [
        {"file": "Existing.md", "policy": "Sample Policy", "chunks": 3}]


def test_upload_adds_a_policy_and_saves_a_copy(client):
    response = client.post("/admin/documents", headers=HEADERS, json={"filename": "New_Policy.md", "content": POLICY})
    assert response.json() == {"file": "New_Policy.md", "added": 3, "updated": 0, "relabelled": 0, "removed": 0, "unchanged": 0}
    assert (client.folder / "New_Policy.md").read_text(encoding="utf-8") == POLICY
    assert len(client.index.chunks) == 6


def test_replacing_a_policy_embeds_only_changed_sections(client):
    edited = POLICY.replace("Follow the rules.", "Follow the new rules.").replace("## 1. Purpose\n\nWhy this policy exists.\n\n", "")
    response = client.post("/admin/documents", headers=HEADERS, json={"filename": "Existing.md", "content": edited})
    assert response.json() == {"file": "Existing.md", "added": 0, "updated": 1, "relabelled": 0, "removed": 1, "unchanged": 1}
    assert client.index.embedded == ["Existing > 2. Rules > 0"]


@pytest.mark.parametrize("filename", ["../evil.md", "a/b.md", "notes.txt", ".md", "C:evil.md", "a b.md"])
def test_unsafe_file_names_are_rejected(client, filename):
    response = client.post("/admin/documents", headers=HEADERS, json={"filename": filename, "content": POLICY})
    assert response.status_code == 422
    assert client.index.embedded == []


@pytest.mark.parametrize("content", ["No headings here.", "## 1. Starts too low\n\nText.", "# Title\x00\n\n## 1. A\n\nText."])
def test_bad_content_is_rejected(client, content):
    response = client.post("/admin/documents", headers=HEADERS, json={"filename": "Bad.md", "content": content})
    assert response.status_code == 422


def test_title_with_no_sections_is_rejected(client):
    response = client.post("/admin/documents", headers=HEADERS, json={"filename": "Bad.md", "content": "# Only a title\n"})
    assert response.status_code == 422


def test_too_large_upload_is_rejected(client):
    response = client.post("/admin/documents", headers=HEADERS,
                           json={"filename": "Big.md", "content": "x" * (config.MAX_UPLOAD_CHARS + 1)})
    assert response.status_code == 422


def test_upload_needing_too_many_embeddings_is_refused_before_any_are_used(client):
    sections = "".join(f"## {number}. Section\n\nText {number}.\n\n" for number in range(BATCH_SIZE + 1))
    response = client.post("/admin/documents", headers=HEADERS, json={"filename": "Big.md", "content": f"# Big\n\n{sections}"})
    assert response.status_code == 413
    assert client.index.embedded == []


def test_failed_embedding_changes_nothing_and_saves_no_file(client):
    client.index.fail = True
    response = client.post("/admin/documents", headers=HEADERS, json={"filename": "New.md", "content": POLICY})
    assert response.status_code == 503
    assert "New.md" not in {c.metadata["source"] for c in client.index.chunks.values()}
    assert not (client.folder / "New.md").exists()


def test_delete_removes_the_chunks_and_the_file(client):
    (client.folder / "Existing.md").write_text(POLICY, encoding="utf-8")
    response = client.delete("/admin/documents/Existing.md", headers=HEADERS)
    assert response.json()["removed"] == 3
    assert client.index.chunks == {}
    assert "Existing.md" not in client.index.documents
    assert not (client.folder / "Existing.md").exists()


def test_deleting_an_unknown_policy_gives_404(client):
    assert client.delete("/admin/documents/Unknown.md", headers=HEADERS).status_code == 404
