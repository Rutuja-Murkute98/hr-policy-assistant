"""Applying a new embedding model to an organisation's chunks, on a real PostgreSQL. No model API is called.

The old collection's vectors come from one fake embedder, and the new one's from another with a different size, as
two real models would be.
"""
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app import reindex
from app.config import Tenant
from app.db import get_engine
from app.identity import Organisations
from app.knowledge import KnowledgeBase
from app.rag.store import DocumentRecord, PolicyIndex, count_chunks, get_store, run_migrations
from app.reindex import Reindex, find_old_index, latest_run
from app.tenancy import TenantEngine
from app.workflow import WorkflowRegistry, create_checkpointer
from pipeline.chunk_documents import chunk_text
from tests.integration.conftest import WordEmbeddings
from tests.integration.test_workflow import ScriptedLLM

pytestmark = pytest.mark.integration

OLD_MODEL, NEW_MODEL = ("fake", "words-64"), ("fake", "words-32")
NORTHWIND = Tenant(id="northwind", company_name="Northwind", legal_name="Northwind Ltd",
                   hr_email="hr@northwind.example", product_name="Northwind HR", logo_initials="N")
CONTOSO = Tenant(id="contoso", company_name="Contoso", legal_name="Contoso Ltd", hr_email="hr@contoso.example",
                 product_name="Contoso HR", logo_initials="C")
POLICY = """# Leave Policy

## 1. Earned Leave

Staff get eighteen days of earned leave each year, credited monthly from the date of joining.

## 2. Sick Leave

Staff get twelve days of sick leave each year. A doctor's note is needed for more than two days in a row.

## 3. Carry Forward

Up to thirty days of earned leave can be carried into the next year. The rest lapses on 31 December.
"""
TENANTS = ("northwind", "contoso")
TABLES = ("reindex_vectors", "reindex_runs", "policy_documents", "document_versions", "indexing_jobs",
          "embedding_models")


class NewEmbeddings(WordEmbeddings):
    """The new model: a different size, and it can be made to fail after some calls."""

    DIMENSIONS = 32

    def __init__(self, fail_after: int | None = None):
        self.calls = 0
        self.fail_after = fail_after

    def embed_documents(self, texts):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise RuntimeError("quota")
        return super().embed_documents(texts)


@pytest.fixture
def world(database_uri, monkeypatch):
    run_migrations(database_uri)
    engine = get_engine(database_uri)
    clean(engine)
    old_store = get_store("reindex_old", database_uri, WordEmbeddings())
    new_store = get_store("reindex_new", database_uri, NewEmbeddings())
    organisations = Organisations(engine)
    indexes = {}
    for profile in (NORTHWIND, CONTOSO):
        organisations.ensure(profile)
        index = PolicyIndex(old_store, profile.id, model=OLD_MODEL)
        chunks = chunk_text(POLICY, "Leave_Policy.md", tenant_id=profile.id, profile=profile)
        index.add(chunks)
        base = KnowledgeBase(engine, index, profile.id, profile=profile)
        base.record_baseline("Leave_Policy.md", chunks, POLICY)
        index.save_document(DocumentRecord("Leave_Policy.md", "Leave Policy", "general", None, None, "hash", 3))
        index.archive_chunks([chunks[0].id], 1, "2026-01-01")  # an old version, which history searches use
        indexes[profile.id] = index
    monkeypatch.setattr(reindex, "BATCH_SIZE", 2)
    saver = create_checkpointer(database_uri)
    yield SimpleNamespace(engine=engine, uri=database_uri, old_store=old_store, new_store=new_store,
                          indexes=indexes, saver=saver)
    saver.conn.close()
    clean(engine)
    old_store.delete_collection()
    new_store.delete_collection()
    engine.dispose()


def clean(engine):
    with engine.begin() as connection:
        for table in TABLES:
            connection.execute(text(f"DELETE FROM {table} WHERE tenant_id IN ('northwind', 'contoso')"))
        connection.execute(text("DELETE FROM organisations WHERE id IN ('northwind', 'contoso')"))


def job(world, tenant_id="northwind", embeddings=None):
    world.new_store.embedding_function = embeddings or NewEmbeddings()
    return Reindex(world.engine, tenant_id, world.indexes[tenant_id], world.new_store, new_model=NEW_MODEL,
                   wait_seconds=0)


def chunks_in(world, collection, tenant_id="northwind"):
    with TenantEngine(world.engine, tenant_id).connect() as connection:
        return dict(connection.execute(text(
            "SELECT e.id, vector_dims(e.embedding) FROM langchain_pg_embedding e JOIN langchain_pg_collection c "
            "ON e.collection_id = c.uuid WHERE c.name = :name"), {"name": collection}).all())


def rows(world, sql, **params):
    with world.engine.connect() as connection:
        return connection.execute(text(sql), params).all()


def test_an_organisation_still_on_the_old_model_is_found(world):
    old = find_old_index(world.engine, "northwind", "reindex_new")
    assert (old.collection, old.provider, old.model) == ("reindex_old", *OLD_MODEL)
    assert find_old_index(world.engine, "northwind", "reindex_old") is None  # already on it


