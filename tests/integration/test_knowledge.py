"""Uploads, versions, jobs and reports through the API, on a real PostgreSQL with fake embeddings."""
from datetime import date
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import config
from app.db import get_engine
from app.knowledge import KnowledgeBase, UploadDetails, UploadRejected
from app.main import app
from app.memory import ConversationStore
from app.rag.prompt import build_messages
from app.rag.retrieval import SearchFilters
from app.rag.store import EmbeddingModelMismatch, PolicyIndex, get_store, run_migrations
from tests.integration.conftest import WordEmbeddings

pytestmark = pytest.mark.integration

HEADERS = {"X-Admin-Key": "admin-key"}
USER = {"X-API-Key": "test-key", "X-User-Id": "E1001"}
V1 = b"""# Leave Policy

**Version:** 1.0 | **Effective Date:** 1 April 2025 | **Next Review:** 1 April 2026

## 1. Earned Leave

All confirmed employees are entitled to fifteen days of earned leave per calendar year.

## 2. Sick Leave

All employees are entitled to twelve days of sick leave per calendar year under this policy.

## 3. Old Section

This older section about carry over rules is removed from the next version of the policy.
"""
V2 = V1.replace(b"fifteen days", b"eighteen days").replace(
    b"## 3. Old Section\n\nThis older section about carry over rules is removed from the next version of the policy.\n", b""
).replace(b"Version:** 1.0", b"Version:** 2.0").replace(b"1 April 2025", b"1 April 2026")


class CountingEmbeddings(WordEmbeddings):
    def __init__(self):
        self.embedded: list[str] = []
        self.fail_after: int | None = None

    def embed_documents(self, texts):
        if self.fail_after is not None and len(self.embedded) >= self.fail_after:
            raise RuntimeError("quota secret detail")
        self.embedded += texts
        return super().embed_documents(texts)


@pytest.fixture(scope="module")
def engine(database_uri):
    run_migrations(database_uri)
    return get_engine(database_uri)


@pytest.fixture
def setup(engine, database_uri, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_KEY", "admin-key")
    monkeypatch.setattr(config, "API_KEY", "test-key")
    monkeypatch.setattr(config, "EMBEDDING_WAIT_SECONDS", 0)
    with engine.begin() as connection:
        for table in ("indexing_jobs", "document_versions", "policy_documents", "feedback", "unanswered_questions",
                      "embedding_models", "messages", "conversations"):
            connection.execute(text(f"DELETE FROM {table}"))
    embeddings = CountingEmbeddings()
    store = get_store("knowledge_test", database_uri, embeddings)
    index = PolicyIndex(store)
    knowledge = KnowledgeBase(engine, index, config.TENANT_ID, tmp_path)
    conversations = ConversationStore(engine, config.TENANT_ID)
    app.state.service = SimpleNamespace(knowledge=knowledge, store=index, conversations=conversations)
    app.state.count_chunks = lambda: 1
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, knowledge=knowledge, index=index, embeddings=embeddings, store=store,
                              folder=tmp_path, engine=engine, conversations=conversations, uri=database_uri)
    store.delete_collection()


def upload(s, data=V1, name="Leave_Policy.md", **fields):
    return s.client.post("/admin/documents", headers=HEADERS, files={"file": (name, data)},
                         data={"uploaded_by": "Asha", **fields})


def search(s, question, **filters):
    vector = s.embeddings.embed_query(question)
    return s.index.candidates(vector, question, 10, filters=SearchFilters(tenant_id=config.TENANT_ID, **filters))


# Uploading

@pytest.mark.parametrize("method,path", [("get", "/admin/documents"), ("get", "/admin/jobs"), ("get", "/admin/unanswered"),
                                         ("get", "/admin/feedback"), ("get", "/admin/documents/x.md/versions"),
                                         ("delete", "/admin/documents/x.md"), ("post", "/admin/jobs/x/retry")])
