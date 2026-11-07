"""Per-answer metrics, and a real backup and restore, on a real PostgreSQL. No model API is called."""
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from sqlalchemy import text

from app import config, security
from app.db import get_engine
from app.main import app
from app.metrics import AnswerMetrics, MetricsStore
from app.rag.prompt import StructuredAnswer
from app.rag.service import RagService
from app.rag.store import PolicyIndex, get_store, run_migrations
from app.tenancy import TenantEngine
from app.workflow import ChatWorkflow, create_checkpointer
from ops.backup import BackupError, create_backup, find_tool, restore_backup, table_counts
from pipeline.chunk_documents import chunk_text
from pipeline.embed_and_store import DocumentChange, apply_documents, plan_documents
from tests.integration.conftest import WordEmbeddings
from tests.integration.test_workflow import LEAVE, ScriptedLLM

pytestmark = pytest.mark.integration


class CountedModel(BaseChatModel):
    """A real LangChain model class, so its calls reach the callbacks, as a provider's would."""

    @property
    def _llm_type(self) -> str:
        return "counted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        message = AIMessage(content="Reworded question", usage_metadata={"input_tokens": 100, "output_tokens": 20,
                                                                         "total_tokens": 120})
        return ChatResult(generations=[ChatGeneration(message=message)])


class CountedLLM(ScriptedLLM):
    """Scripted answers, with one real model call per step for the callbacks to see."""

    def __init__(self, **options):
        super().__init__(**options)
        self.model = CountedModel()

    def with_structured_output(self, schema):
        scripted = super().with_structured_output(schema)

        def invoke(messages):
            self.model.invoke("A prompt")
            return scripted.invoke(messages)

        def stream(messages):
            self.model.invoke("A prompt")
            yield from scripted.stream(messages)

        return SimpleNamespace(invoke=invoke, stream=stream)


@pytest.fixture(scope="module")
def shared(database_uri):
    run_migrations(database_uri)
    store = get_store("metrics_test", database_uri, WordEmbeddings())
    index = PolicyIndex(store)
    plan, changed = plan_documents(index, [DocumentChange("Leave_Policy.md", LEAVE, chunk_text(LEAVE, "Leave_Policy.md"))])
    apply_documents(index, plan, changed, wait_seconds=0)
    saver = create_checkpointer(database_uri)
    yield SimpleNamespace(index=index, engine=get_engine(database_uri), saver=saver, uri=database_uri)
    saver.conn.close()
    store.delete_collection()


@pytest.fixture(autouse=True)
def clean(shared, monkeypatch):
    with shared.engine.begin() as connection:
        connection.execute(text("DELETE FROM answer_metrics"))
    monkeypatch.setattr(config, "ABSTENTION_MODE", "distance")
    monkeypatch.setattr(config, "MAX_DISTANCE", 0.9)
    monkeypatch.setattr(config, "MIN_BODY_CHARS", 10)
    monkeypatch.setattr(config, "GRADE_MIN_CONFIDENCE", -100.0)


def make(shared, llm=None, tenant_id=config.TENANT_ID) -> ChatWorkflow:
    llm = llm or CountedLLM()
    index = shared.index if tenant_id == config.TENANT_ID else PolicyIndex(shared.index.store, tenant_id)
    return ChatWorkflow(RagService(index, llm), llm, shared.engine, shared.saver, tenant_id=tenant_id)


def rows(shared):
    with shared.engine.connect() as connection:
        return connection.execute(text("SELECT * FROM answer_metrics ORDER BY id")).all()


def test_an_answer_records_its_times_and_tokens(shared):
    make(shared).ask("How many earned leave days?", user_id="E1001")
    row = rows(shared)[0]
    assert (row.kind, row.intent, row.answered, row.failed) == ("question", "policy_question", True, False)
    assert row.llm_calls == 2  # understanding the question, then writing the answer
    assert (row.input_tokens, row.output_tokens) == (200, 40)
    assert row.retrieval_ms > 0 and row.embedding_ms >= 0 and row.llm_ms > 0
    assert row.total_ms >= row.retrieval_ms + row.llm_ms


def test_a_streamed_answer_is_recorded_before_done(shared):
    for kind, _ in make(shared).stream("How many earned leave days?", user_id="E1001"):
        if kind == "done":
            break  # a caller stops reading here
    assert len(rows(shared)) == 1


def test_metrics_hold_no_question_or_answer_text(shared):
    make(shared).ask("How many zebrafinch earned leave days?", user_id="E1001")
    assert "zebrafinch" not in str(tuple(rows(shared)[0]))
    assert "Eighteen" not in str(tuple(rows(shared)[0]))


