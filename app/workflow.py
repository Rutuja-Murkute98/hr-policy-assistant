"""Run the workflow for a request: load the chat, run the graph, save the result.

Workflow checkpoints only exist while a run is paused for approval. A finished run's checkpoint
is deleted straight away, so stored chats stay in the conversation tables, under their retention rules.
"""
import logging
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date

from langchain_core.documents import Document
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app import config
from app.agent.graph import build_workflow
from app.agent.hrms import MockHRMS
from app.agent.tools import PendingActions, ToolRunner
from app.db import get_engine
from app.identity import Organisations, OrganisationUnavailable
from app.knowledge import KnowledgeBase
from app.memory import ConversationStore, summarise_if_long
from app.metrics import AnswerMetrics, MetricsStore
from app.observability import Tracing, UsageCallback, estimated_cost
from app.org_settings import Profile, SettingsStore, setting
from app.pii import mask
from app.rag.answer_cache import AnswerCache, answer_key
from app.rag.embedding_cache import QuestionEmbeddingCache
from app.rag.prompt import build_messages
from app.rag.providers import get_chat_model, get_router_model
from app.rag.rerank import get_reranker
from app.rag.retrieval import SearchFilters
from app.rag.service import RagAnswer, RagService
from app.rag.store import PolicyIndex, get_database_uri, get_store
from app.tenancy import ENTER_TENANT, check_tenant_id, secure_library_tables, tenant_scoped

logger = logging.getLogger("hr_helpdesk")
PURGE_INTERVAL_SECONDS = 24 * 60 * 60


@dataclass
class WorkflowAnswer(RagAnswer):
    intent: str = ""
    conversation_id: str | None = None
    pending_action: dict | None = None
    tool: str = ""
    tool_status: str = ""
    message_id: int | None = None  # the stored answer, which feedback refers to
    step_ms: dict = field(default_factory=dict)  # milliseconds per workflow step
    degraded: list = field(default_factory=list)  # steps whose model call failed, so a fallback was used


class TenantSaver(PostgresSaver):
    """Paused runs for one tenant. Row-Level Security applies, as it does to the app's own tables.

    The role and tenant are set per transaction, so this overrides the library's cursor method, and
    an integration test proves it still works. Any change to that method would fail that test.
    """

    def __init__(self, pool: ConnectionPool, tenant_id: str):
        super().__init__(pool)  # type: ignore[arg-type]
        self.pool = pool
        self.tenant_id = check_tenant_id(tenant_id)

    @contextmanager
    def _cursor(self, *, pipeline: bool = False):
        with self.lock, self.pool.connection() as connection, connection.transaction(), connection.cursor(
                binary=True, row_factory=dict_row) as cursor:
            cursor.execute(ENTER_TENANT, (self.tenant_id,))
            yield cursor