def test_admin_routes_need_the_admin_key(setup, method, path):
    call = getattr(setup.client, method)
    assert call(path).status_code == 401
    assert call(path, headers={"X-Admin-Key": "wrong"}).status_code == 401
    assert call(path, headers={"X-API-Key": "admin-key"}).status_code == 401


def test_upload_needs_the_admin_key(setup):
    assert setup.client.post("/admin/documents", files={"file": ("a.md", V1)}).status_code == 401


def test_admin_is_off_without_a_key_set(setup, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_KEY", "")
    assert setup.client.get("/admin/documents", headers=HEADERS).status_code == 503


def test_upload_returns_a_job_and_indexes_the_policy(setup):
    response = upload(setup)
    assert response.status_code == 202
    body = response.json()
    assert (body["status"], body["version"], body["file"]) == ("queued", 1, "Leave_Policy.md")
    job = setup.client.get(f"/admin/jobs/{body['job_id']}", headers=HEADERS).json()
    assert (job["status"], job["embedded"], job["to_embed"], job["attempts"]) == ("succeeded", 3, 3, 1)
    assert len(setup.index.stored_chunks()) == 3
    assert (setup.folder / "Leave_Policy.md").exists()


def test_upload_details_reach_the_chunks_and_the_version_list(setup):
    upload(setup, title="Time Off", category="leave", version_label="9.9", effective_date="2027-01-15")
    chunk = next(iter(setup.index.stored_chunks().values()))
    assert (chunk.metadata["policy"], chunk.metadata["category"], chunk.metadata["version"], chunk.metadata["effective_date"]) == (
        "Time Off", "leave", "9.9", "2027-01-15")
    version = setup.client.get("/admin/documents/Leave_Policy.md/versions", headers=HEADERS).json()[0]
    assert (version["title"], version["uploaded_by"], version["status"]) == ("Time Off", "Asha", "current")


def test_any_format_becomes_the_same_policy_name(setup):
    upload(setup, b"1. Rules\nEmployees get fifteen days of leave every single calendar year.", "Leave_Policy.txt")
    assert setup.client.get("/admin/documents", headers=HEADERS).json()[0]["file"] == "Leave_Policy.md"


@pytest.mark.parametrize("name", ["../evil.md", "a b.md", ".md", "C:evil.md"])
def test_unsafe_names_are_refused(setup, name):
    response = upload(setup, name=name)
    assert response.status_code == 422
    assert setup.embeddings.embedded == []


def test_unsupported_type_is_refused(setup):
    assert "Please upload one of" in upload(setup, name="virus.exe").json()["detail"]


def test_unknown_category_is_refused(setup):
    assert "Category must be" in upload(setup, category="made-up").json()["detail"]


def test_too_large_file_is_refused(setup, monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 10)
    assert "larger than" in upload(setup).json()["detail"]


def test_too_much_text_is_refused(setup, monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_CHARS", 10)
    assert "characters" in upload(setup).json()["detail"]


def test_bad_date_is_refused(setup):
    assert upload(setup, effective_date="soon").status_code == 422


# Duplicates

def test_same_file_again_changes_nothing(setup):
    upload(setup)
    setup.embeddings.embedded.clear()
    body = upload(setup).json()
    assert (body["status"], body["job_id"], body["version"]) == ("unchanged", None, 1)
    assert setup.embeddings.embedded == []


def test_same_text_under_another_name_is_refused(setup):
    upload(setup)
    response = upload(setup, name="Copy_Of_Leave.md")
    assert response.status_code == 409
    assert "Leave_Policy.md" in response.json()["detail"]


def test_changing_only_the_details_makes_a_new_version_without_embedding(setup):
    upload(setup)
    setup.embeddings.embedded.clear()
    body = upload(setup, category="conduct").json()
    assert body["version"] == 2
    assert setup.embeddings.embedded == []
    assert next(iter(setup.index.stored_chunks().values())).metadata["category"] == "conduct"


# Versions

def test_only_changed_sections_are_embedded_and_old_text_is_kept(setup):
    upload(setup)
    setup.embeddings.embedded.clear()
    body = upload(setup, V2).json()
    assert body["version"] == 2
    assert len(setup.embeddings.embedded) == 1  # only Earned Leave changed
    current = setup.index.stored_chunks()
    assert "Leave_Policy > 3. Old Section > 0" not in current
    assert "eighteen" in current["Leave_Policy > 1. Earned Leave > 0"].page_content
    versions = setup.client.get("/admin/documents/Leave_Policy.md/versions", headers=HEADERS).json()
    assert [(v["version"], v["status"]) for v in versions] == [(2, "current"), (1, "superseded")]


def test_normal_search_only_finds_the_current_version(setup):
    upload(setup)
    upload(setup, V2)
    found = search(setup, "earned leave days fifteen")
    assert all(c.chunk.metadata.get("is_current") != "false" for c in found)
    assert not any("fifteen" in c.chunk.page_content for c in found)


def test_history_search_finds_the_old_version_marked_as_superseded(setup):
    upload(setup)
    upload(setup, V2)
    old = next(c for c in search(setup, "earned leave days fifteen", include_history=True)
               if "fifteen" in c.chunk.page_content)
    assert old.chunk.id == "Leave_Policy > 1. Earned Leave > 0 @v1"
    assert old.chunk.metadata["version_number"] == 1
    prompt = build_messages("What was it before?", [old.chunk])[1].content
    assert "(superseded: version 1, replaced on" in prompt


def test_effective_on_answers_as_of_a_date(setup):
    upload(setup)
    upload(setup, V2)
    today = date.today().isoformat()
    past = search(setup, "earned leave", effective_on="2025-06-01")
    assert {c.chunk.metadata.get("effective_date") for c in past} == {"2025-04-01"}
    assert any("fifteen" in c.chunk.page_content for c in past)
    assert not any("fifteen" in c.chunk.page_content for c in search(setup, "earned leave", effective_on=today))


def test_removed_policy_stops_answering_but_keeps_history(setup):
    upload(setup)
    response = setup.client.delete("/admin/documents/Leave_Policy.md", headers=HEADERS).json()
    assert response["removed"] == 3
    assert setup.index.stored_chunks() == {}
    assert search(setup, "earned leave") == []
    assert search(setup, "earned leave", include_history=True)
    assert not (setup.folder / "Leave_Policy.md").exists()


def test_purge_removes_everything(setup):
    upload(setup)
    setup.client.delete("/admin/documents/Leave_Policy.md?purge=true", headers=HEADERS)
    assert search(setup, "earned leave", include_history=True) == []
    assert setup.client.get("/admin/documents/Leave_Policy.md/versions", headers=HEADERS).status_code == 404


def test_removing_an_unknown_policy_gives_404(setup):
    assert setup.client.delete("/admin/documents/Nope.md", headers=HEADERS).status_code == 404


def test_policy_indexed_before_versions_gets_a_baseline_when_replaced(setup):
    from pipeline.chunk_documents import chunk_text

    setup.index.add(chunk_text(V1.decode(), "Leave_Policy.md"))
    body = upload(setup, V2).json()
    assert body["version"] == 2
    versions = setup.client.get("/admin/documents/Leave_Policy.md/versions", headers=HEADERS).json()
    assert [v["version"] for v in versions] == [2, 1]
    assert any("fifteen" in c.chunk.page_content for c in search(setup, "earned leave fifteen", include_history=True))


def test_search_counts_and_stored_chunks_skip_old_versions(setup):
    from app.rag.store import count_chunks

    upload(setup)
    upload(setup, V2)
    assert count_chunks("knowledge_test", setup.uri) == 2
    assert len(setup.index.stored_chunks()) == 2


# Jobs

def test_failed_job_is_reported_cleanly_and_the_old_text_stays(setup):
    upload(setup)
    setup.embeddings.fail_after = len(setup.embeddings.embedded)
    body = upload(setup, V2).json()
    job = setup.client.get(f"/admin/jobs/{body['job_id']}", headers=HEADERS).json()
    assert job["status"] == "failed"
    assert job["can_retry"]
    assert "secret" not in job["error"]
    assert "fifteen" in setup.index.stored_chunks()["Leave_Policy > 1. Earned Leave > 0"].page_content
    versions = setup.client.get("/admin/documents/Leave_Policy.md/versions", headers=HEADERS).json()
    assert [(v["version"], v["status"]) for v in versions] == [(2, "pending"), (1, "current")]


def test_retry_finishes_the_job(setup):
    setup.embeddings.fail_after = 0
    body = upload(setup).json()
    setup.embeddings.fail_after = None
    response = setup.client.post(f"/admin/jobs/{body['job_id']}/retry", headers=HEADERS)
    assert response.status_code == 202
    job = setup.client.get(f"/admin/jobs/{body['job_id']}", headers=HEADERS).json()
    assert (job["status"], job["attempts"], job["can_retry"]) == ("succeeded", 2, False)
    assert len(setup.index.stored_chunks()) == 3


def test_retry_resumes_rather_than_starting_again(setup, monkeypatch):
    monkeypatch.setattr("pipeline.embed_and_store.BATCH_SIZE", 1)
    setup.embeddings.fail_after = 2
    body = upload(setup).json()
    assert setup.client.get(f"/admin/jobs/{body['job_id']}", headers=HEADERS).json()["status"] == "failed"
    done = list(setup.embeddings.embedded)
    setup.embeddings.fail_after = None
    setup.client.post(f"/admin/jobs/{body['job_id']}/retry", headers=HEADERS)
    assert len(done) == 2
    assert len(setup.embeddings.embedded) == 3  # only the third section was embedded on retry
    assert len(setup.index.stored_chunks()) == 3


def test_only_failed_jobs_can_be_retried(setup):
    body = upload(setup).json()
    assert setup.client.post(f"/admin/jobs/{body['job_id']}/retry", headers=HEADERS).status_code == 409
    assert setup.client.post("/admin/jobs/unknown/retry", headers=HEADERS).status_code == 404


def test_a_replaced_failed_job_cant_be_retried(setup):
    setup.embeddings.fail_after = 0
    first = upload(setup).json()
    setup.embeddings.fail_after = None
    upload(setup, V2)
    assert setup.client.post(f"/admin/jobs/{first['job_id']}/retry", headers=HEADERS).status_code == 409


def test_a_second_upload_waits_for_a_running_job(setup):
    created = setup.knowledge.add_upload(V1, "Leave_Policy.md", UploadDetails(uploaded_by="Asha"))
    assert created["status"] == "queued"
    with pytest.raises(UploadRejected, match="already being indexed"):
        setup.knowledge.add_upload(V2, "Leave_Policy.md", UploadDetails(uploaded_by="Asha"))
    setup.knowledge.run_job(created["job_id"])


def test_a_restart_marks_unfinished_jobs_as_failed_so_they_can_be_retried(setup):
    created = setup.knowledge.add_upload(V1, "Leave_Policy.md", UploadDetails(uploaded_by="Asha"))
    assert setup.knowledge.fail_interrupted() == 1
    job = setup.client.get(f"/admin/jobs/{created['job_id']}", headers=HEADERS).json()
    assert job["status"] == "failed"
    assert "restarted" in job["error"]
    assert job["can_retry"]


def test_another_tenant_sees_no_documents_or_jobs(setup):
    upload(setup)
    other = KnowledgeBase(setup.engine, setup.index, "other-tenant")
    assert other.jobs() == []
    assert other.current_number("Leave_Policy.md") == 0
    assert other.unanswered_report() == []


# Embedding model safeguards

def test_collection_records_its_embedding_model(setup):
    upload(setup)
    with setup.engine.connect() as connection:
        row = connection.execute(text("SELECT provider, model, dimension FROM embedding_models")).one()
    assert (row.provider, row.model, row.dimension) == (config.EMBEDDING_PROVIDER, config.EMBEDDING_MODEL, 64)


def test_another_embedding_model_is_refused_at_startup(setup, monkeypatch):
    upload(setup)
    monkeypatch.setattr(config, "EMBEDDING_MODEL", "another-model")
    with pytest.raises(EmbeddingModelMismatch, match="re-index"):
        PolicyIndex(setup.store)


def test_vectors_of_another_size_are_refused_before_storing(setup):
    upload(setup)
    setup.embeddings.DIMENSIONS = 32
    body = upload(setup, V2).json()
    assert setup.client.get(f"/admin/jobs/{body['job_id']}", headers=HEADERS).json()["status"] == "failed"
    assert "fifteen" in setup.index.stored_chunks()["Leave_Policy > 1. Earned Leave > 0"].page_content


def test_existing_collection_is_recorded_from_its_vectors(setup):
    upload(setup)
    with setup.engine.begin() as connection:
        connection.execute(text("DELETE FROM embedding_models"))
    PolicyIndex(setup.store)
    with setup.engine.connect() as connection:
        assert connection.execute(text("SELECT dimension FROM embedding_models")).scalar() == 64


# Feedback and unanswered questions

def stored_answer(s, user="E1001"):
    chat = s.conversations.start(user)
    return s.conversations.add_message(user, chat, "assistant", "An answer about leave.")


def test_feedback_is_stored_and_replaced(setup):
    message = stored_answer(setup)
    assert setup.client.post("/feedback", headers=USER, json={"message_id": message, "rating": 1}).json() == {"saved": True}
    setup.client.post("/feedback", headers=USER, json={"message_id": message, "rating": -1, "comment": "Wrong"})
    summary = setup.client.get("/admin/feedback", headers=HEADERS).json()
    assert (summary["thumbs_up"], summary["thumbs_down"]) == (0, 1)
    assert summary["recent_negative"][0]["comment"] == "Wrong"


def test_someone_else_cant_rate_an_answer(setup):
    message = stored_answer(setup, "E1002")
    assert setup.client.post("/feedback", headers=USER, json={"message_id": message, "rating": 1}).status_code == 404


def test_a_question_message_cant_be_rated(setup):
    chat = setup.conversations.start("E1001")
    message = setup.conversations.add_message("E1001", chat, "user", "A question")
    assert setup.client.post("/feedback", headers=USER, json={"message_id": message, "rating": 1}).status_code == 404


@pytest.mark.parametrize("rating", [0, 2, -5])
def test_ratings_are_only_up_or_down(setup, rating):
    assert setup.client.post("/feedback", headers=USER, json={"message_id": 1, "rating": rating}).status_code == 422


def test_feedback_needs_a_user(setup):
    headers = {"X-API-Key": "test-key"}
    assert setup.client.post("/feedback", headers=headers, json={"message_id": 1, "rating": 1}).status_code == 422


def test_unanswered_questions_are_grouped_with_no_user_stored(setup):
    for question in ("Is there a gym?", "is there a gym?", "Free lunch?"):
        setup.knowledge.record_unanswered(question, "no_evidence")
    report = setup.client.get("/admin/unanswered", headers=HEADERS).json()
    assert (report[0]["question"], report[0]["asked"]) == ("is there a gym?", 2)
    with psycopg.connect(setup.uri) as connection:
        columns = {row[0] for row in connection.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'unanswered_questions'").fetchall()}
    assert "user_id" not in columns


def test_old_unanswered_questions_are_removed(setup):
    setup.knowledge.record_unanswered("Old question", "no_evidence")
    with setup.engine.begin() as connection:
        connection.execute(text("UPDATE unanswered_questions SET created_at = now() - interval '100 days'"))
    setup.knowledge.purge_unanswered_older_than(90)
    assert setup.client.get("/admin/unanswered", headers=HEADERS).json() == []