def test_a_refusal_counts_as_a_refusal(shared):
    answer = StructuredAnswer(answered=False)
    make(shared, CountedLLM(answer=answer)).ask("How many earned leave days?")
    summary = make(shared).metrics.summary()
    assert (summary["requests"], summary["refusals"], summary["refusal_rate"]) == (1, 1, 1.0)
    assert rows(shared)[0].abstain_reason == "model_abstained"


def test_a_failed_answer_counts_as_a_failure(shared):
    class Failing(CountedLLM):
        def with_structured_output(self, schema):
            if schema is StructuredAnswer:
                return SimpleNamespace(invoke=lambda messages: 1 / 0, stream=lambda messages: 1 / 0)
            return super().with_structured_output(schema)

    with pytest.raises(ZeroDivisionError):
        make(shared, Failing()).ask("How many earned leave days?")
    assert rows(shared)[0].failed is True
    summary = make(shared).metrics.summary()
    assert (summary["requests"], summary["failures"], summary["refusals"]) == (1, 1, 0)


def test_a_tool_is_recorded_with_how_it_went(shared):
    llm = CountedLLM(intent="personal_data_request", tool="get_leave_balance")
    make(shared, llm).ask("What's my leave balance?", user_id="E9999")  # no such employee, so the tool rejects it
    row = rows(shared)[0]
    assert (row.tool, row.tool_status) == ("get_leave_balance", "rejected")
    assert make(shared).metrics.summary()["tool_calls"] == 1


def test_a_failure_to_save_metrics_never_stops_an_answer(shared, monkeypatch):
    workflow = make(shared)
    monkeypatch.setattr(workflow.metrics, "engine", TenantEngine(get_engine("postgresql://nobody@127.0.0.1:9/none"), "x"))
    assert workflow.ask("How many earned leave days?").answered


def test_the_summary_adds_up(shared):
    store = make(shared).metrics
    for total in (1000, 2000, 3000, 4000, 5000):
        store.record(AnswerMetrics(kind="question", total_ms=total, answered=True, retrieval_ms=100, llm_ms=800,
                                   llm_calls=2, input_tokens=10, output_tokens=5, estimated_cost_usd=0.5))
    summary = store.summary()
    assert summary["requests"] == 5
    assert summary["latency_ms"]["total_p50"] == 3000
    assert summary["latency_ms"]["total_p95"] == 4800
    assert (summary["input_tokens"], summary["output_tokens"], summary["estimated_cost_usd"]) == (50, 25, 2.5)


def test_an_empty_summary_is_fine(shared):
    summary = make(shared).metrics.summary()
    assert summary["requests"] == 0
    assert summary["refusal_rate"] is None
    assert summary["latency_ms"]["total_p95"] is None


def test_each_organisation_only_sees_its_own_metrics(shared):
    make(shared).ask("How many earned leave days?")
    assert make(shared, tenant_id="other-tenant").metrics.summary()["requests"] == 0
    assert make(shared).metrics.summary()["requests"] == 1


def test_the_app_cannot_change_recorded_metrics(shared):
    store = MetricsStore(shared.engine, "other-tenant")
    store.record(AnswerMetrics(kind="question", total_ms=1))
    with pytest.raises(Exception, match="permission denied"), TenantEngine(shared.engine, "other-tenant").begin() as connection:
        connection.execute(text("DELETE FROM answer_metrics"))


def test_old_metrics_are_removed(shared):
    from app.identity import AuditLog

    with shared.engine.begin() as connection:
        connection.execute(text("INSERT INTO answer_metrics (tenant_id, kind, total_ms, created_at) "
                                "VALUES ('x', 'question', 1, now() - interval '100 days')"))
    assert AuditLog(shared.engine).purge_metrics_older_than(90) == 1