class ChatWorkflow:
    def __init__(self, rag: RagService, llm, engine, checkpointer, hrms=None, tenant_id: str = config.TENANT_ID,
                 today=date.today, tracing: Tracing | None = None, router=None):
        if rag.tenant_id != tenant_id:
            raise ValueError("This search service belongs to another tenant")
        scoped = tenant_scoped(engine, tenant_id)
        self.rag = rag
        self.llm = llm
        self.tenant_id = tenant_id
        self.today = today
        self.public_emails = (rag.profile.hr_email,)
        self.conversations = ConversationStore(scoped, tenant_id, self.public_emails)
        self.actions = PendingActions(scoped, tenant_id)
        self.runner = ToolRunner(scoped, hrms or MockHRMS(scoped, tenant_id))
        self.knowledge = KnowledgeBase(scoped, rag.store, tenant_id, profile=rag.profile)
        self.metrics = MetricsStore(scoped, tenant_id)
        if config.CACHE_QUESTION_EMBEDDINGS and rag.query_cache is None:
            rag.query_cache = QuestionEmbeddingCache(scoped, tenant_id)
        self.answers: AnswerCache | None = None
        if setting(rag.profile, "cache_answers"):
            collection = getattr(getattr(rag.store, "store", None), "collection_name", config.COLLECTION_NAME)
            # A change to any of the organisation's settings, such as a threshold, starts a fresh cache
            version = getattr(rag.profile, "version", "")
            model = answer_key(f"{config.LLM_PROVIDER}:{setting(rag.profile, 'chat_model')}"
                               + (f":{version}" if version else ""), build_messages("", [], rag.profile))
            self.answers = AnswerCache(scoped, tenant_id, collection, model)
        self.tracing_callbacks = tracing.callbacks if tracing else []
        self.checkpointer = checkpointer
        if isinstance(checkpointer, PostgresSaver):
            self.checkpointer = TenantSaver(checkpointer.conn, tenant_id)  # type: ignore[arg-type]
        self.graph = build_workflow(rag, llm, self.runner, tenant_id, self.checkpointer,
                                    router=router if router is not None else get_router_model(),
                                    answers=self.answers)
        self._last_purge = 0.0

    @property
    def store(self):
        return self.rag.store

    def _config(self, run_id: str) -> dict:
        return {"configurable": {"thread_id": run_id}}

    def _record(self, kind: str, started: float, usage: UsageCallback, values: dict | None = None,
                failed: bool = False) -> None:
        values = values or {}
        self.metrics.record(AnswerMetrics(
            kind=kind, total_ms=(time.perf_counter() - started) * 1000, failed=failed,
            intent=values.get("intent", ""), answered=bool(values.get("answered", False)),
            abstain_reason=values.get("abstain_reason", ""), tool=values.get("tool") or "",
            tool_status=values.get("tool_status") or "", embedding_ms=values.get("embedding_ms", 0.0),
            retrieval_ms=values.get("retrieval_ms", 0.0), llm_ms=usage.llm_ms, llm_calls=usage.llm_calls,
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            estimated_cost_usd=estimated_cost(usage.input_tokens, usage.output_tokens),
            step_ms=values.get("step_ms") or {}))

    def _forget_run(self, run_id: str) -> None:
        self.checkpointer.delete_thread(run_id)

    def _answer(self, state: dict, conversation_id: str | None) -> WorkflowAnswer:
        sources = state.get("sources") or []
        chunks = [Document(id=c["id"], page_content=c["page_content"], metadata=c["metadata"])
                  for c in state.get("chunks") or []]
        return WorkflowAnswer(
            answer=state.get("answer", ""), sources=sources, chunks=chunks,
            question_used=state.get("standalone", state.get("question", "")),
            answered=state.get("answered", True), abstain_reason=state.get("abstain_reason", ""),
            intent=state.get("intent", ""), conversation_id=conversation_id,
            pending_action=state.get("pending_action"), tool=state.get("tool", ""),
            tool_status=state.get("tool_status", ""), step_ms=dict(state.get("step_ms") or {}),
            degraded=list(state.get("degraded") or []),
        )

    def run_maintenance(self, force: bool = False) -> None:
        """Apply the retention period and expire old approvals, at most once a day."""
        if not force and time.monotonic() - self._last_purge < PURGE_INTERVAL_SECONDS and self._last_purge:
            return
        self._last_purge = time.monotonic()
        for run_id in self.actions.expire_old():
            self._forget_run(run_id)
        if force:  # only at startup: anything marked as running then was cut off by the restart
            self.knowledge.fail_interrupted()
        self.knowledge.purge_unanswered_older_than(config.CONVERSATION_RETENTION_DAYS)
        removed = self.conversations.purge_older_than(config.CONVERSATION_RETENTION_DAYS)
        if self.rag.query_cache is not None:
            self.rag.query_cache.purge_older_than(config.CONVERSATION_RETENTION_DAYS)
        if self.answers is not None:
            self.answers.purge_older_than(config.CONVERSATION_RETENTION_DAYS)
        if removed:
            logger.info("Removed %s conversations past the retention period", removed)

    def stream(self, question: str, history: list[dict] | None = None, filters: SearchFilters | None = None,
               stream_tokens: bool = True, user_id: str | None = None, conversation_id: str | None = None,
               save_history: bool = True) -> Iterator[tuple[str, object]]:
        """Yield ("token", text) as the answer is written, then ("done", WorkflowAnswer)."""
        started = time.perf_counter()
        usage = UsageCallback()
        self.run_maintenance()
        # From here on the masked text is all that's used, so a model provider never sees what was masked
        question = mask(question, self.public_emails)
        summary = ""
        if user_id and save_history:
            # A stored chat is the source of history, so a caller can't rewrite what was said
            conversation_id = self.conversations.start(user_id, conversation_id)
            summary, history = self.conversations.context(user_id, conversation_id, config.MAX_HISTORY_MESSAGES)
            self.conversations.add_message(user_id, conversation_id, "user", question)
        else:
            conversation_id = None
            history = [{**turn, "content": mask(turn["content"], self.public_emails)}
                       for turn in (history or [])[-config.MAX_HISTORY_MESSAGES:]]

        run_id = f"{self.tenant_id}:{user_id or 'anonymous'}:{uuid.uuid4().hex}"
        run_config = self._config(run_id)
        given = {"policy": filters.policy, "category": filters.category,
                 "effective_on": filters.effective_on} if filters else {}
        state_in = {"question": question, "summary": summary, "history": history, "user_id": user_id,
                    "conversation_id": conversation_id, "run_id": run_id, "today": self.today().isoformat(),
                    "filters": given, "stream_tokens": stream_tokens}
        try:
            for event in self.graph.stream(state_in, {**run_config, "callbacks": [usage, *self.tracing_callbacks]},
                                           stream_mode="custom", durability="exit"):
                if "status" in event:
                    yield "status", event["status"]
                else:
                    yield "token", event["token"]
            snapshot = self.graph.get_state(run_config)
        except Exception:
            self._forget_run(run_id)
            self._record("question", started, usage, failed=True)
            raise

        answer = self._answer(snapshot.values, conversation_id)
        if not snapshot.next:
            self._forget_run(run_id)
        if user_id and conversation_id:
            answer.message_id = self.conversations.add_message(user_id, conversation_id, "assistant", answer.answer)
            summarise_if_long(self.conversations, self.llm, user_id, conversation_id)
        # The wording of a question is kept here too, so someone who chose not to save their chats is left out
        if save_history and answer.abstain_reason in ("no_evidence", "model_abstained"):
            self.knowledge.record_unanswered(answer.question_used or question, answer.abstain_reason)
        self._record("question", started, usage, snapshot.values)  # before "done", as a caller may stop reading there
        yield "done", answer

    def ask(self, question: str, history: list[dict] | None = None, filters: SearchFilters | None = None,
            user_id: str | None = None, conversation_id: str | None = None,
            save_history: bool = True) -> WorkflowAnswer:
        for kind, value in self.stream(question, history, filters, stream_tokens=False, user_id=user_id,
                                       conversation_id=conversation_id, save_history=save_history):
            if kind == "done":
                assert isinstance(value, WorkflowAnswer)
                return value
        raise RuntimeError("The workflow ended without a final answer")

    def decide(self, user_id: str, action_id: str, approve: bool) -> WorkflowAnswer:
        """Record an approval or refusal, then resume the paused run. It works after a restart."""
        action = self.actions.decide(user_id, action_id, approve)
        run_config = self._config(action["run_id"])
        started = time.perf_counter()
        usage = UsageCallback()
        try:
            self.graph.invoke(Command(resume="approve" if approve else "decline"),
                              {**run_config, "callbacks": [usage, *self.tracing_callbacks]}, durability="exit")
            values = self.graph.get_state(run_config).values
            answer = self._answer(values, action["conversation_id"])
            self._record("decision", started, usage, {**values, "embedding_ms": 0.0, "retrieval_ms": 0.0})
        except Exception:
            self._record("decision", started, usage, failed=True)
            raise
        finally:
            self._forget_run(action["run_id"])
        if action["conversation_id"]:
            self.conversations.add_message(user_id, action["conversation_id"], "assistant", answer.answer)
        return answer

    def close(self) -> None:
        """Close the database connections held for paused runs."""
        pool = self.checkpointer.conn
        if hasattr(pool, "close"):
            pool.close()

    def delete_conversation(self, user_id: str, conversation_id: str) -> None:
        self.conversations.delete(user_id, conversation_id)
        for run_id in self.actions.cancel_for_conversations(user_id, conversation_id):
            self._forget_run(run_id)

    def delete_user_history(self, user_id: str) -> int:
        removed = self.conversations.delete_all(user_id)
        for run_id in self.actions.cancel_for_conversations(user_id):
            self._forget_run(run_id)
        return removed


