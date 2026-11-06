"""The real SQL against a real PostgreSQL with pgvector. No model API is called.

See conftest.py for which database is used. It never touches the real data.
"""
import psycopg
import pytest
from sqlalchemy import text

from app import config
from app.rag.retrieval import SearchFilters, keep_usable, select_chunks
from app.rag.store import PolicyIndex, get_store, run_migrations
from pipeline.chunk_documents import chunk_text
from pipeline.embed_and_store import DocumentChange, apply_documents, plan_documents
from tests.integration.conftest import WordEmbeddings

pytestmark = pytest.mark.integration

LEAVE = """# Leave Policy

**Version:** 2.0 | **Effective Date:** 1 April 2026 | **Next Review:** 1 April 2027

**At a Glance:** Confirmed employees get eighteen days of earned leave each calendar year, and unused days carry over.

## 1. Earned Leave

All confirmed employees are entitled to eighteen days of earned leave per calendar year under this policy.

## 2. Loss of Pay

Absence without approved leave is treated as LOP, which means the salary for those days is not paid out.
"""
SAFETY = """# Asset Policy

**Version:** 1.0 | **Effective Date:** 1 June 2027 | **Next Review:** 1 June 2028

## 1. Stolen Laptop

If a company laptop is stolen you must file an FIR with the police within forty eight hours of the theft.
"""


@pytest.fixture
def index(database_uri):
    run_migrations(database_uri)
    with psycopg.connect(database_uri, autocommit=True) as connection:
        connection.execute("DELETE FROM policy_documents")
    store = get_store("test_collection", database_uri, WordEmbeddings())
    policy_index = PolicyIndex(store)
    documents = [DocumentChange(name, body, chunk_text(body, name))
                 for name, body in [("Leave_Policy.md", LEAVE), ("IT_Asset_and_Equipment_Policy.md", SAFETY)]]
    plan, changed = plan_documents(policy_index, documents)
    apply_documents(policy_index, plan, changed, wait_seconds=0)
    yield policy_index
    store.delete_collection()


def search(index, question, **filters):
    vector = index.embeddings.embed_query(question)
    return index.candidates(vector, question, 5, filters=SearchFilters(tenant_id=config.TENANT_ID, **filters))


def test_migrations_create_the_table_with_a_tenant_column(index, database_uri):
    with psycopg.connect(database_uri) as connection:
        columns = {row[0] for row in connection.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'policy_documents'").fetchall()}
    assert {"tenant_id", "content_hash", "effective_date", "category"} <= columns


def test_migrations_are_safe_to_run_twice(index, database_uri):
    run_migrations(database_uri)


def test_document_records_are_saved(index, database_uri):
    with psycopg.connect(database_uri) as connection:
        row = connection.execute(
            "SELECT tenant_id, title, category, version, effective_date::text, chunk_count FROM policy_documents "
            "WHERE source_file = 'Leave_Policy.md'").fetchone()
    assert row == (config.TENANT_ID, "Leave Policy", "leave", "2.0", "2026-04-01", 3)


def test_vector_and_keyword_searches_both_rank_results(index):
    found = {c.chunk.id: c for c in search(index, "file an FIR")}
    fir = found["IT_Asset_and_Equipment_Policy > 1. Stolen Laptop > 0"]
    assert fir.vector_rank == 1
    assert fir.keyword_rank == 1
    assert fir.distance < 1


def test_keyword_search_matches_any_word_not_all(index):
    found = search(index, "LOP zebra unknownword")
    assert any(c.keyword_rank == 1 and "2. Loss of Pay" in c.chunk.id for c in found)


def test_question_of_only_common_words_finds_no_keywords(index):
    assert all(c.keyword_rank is None for c in search(index, "what is the"))


@pytest.mark.parametrize("filters,expected_source", [
    ({"category": "leave"}, "Leave_Policy.md"),
    ({"policy": "Asset Policy"}, "IT_Asset_and_Equipment_Policy.md"),
    ({"effective_on": "2026-12-31"}, "Leave_Policy.md"),  # the asset policy isn't in effect yet
])
def test_filters_limit_both_searches(index, filters, expected_source):
    found = search(index, "laptop stolen FIR leave", **filters)
    assert found
    assert {c.chunk.metadata["source"] for c in found} == {expected_source}


def test_filter_values_are_parameters_not_sql(index):
    assert search(index, "leave", category="x' OR '1'='1") == []


def test_another_tenant_finds_nothing(index):
    vector = index.embeddings.embed_query("leave")
    assert index.candidates(vector, "leave", 5, filters=SearchFilters(tenant_id="another-tenant")) == []


def test_confidence_is_higher_for_the_best_match(index):
    found = sorted(search(index, "file an FIR with the police"), key=lambda c: c.distance)
    assert found[0].confidence > found[-1].confidence


def test_results_work_with_filtering_and_selection(index, monkeypatch):
    monkeypatch.setattr(config, "ABSTENTION_MODE", "distance")
    monkeypatch.setattr(config, "MAX_DISTANCE", 1.0)
    monkeypatch.setattr(config, "MIN_BODY_CHARS", 10)
    chunks = select_chunks(keep_usable(search(index, "file an FIR with the police")))
    assert chunks[0].id == "IT_Asset_and_Equipment_Policy > 1. Stolen Laptop > 0"


def test_full_text_index_exists(index, database_uri):
    with psycopg.connect(database_uri) as connection:
        names = [row[0] for row in connection.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'langchain_pg_embedding'").fetchall()]
    assert "ix_embedding_full_text" in names


def test_edit_only_changes_what_changed_and_removes_renamed_sections(index):
    edited = LEAVE.replace("## 2. Loss of Pay", "## 2. Unpaid Absence")
    plan, changed = plan_documents(index, [DocumentChange("Leave_Policy.md", edited, chunk_text(edited, "Leave_Policy.md"))])
    assert [c.id for c in plan.new] == ["Leave_Policy > 2. Unpaid Absence > 0"]
    assert plan.edited == []
    assert plan.stale_ids == ["Leave_Policy > 2. Loss of Pay > 0"]
    apply_documents(index, plan, changed, wait_seconds=0)
    stored = index.stored_chunks()
    assert "Leave_Policy > 2. Loss of Pay > 0" not in stored
    assert "Leave_Policy > 2. Unpaid Absence > 0" in stored


def test_unchanged_documents_are_skipped_by_hash(index):
    plan, changed = plan_documents(index, [DocumentChange("Leave_Policy.md", LEAVE, chunk_text(LEAVE, "Leave_Policy.md"))])
    assert changed == []
    assert plan.to_embed == []


def test_relabelling_updates_metadata_without_touching_vectors(index):
    chunk = chunk_text(LEAVE, "Leave_Policy.md")[0]
    chunk.metadata["category"] = "changed"
    with index.store.session_maker() as session:
        before = session.execute(text("SELECT embedding::text FROM langchain_pg_embedding WHERE id = :id"), {"id": chunk.id}).scalar()
    index.update_metadata([chunk])
    assert index.stored_chunks()[chunk.id].metadata["category"] == "changed"
    with index.store.session_maker() as session:
        after = session.execute(text("SELECT embedding::text FROM langchain_pg_embedding WHERE id = :id"), {"id": chunk.id}).scalar()
    assert before == after