def test_admins_can_read_the_report_over_the_api(shared, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_KEY", "admin-key")
    workflow = make(shared)
    workflow.ask("How many earned leave days?")
    security.rate_limit.reset()
    app.state.service = SimpleNamespace(metrics=workflow.metrics, close=lambda: None)
    app.state.count_chunks = lambda: 1
    with TestClient(app) as client:
        assert client.get("/admin/metrics", headers={"X-Admin-Key": "wrong"}).status_code == 401
        report = client.get("/admin/metrics?days=30", headers={"X-Admin-Key": "admin-key"}).json()
    assert (report["days"], report["requests"]) == (30, 1)


# Backup and restore

@pytest.fixture
def empty_database(shared):
    """A separate empty database on the same server, with the same extension available."""
    admin = psycopg.connect(shared.uri, autocommit=True)
    admin.execute("DROP DATABASE IF EXISTS restore_check")
    admin.execute("CREATE DATABASE restore_check")
    parts = psycopg.conninfo.conninfo_to_dict(shared.uri)
    parts["dbname"] = "restore_check"
    yield psycopg.conninfo.make_conninfo(**parts)
    admin.execute("DROP DATABASE IF EXISTS restore_check WITH (FORCE)")
    admin.close()


def requires_tools(uri):
    try:
        find_tool("pg_dump", uri)
        find_tool("pg_restore", uri)
    except BackupError:
        pytest.skip("No PostgreSQL client tools of a suitable version here")


def as_url(conninfo: str) -> str:
    parts = psycopg.conninfo.conninfo_to_dict(conninfo)
    if conninfo.startswith("postgres"):
        return conninfo
    host = parts.get("host", "localhost")
    login = parts.get("user", "postgres") + (f":{parts['password']}" if parts.get("password") else "")
    return f"postgresql://{login}@{host}:{parts.get('port', 5432)}/{parts['dbname']}"


@pytest.fixture
def restored(shared, empty_database, tmp_path):
    requires_tools(shared.uri)
    make(shared).ask("How many earned leave days?", user_id="E1001")
    backup = tmp_path / "helpdesk.dump"
    counts = create_backup(backup, shared.uri)
    target = as_url(empty_database) if not shared.uri.startswith("postgres") else _swap_database(shared.uri)
    restore_backup(backup, target)
    return SimpleNamespace(backup=backup, counts=counts, uri=target)


def _swap_database(uri: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(uri)
    return urlunsplit((parts.scheme, parts.netloc, "/restore_check", parts.query, ""))


def test_a_restore_brings_back_every_row(shared, restored):
    assert table_counts(restored.uri) == restored.counts
    assert restored.counts["langchain_pg_embedding"] > 0


def test_a_restored_database_keeps_tenants_apart(shared, restored):
    engine = get_engine(restored.uri)
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL ROLE helpdesk_app"))
        assert connection.execute(text("SELECT count(*) FROM conversations")).scalar() == 0  # no tenant set
    with TenantEngine(engine, config.TENANT_ID).begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM conversations")).scalar() >= 1
        assert connection.execute(text("SELECT count(*) FROM answer_metrics")).scalar() >= 1
    with TenantEngine(engine, "other-tenant").begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM conversations")).scalar() == 0
        assert connection.execute(text("SELECT count(*) FROM langchain_pg_embedding")).scalar() == 0
    engine.dispose()


def test_the_app_works_on_a_restored_database(shared, restored):
    store = get_store("metrics_test", restored.uri, WordEmbeddings())
    llm = CountedLLM()
    workflow = ChatWorkflow(RagService(PolicyIndex(store), llm), llm, get_engine(restored.uri), shared.saver)
    found = workflow.rag.retrieve("earned leave")
    assert found
    assert workflow.conversations.conversations("E1001")


def test_a_restore_refuses_a_database_with_tables(shared, tmp_path):
    requires_tools(shared.uri)
    backup = tmp_path / "helpdesk.dump"
    create_backup(backup, shared.uri)
    before = table_counts(shared.uri)
    with pytest.raises(BackupError, match="already has tables"):
        restore_backup(backup, shared.uri)
    assert table_counts(shared.uri) == before


def test_a_restore_notices_missing_rows(shared, empty_database, tmp_path):
    requires_tools(shared.uri)
    backup = tmp_path / "helpdesk.dump"
    create_backup(backup, shared.uri)
    manifest = tmp_path / "helpdesk.dump.manifest.json"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace('"users": 0', '"users": 5'), encoding="utf-8")
    target = as_url(empty_database) if not shared.uri.startswith("postgres") else _swap_database(shared.uri)
    with pytest.raises(BackupError, match="don't match"):
        restore_backup(backup, target)


def test_a_missing_backup_is_explained(shared, tmp_path):
    with pytest.raises(BackupError, match="doesn't exist"):
        restore_backup(tmp_path / "none.dump", shared.uri)


def test_a_backup_file_holds_no_password(shared, tmp_path):
    requires_tools(shared.uri)
    backup = tmp_path / "helpdesk.dump"
    create_backup(backup, shared.uri)
    password = psycopg.conninfo.conninfo_to_dict(shared.uri).get("password")
    if password:
        assert password.encode() not in backup.read_bytes()
