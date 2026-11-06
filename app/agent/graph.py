"""The LangGraph workflow: understand the message, route it, then answer or act.

    understand -> greet | refuse | needs_identity
    understand -> retrieve -> (weak evidence? rewrite -> retrieve, once) -> generate | abstain
    understand -> select_tool -> clarify | execute_tool
                  select_tool -> prepare_approval -> wait_for_approval -> execute_tool
"""
import logging
from datetime import date
from typing import TypedDict

from langchain_core.documents import Document
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from app import config
from app.agent.hrms import HRMSRejected
from app.agent.prompts import (
    CATEGORIES,
    CLARIFY_ANSWER,
    NEEDS_IDENTITY_ANSWER,
    OUT_OF_SCOPE_ANSWER,
    Understanding,
    build_retry_messages,
    build_tool_messages,
    build_understanding_messages,
)
from app.agent.tools import TOOLS, ToolContext, ToolRunner, langchain_tools
from app.rag.prompt import FALLBACK_ANSWER, GREETING_ANSWER
from app.rag.retrieval import SearchFilters
from app.rag.service import GREETING, RagAnswer, RagService

logger = logging.getLogger("hr_helpdesk")


class WorkflowState(TypedDict, total=False):
    question: str
    summary: str
    history: list[dict]
    user_id: str | None
    conversation_id: str | None
    run_id: str
    today: str
    filters: dict
    stream_tokens: bool
    intent: str
    category: str
    standalone: str
    chunks: list[dict]
    confidence: float
    retried: bool
    answer: str
    answered: bool
    abstain_reason: str
    sources: list[dict]
    tool: str
    tool_args: dict
    tool_status: str
    pending_action: dict | None


def _to_documents(chunks: list[dict]) -> list[Document]:
    return [Document(id=chunk["id"], page_content=chunk["page_content"], metadata=chunk["metadata"])
            for chunk in chunks]


def _say(state: WorkflowState, text: str, **fields) -> dict:
    """Finish a step with a fixed reply, sent to anyone streaming."""
    get_stream_writer()({"token": text})
    return {"answer": text, **fields}


