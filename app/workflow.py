"""Run the workflow for a request: load the chat, run the graph, save the result.

Workflow checkpoints only exist while a run is paused for approval. A finished run's checkpoint
is deleted straight away, so stored chats stay in the conversation tables, under their retention rules.
"""
import logging
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
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
from app.memory import ConversationStore, summarise_if_long
from app.rag.providers import get_chat_model
from app.rag.rerank import get_reranker
from app.rag.retrieval import SearchFilters
from app.rag.service import RagAnswer, RagService
from app.rag.store import PolicyIndex, get_database_uri, get_store

logger = logging.getLogger("hr_helpdesk")
PURGE_INTERVAL_SECONDS = 24 * 60 * 60


@dataclass
class WorkflowAnswer(RagAnswer):
    intent: str = ""
    conversation_id: str | None = None
    pending_action: dict | None = None
    tool: str = ""
    tool_status: str = ""


class ChatWorkflow:
    def __init__(self, rag: RagService, llm, engine, checkpointer, hrms=None, tenant_id: str = config.TENANT_ID,
                 today=date.today):
        self.rag = rag
        self.llm = llm
        self.tenant_id = tenant_id
        self.today = today
        self.conversations = ConversationStore(engine, tenant_id)
        self.actions = PendingActions(engine, tenant_id)
        self.runner = ToolRunner(engine, hrms or MockHRMS(engine, tenant_id))
        self.checkpointer = checkpointer
        self.graph = build_workflow(rag, llm, self.runner, tenant_id, checkpointer)
        self._last_purge = 0.0

    @property
    def store(self):
        return self.rag.store

    def _config(self, run_id: str) -> dict:
        return {"configurable": {"thread_id": run_id}}

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
            tool_status=state.get("tool_status", ""),
        )

    def run_maintenance(self, force: bool = False) -> None:
        """Apply the retention period and expire old approvals, at most once a day."""
        if not force and time.monotonic() - self._last_purge < PURGE_INTERVAL_SECONDS and self._last_purge:
            return
        self._last_purge = time.monotonic()
        for run_id in self.actions.expire_old():
            self._forget_run(run_id)
        removed = self.conversations.purge_older_than(config.CONVERSATION_RETENTION_DAYS)
        if removed:
            logger.info("Removed %s conversations past the retention period", removed)

    def stream(self, question: str, history: list[dict] | None = None, filters: SearchFilters | None = None,
               stream_tokens: bool = True, user_id: str | None = None,
               conversation_id: str | None = None) -> Iterator[tuple[str, object]]:
        """Yield ("token", text) as the answer is written, then ("done", WorkflowAnswer)."""
        self.run_maintenance()
        summary = ""
        if user_id:
            # A stored chat is the source of history, so a caller can't rewrite what was said
            conversation_id = self.conversations.start(user_id, conversation_id)
            summary, history = self.conversations.context(user_id, conversation_id, config.MAX_HISTORY_MESSAGES)
            self.conversations.add_message(user_id, conversation_id, "user", question)
        else:
            conversation_id = None
            history = (history or [])[-config.MAX_HISTORY_MESSAGES:]

        run_id = f"{self.tenant_id}:{user_id or 'anonymous'}:{uuid.uuid4().hex}"
        run_config = self._config(run_id)
        given = {"policy": filters.policy, "category": filters.category,
                 "effective_on": filters.effective_on} if filters else {}
        state_in = {"question": question, "summary": summary, "history": history, "user_id": user_id,
                    "conversation_id": conversation_id, "run_id": run_id, "today": self.today().isoformat(),
                    "filters": given, "stream_tokens": stream_tokens}
        try:
            for event in self.graph.stream(state_in, run_config, stream_mode="custom", durability="exit"):
                yield "token", event["token"]
            snapshot = self.graph.get_state(run_config)
        except Exception:
            self._forget_run(run_id)
            raise

        answer = self._answer(snapshot.values, conversation_id)
        if not snapshot.next:
            self._forget_run(run_id)
        if user_id and conversation_id:
            self.conversations.add_message(user_id, conversation_id, "assistant", answer.answer)
            summarise_if_long(self.conversations, self.llm, user_id, conversation_id)
        yield "done", answer

    def ask(self, question: str, history: list[dict] | None = None, filters: SearchFilters | None = None,
            user_id: str | None = None, conversation_id: str | None = None) -> WorkflowAnswer:
        for kind, value in self.stream(question, history, filters, stream_tokens=False, user_id=user_id,
                                       conversation_id=conversation_id):
            if kind == "done":
                assert isinstance(value, WorkflowAnswer)
                return value
        raise RuntimeError("The workflow ended without a final answer")

    def decide(self, user_id: str, action_id: str, approve: bool) -> WorkflowAnswer:
        """Record an approval or refusal, then resume the paused run. It works after a restart."""
        action = self.actions.decide(user_id, action_id, approve)
        run_config = self._config(action["run_id"])
        try:
            self.graph.invoke(Command(resume="approve" if approve else "decline"), run_config, durability="exit")
            answer = self._answer(self.graph.get_state(run_config).values, action["conversation_id"])
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
    return saver


def create_rag_service(llm=None) -> RagService:
    return RagService(PolicyIndex(get_store()), llm or get_chat_model(), get_reranker())


def create_workflow() -> ChatWorkflow:
    uri = get_database_uri()
    llm = get_chat_model()
    workflow = ChatWorkflow(create_rag_service(llm), llm, get_engine(uri), create_checkpointer(uri))
    workflow.run_maintenance(force=True)
    return workflow