def create_checkpointer(database_uri: str) -> PostgresSaver:
    """Create the store for paused runs. It lets connections close when idle, so a hosted database can sleep."""
    pool = ConnectionPool(database_uri, min_size=0, max_size=4, max_idle=60, open=True,
                          check=ConnectionPool.check_connection,
                          kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
    saver = PostgresSaver(pool)  # type: ignore[arg-type]
    saver.setup()
    engine = get_engine(database_uri)
    with engine.begin() as connection:
        secure_library_tables(connection, config.TENANT_ID)
    engine.dispose()
    return saver


def create_rag_service(llm=None) -> RagService:
    return RagService(PolicyIndex(get_store()), llm or get_chat_model(), get_reranker())


def create_workflow() -> ChatWorkflow:
    """One workflow for the default organisation, as the evaluations use."""
    uri = get_database_uri()
    llm = get_chat_model()
    workflow = ChatWorkflow(create_rag_service(llm), llm, get_engine(uri), create_checkpointer(uri))
    workflow.run_maintenance(force=True)
    return workflow


class WorkflowRegistry:
    """One workflow per organisation, sharing the database pools and model, as memory is tight.

    An organisation's workflow is rebuilt when its settings change, which every process notices within
    SETTINGS_CACHE_SECONDS, so a change needs no restart.
    """

    def __init__(self, store, llm, engine, checkpointer, reranker=None, tracing: Tracing | None = None,
                 model_factory=get_chat_model):
        self.store = store
        self.llm = llm
        self.engine = engine
        self.checkpointer = checkpointer
        self.reranker = reranker
        self.tracing = tracing or Tracing()
        self.organisations = Organisations(engine)
        self.settings = SettingsStore(engine)
        self.model_factory = model_factory
        self._workflows: dict[str, ChatWorkflow] = {}
        self._models: dict[tuple, object] = {}
        self._lock = threading.Lock()

    def for_tenant(self, tenant_id: str) -> ChatWorkflow:
        _, version = self.settings.read(tenant_id)
        with self._lock:
            workflow = self._workflows.get(tenant_id)
            if workflow is None or getattr(workflow.rag.profile, "version", "") != version:
                workflow = self._workflows[tenant_id] = self._build(tenant_id)
        return workflow

    def profile_for(self, tenant_id: str) -> Profile | None:
        """The organisation with its own settings applied, or None if it isn't available."""
        base = config.tenant if tenant_id == config.TENANT_ID else self.organisations.get(tenant_id)
        return None if base is None else self.settings.profile(tenant_id, base)

    def model_for(self, profile: Profile):
        """The organisation's answer model. Organisations that chose the same model share it."""
        model, temperature = setting(profile, "chat_model"), profile.overrides.temperature
        if model == config.CHAT_MODEL and temperature is None:
            return self.llm
        key = (model, temperature)
        if key not in self._models:
            self._models[key] = self.model_factory(model=model, temperature=temperature)
        return self._models[key]

    def _build(self, tenant_id: str) -> ChatWorkflow:
        profile = self.profile_for(tenant_id)
        if profile is None:
            raise OrganisationUnavailable(tenant_id)
        llm = self.model_for(profile)
        rag = RagService(PolicyIndex(self.store, tenant_id), llm, self.reranker, profile=profile)
        workflow = ChatWorkflow(rag, llm, self.engine, self.checkpointer, tenant_id=tenant_id,
                                tracing=self.tracing)
        workflow.run_maintenance(force=True)
        return workflow

    def close(self) -> None:
        self.tracing.close()
        self.checkpointer.conn.close()


def create_registry() -> WorkflowRegistry:
    uri = get_database_uri()
    registry = WorkflowRegistry(get_store(), get_chat_model(), get_engine(uri), create_checkpointer(uri),
                                get_reranker())
    registry.for_tenant(config.TENANT_ID)  # checks the index and settings at startup, not on the first question
    return registry