def test_every_chunk_moves_with_its_new_vector_and_its_id(world):
    before = chunks_in(world, "reindex_old")
    assert len(before) == 4  # three sections, and one old version
    assert job(world).claim()
    assert job(world).run() is True
    after = chunks_in(world, "reindex_new")
    assert set(after) == set(before) and set(after.values()) == {32}
    assert chunks_in(world, "reindex_old") == {}
    assert latest_run(world.engine, "northwind")["status"] == "succeeded"
    assert latest_run(world.engine, "northwind")["embedded"] == 4


def test_the_document_records_move_and_the_new_model_is_recorded(world):
    job(world).claim()
    job(world).run()
    for table in ("policy_documents", "document_versions"):
        assert {row.collection_name for row in rows(world, f"SELECT collection_name FROM {table} "
                                                           "WHERE tenant_id = 'northwind'")} == {"reindex_new"}
    models = rows(world, "SELECT collection_name, provider, model, dimension FROM embedding_models "
                         "WHERE tenant_id = 'northwind'")
    assert [tuple(row) for row in models] == [("reindex_new", *NEW_MODEL, 32)]
    assert rows(world, "SELECT * FROM reindex_vectors WHERE tenant_id = 'northwind'") == []
    base = KnowledgeBase(world.engine, PolicyIndex(world.new_store, "northwind", model=NEW_MODEL), "northwind",
                         profile=NORTHWIND)
    assert [document["file"] for document in base.documents()] == ["Leave_Policy.md"]


def test_another_organisation_is_left_alone(world):
    job(world).claim()
    job(world).run()
    assert len(chunks_in(world, "reindex_old", "contoso")) == 4
    assert chunks_in(world, "reindex_new", "contoso") == {}
    assert find_old_index(world.engine, "contoso", "reindex_new").collection == "reindex_old"


def test_a_failure_moves_nothing_and_a_retry_carries_on(world):
    failing = NewEmbeddings(fail_after=1)
    assert job(world).claim()
    assert job(world, embeddings=failing).run() is False
    run = latest_run(world.engine, "northwind")
    assert run["status"] == "failed" and run["embedded"] == 2 and "Retry" in run["error"]
    assert len(chunks_in(world, "reindex_old")) == 4 and chunks_in(world, "reindex_new") == {}
    assert not job(world).claim()  # a failed run waits for an admin
    assert reindex.queue_retry(world.engine, "northwind")
    retrying = NewEmbeddings()
    assert job(world).claim()
    assert job(world, embeddings=retrying).run() is True
    assert retrying.calls == 1  # only the 2 chunks it didn't reach, in one batch
    assert latest_run(world.engine, "northwind")["attempts"] == 2


def test_a_chunk_changed_during_the_job_gets_a_fresh_vector(world, monkeypatch):
    first = job(world)
    first.claim()
    staged_before_switch = first._switch

    def change_then_switch():
        chunk = next(iter(world.indexes["northwind"].stored_chunks().values()))
        chunk.page_content += " Updated by HR."
        world.indexes["northwind"].add([chunk])
        monkeypatch.setattr(first, "_switch", staged_before_switch)
        staged_before_switch()

    monkeypatch.setattr(first, "_switch", change_then_switch)
    assert first.run() is True
    moved = rows(world, "SELECT e.document FROM langchain_pg_embedding e JOIN langchain_pg_collection c "
                        "ON e.collection_id = c.uuid WHERE c.name = 'reindex_new' AND e.document LIKE '%Updated by HR.'")
    assert len(moved) == 1
    assert chunks_in(world, "reindex_old") == {}


def test_a_running_job_is_not_started_twice(world):
    assert job(world).claim()
    assert not job(world).claim()


def test_changing_back_to_a_model_used_before_re_indexes_again(world):
    job(world).claim()
    job(world).run()
    with world.engine.begin() as connection:
        connection.execute(text("UPDATE reindex_runs SET status = 'succeeded' WHERE tenant_id = 'northwind'"))
    assert job(world).claim()  # a finished run from an earlier change doesn't block a new one


def test_health_counts_the_old_collection_while_it_is_still_searched(world):
    job(world).claim()
    assert count_chunks("reindex_new", world.uri) >= 6  # both organisations' current chunks, still in the old one


def test_the_registry_searches_the_old_collection_then_switches(world):
    registry = WorkflowRegistry(world.new_store, ScriptedLLM(), world.engine, world.saver,
                                store_factory=lambda name, embeddings: get_store(name, world.uri, embeddings),
                                embeddings_factory=lambda provider, model: WordEmbeddings(),
                                run_in_background=False, reindex_wait_seconds=0)
    world.new_store.embedding_function = NewEmbeddings(fail_after=0)
    stuck = registry.for_tenant("northwind")
    assert stuck.rag.store.store.collection_name == "reindex_old"  # the move failed, so answers use the old model
    assert stuck.rag.store.model == OLD_MODEL
    assert len(stuck.rag.store.stored_chunks()) == 3
    world.new_store.embedding_function = NewEmbeddings()
    assert registry.retry_reindex("northwind")
    registry.forget("northwind")
    moved = registry.for_tenant("northwind")
    assert moved.rag.store.store.collection_name == "reindex_new"
    assert len(moved.rag.store.stored_chunks()) == 3
    assert registry.for_tenant("contoso").rag.store.store.collection_name == "reindex_new"  # its own job ran too