def build_workflow(rag: RagService, llm, runner: ToolRunner, tenant_id: str, checkpointer):
    known_tools = langchain_tools()

    def understand(state: WorkflowState) -> dict:
        question = state["question"]
        if GREETING.fullmatch(question.strip()):  # needs no model call
            return {"intent": "greeting", "standalone": question, "category": ""}
        try:
            result = llm.with_structured_output(Understanding).invoke(
                build_understanding_messages(question, state.get("summary", ""), state.get("history", [])))
        except Exception:
            # The policy path is the safest default: it can still abstain
            logger.exception("Understanding a message failed, so it was treated as a policy question")
            return {"intent": "policy_question", "standalone": question, "category": ""}
        standalone = result.standalone_question.strip()
        if not standalone or len(standalone) > config.MAX_QUESTION_CHARS:
            standalone = question
        return {"intent": result.intent, "standalone": standalone,
                "category": result.category if result.category in CATEGORIES else ""}

    def route_intent(state: WorkflowState) -> str:
        intent = state["intent"]
        if intent == "greeting":
            return "greet"
        if intent == "out_of_scope":
            return "refuse"
        if intent in ("personal_data_request", "action_request"):
            return "select_tool" if state.get("user_id") and config.SELF_SERVICE_ENABLED else "needs_identity"
        return "retrieve"

    def greet(state: WorkflowState) -> dict:
        return _say(state, GREETING_ANSWER, answered=True)

    def refuse(state: WorkflowState) -> dict:
        return _say(state, OUT_OF_SCOPE_ANSWER, answered=False, abstain_reason="out_of_scope")

    def needs_identity(state: WorkflowState) -> dict:
        return _say(state, NEEDS_IDENTITY_ANSWER, answered=False, abstain_reason="needs_identity")

    def retrieve(state: WorkflowState) -> dict:
        given = state.get("filters") or {}
        filters = SearchFilters(tenant_id=tenant_id, policy=given.get("policy"), category=given.get("category"),
                                effective_on=given.get("effective_on"))
        # Narrow by topic the first time only. FAQ and glossary chunks are "general", so they stay in.
        if config.USE_CATEGORY_ROUTING and state.get("category") and not filters.category and not state.get("retried"):
            filters.categories = [state["category"], config.tenant.default_category]
        chunks, confidence = rag.search(state["standalone"], filters)
        return {"chunks": [{"id": c.id, "page_content": c.page_content, "metadata": c.metadata} for c in chunks],
                "confidence": confidence}

    def grade_evidence(state: WorkflowState) -> str:
        weak = not state["chunks"] or state["confidence"] < config.GRADE_MIN_CONFIDENCE
        if weak and not state.get("retried"):
            return "rewrite_query"
        return "generate" if state["chunks"] else "abstain"

    def rewrite_query(state: WorkflowState) -> dict:
        try:
            rewritten = llm.invoke(build_retry_messages(state["standalone"])).text.strip()
        except Exception:
            logger.exception("Rewriting a weak search failed, so the search was repeated without the topic filter")
            rewritten = ""
        usable = rewritten and len(rewritten) <= config.MAX_QUESTION_CHARS
        return {"retried": True, "standalone": rewritten if usable else state["standalone"]}

    def abstain(state: WorkflowState) -> dict:
        return _say(state, FALLBACK_ANSWER, answered=False, abstain_reason="no_evidence")

    def generate(state: WorkflowState) -> dict:
        writer = get_stream_writer()
        final: RagAnswer | None = None
        for kind, value in rag.answer_from_chunks(state["standalone"], _to_documents(state["chunks"]),
                                                  state.get("stream_tokens", True)):
            if kind == "token":
                writer({"token": value})
            else:
                assert isinstance(value, RagAnswer)
                final = value
        assert final is not None
        return {"answer": final.answer, "answered": final.answered, "abstain_reason": final.abstain_reason,
                "sources": final.sources}

    def select_tool(state: WorkflowState) -> dict:
        today = date.fromisoformat(state["today"])
        reply = llm.bind_tools(known_tools).invoke(
            build_tool_messages(state["question"], state.get("summary", ""), state.get("history", []), today))
        calls = getattr(reply, "tool_calls", None) or []
        if not calls:
            # Only a question is passed on, so the model can't state a figure of its own
            text = reply.text.strip()
            return {"tool": "", "answer": text if text.endswith("?") else CLARIFY_ANSWER}
        return {"tool": calls[0]["name"], "tool_args": calls[0]["args"] or {}}

    def route_tool(state: WorkflowState) -> str:
        tool = state.get("tool")
        if not tool:
            return "clarify"
        spec = TOOLS.get(tool)
        return "prepare_approval" if spec and spec.requires_approval else "execute_tool"

    def clarify(state: WorkflowState) -> dict:
        return _say(state, state["answer"], answered=True, abstain_reason="")

    def _context(state: WorkflowState) -> ToolContext:
        return ToolContext(tenant_id, state["user_id"] or "", state.get("conversation_id"), state["run_id"],
                           date.fromisoformat(state["today"]))

    def prepare_approval(state: WorkflowState) -> dict:
        try:
            action_id, summary = runner.prepare_approval(_context(state), state["tool"], state["tool_args"])
        except HRMSRejected as rejection:
            return _say(state, str(rejection), answered=True, tool_status="rejected", pending_action=None)
        except ValidationError:
            return _say(state, "I need a few more details to do that. Which dates and type of leave?",
                        answered=True, tool_status="invalid_arguments", pending_action=None)
        return _say(state, f"I can {summary}. Please approve or decline this request.", answered=True,
                    tool_status="waiting_for_approval", pending_action={"id": action_id, "summary": summary})

    def route_after_prepare(state: WorkflowState) -> str:
        return "wait_for_approval" if state.get("pending_action") else END

    def wait_for_approval(state: WorkflowState) -> dict:
        # The run pauses here, and is saved, until someone decides. It can resume after a restart.
        decision = interrupt({"action": state["pending_action"]})
        return {"tool_status": "approved" if decision == "approve" else "declined"}

    def route_after_approval(state: WorkflowState) -> str:
        return "execute_tool" if state["tool_status"] == "approved" else "declined"

    def declined(state: WorkflowState) -> dict:
        return _say(state, "Okay, I've cancelled that request. Nothing was changed.", pending_action=None)

    def execute_tool(state: WorkflowState) -> dict:
        action = state.get("pending_action") or {}
        outcome = runner.execute(_context(state), state["tool"], state.get("tool_args", {}), action.get("id"))
        return _say(state, outcome.message, answered=True, tool_status=outcome.status, pending_action=None)

    graph = StateGraph(WorkflowState)
    for step in (understand, greet, refuse, needs_identity, retrieve, rewrite_query, abstain, generate, select_tool,
                 clarify, prepare_approval, wait_for_approval, declined, execute_tool):
        graph.add_node(step.__name__, step)
    graph.add_edge(START, "understand")
    graph.add_conditional_edges("understand", route_intent,
                                ["greet", "refuse", "needs_identity", "retrieve", "select_tool"])
    graph.add_conditional_edges("retrieve", grade_evidence, ["rewrite_query", "generate", "abstain"])
    graph.add_edge("rewrite_query", "retrieve")
    graph.add_conditional_edges("select_tool", route_tool, ["clarify", "prepare_approval", "execute_tool"])
    graph.add_conditional_edges("prepare_approval", route_after_prepare, ["wait_for_approval", END])
    graph.add_conditional_edges("wait_for_approval", route_after_approval, ["execute_tool", "declined"])
    for last in ("greet", "refuse", "needs_identity", "abstain", "generate", "clarify", "declined", "execute_tool"):
        graph.add_edge(last, END)
    return graph.compile(checkpointer=checkpointer)
