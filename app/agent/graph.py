"""The LangGraph workflow: understand the message, route it, then answer or act.

    understand -> greet | needs_identity
    understand -> confirm_out_of_scope -> refuse | retrieve
    understand -> retrieve -> (weak evidence? rewrite -> retrieve, once) -> generate | abstain
    understand -> select_tool -> clarify | execute_tool | retrieve (no tool fits a question)
                  select_tool -> prepare_approval -> wait_for_approval -> execute_tool
"""
import functools
import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date
from typing import Annotated, TypedDict

from langchain_core.documents import Document
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from app import config
from app.agent.hrms import HRMSRejected
from app.agent.prompts import (
    CLARIFY_ANSWER,
    Understanding,
    build_retry_messages,
    build_tool_messages,
    build_understanding_messages,
    categories_of,
    needs_identity_answer,
    out_of_scope_answer,
)
from app.agent.tools import TOOLS, ToolContext, ToolRunner, langchain_tools
from app.rag.prompt import fallback_answer, greeting_answer
from app.rag.retrieval import SearchFilters
from app.rag.service import GREETING, RagAnswer, RagService

logger = logging.getLogger("hr_helpdesk")
# Searches started while a message is being understood. Shared, as each one is short and memory is tight.
PREFETCH = ThreadPoolExecutor(max_workers=4, thread_name_prefix="prefetch")


def add_step_times(old: dict | None, new: dict | None) -> dict:
    """Merge step timings. A step that runs twice, such as a search after a retry, counts both times."""
    merged = dict(old or {})
    for step, milliseconds in (new or {}).items():
        merged[step] = merged.get(step, 0.0) + milliseconds
    return merged


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
    asks_about_past: bool
    standalone: str     # the question the answer must address
    search_query: str   # what to search for, which a retry can reword
    first_search: dict  # the results before a retry, kept in case the retry finds less
    chunks: list[dict]
    confidence: float
    retried: bool
    embedding_ms: float
    retrieval_ms: float
    answer: str
    answered: bool
    abstain_reason: str
    sources: list[dict]
    tool: str
    tool_args: dict
    tool_status: str
    pending_action: dict | None
    prefetched: dict | None  # a search made while the message was being understood
    step_ms: Annotated[dict, add_step_times]  # milliseconds spent in each step


def timed_step(step):
    """Run a workflow step and record how long it took, so the slow step of an answer can be found."""
    @functools.wraps(step)
    def run(state: WorkflowState) -> dict:
        started = time.perf_counter()
        result = step(state) or {}
        return {**result, "step_ms": {step.__name__: round((time.perf_counter() - started) * 1000, 1)}}
    return run


def _to_documents(chunks: list[dict]) -> list[Document]:
    return [Document(id=chunk["id"], page_content=chunk["page_content"], metadata=chunk["metadata"])
            for chunk in chunks]


TOOL_STATUS = {"get_leave_balance": "Checking leave balance", "get_latest_payslip": "Checking your payslip",
               "apply_for_leave": "Submitting your leave request", "raise_hr_ticket": "Raising your HR ticket"}


def _status(text: str) -> None:
    """Say what's happening, in fixed words. Reasoning and prompts are never sent."""
    get_stream_writer()({"status": text})


def _say(state: WorkflowState, text: str, **fields) -> dict:
    """Finish a step with a fixed reply, sent to anyone streaming."""
    get_stream_writer()({"token": text})
    return {"answer": text, **fields}


def build_workflow(rag: RagService, llm, runner: ToolRunner, tenant_id: str, checkpointer, router=None):
    """router reads each message, and can be a smaller, faster model than llm, which writes the answers."""
    profile = rag.profile
    known_tools = langchain_tools()
    router = router or llm

    def search_filters(state: WorkflowState, include_history: bool = False) -> SearchFilters:
        given = state.get("filters") or {}
        return SearchFilters(tenant_id=tenant_id, policy=given.get("policy"), category=given.get("category"),
                             effective_on=given.get("effective_on"), include_history=include_history)

    def can_prefetch(state: WorkflowState) -> bool:
        """Only a first message can be searched as typed: a follow-up needs rewriting first."""
        return (config.SEARCH_WHILE_UNDERSTANDING and not config.USE_CATEGORY_ROUTING
                and not state.get("history") and not state.get("summary"))

    def use_prefetch(future: Future, state: WorkflowState, question: str) -> dict:
        """Wait for the search started earlier, and keep it for the next step. A failed one is just done again."""
        try:
            chunks, confidence, timing = future.result()
        except Exception:
            logger.exception("A search made while understanding failed, so it will be done again")
            return {}
        return {"prefetched": {"query": question, "confidence": confidence,
                               "chunks": [{"id": c.id, "page_content": c.page_content, "metadata": c.metadata}
                                          for c in chunks]}, **timing}

    def timed_search(state: WorkflowState, query: str, filters: SearchFilters) -> tuple[list[Document], float, dict]:
        """Search, and add the time it took to what this answer has used so far."""
        timing: dict = {}
        chunks, confidence = rag.search(query, filters, timing)
        return chunks, confidence, {"embedding_ms": state.get("embedding_ms", 0.0) + timing["embedding_ms"],
                                    "retrieval_ms": state.get("retrieval_ms", 0.0) + timing["retrieval_ms"]}

    def understand(state: WorkflowState) -> dict:
        _status("Understanding your question")
        question = state["question"]
        if GREETING.fullmatch(question.strip()):  # needs no model call
            return {"intent": "greeting", "standalone": question, "category": ""}
        # Most first messages are policy questions, so their search runs while the model reads them
        future = None
        if can_prefetch(state):
            future = PREFETCH.submit(timed_search, state, question, search_filters(state))
        try:
            result = router.with_structured_output(Understanding).invoke(
                build_understanding_messages(question, state.get("summary", ""), state.get("history", []), profile))
        except Exception:
            # The policy path is the safest default: it can still abstain
            logger.exception("Understanding a message failed, so it was treated as a policy question")
            fallback = {"intent": "policy_question", "standalone": question, "category": ""}
            return {**fallback, **(use_prefetch(future, state, question) if future else {})}
        standalone = result.standalone_question.strip()
        if not standalone or len(standalone) > config.MAX_QUESTION_CHARS:
            standalone = question
        understood = {"intent": result.intent, "standalone": standalone, "asks_about_past": result.asks_about_past,
                      "category": result.category if result.category in categories_of(profile) else ""}
        searches_next = result.intent in ("policy_question", "out_of_scope") and not result.asks_about_past
        if future and searches_next:
            return {**understood, **use_prefetch(future, state, question)}
        return understood  # a tool request, or a question about old versions, doesn't use it

    def prefetched(state: WorkflowState) -> tuple[list[Document], float] | None:
        """The search made while understanding, if this is the first search of the answer."""
        found = state.get("prefetched")
        if not found or state.get("retried") or state.get("first_search"):
            return None
        return _to_documents(found["chunks"]), found["confidence"]

    def route_intent(state: WorkflowState) -> str:
        intent = state["intent"]
        if intent == "greeting":
            return "greet"
        if intent == "out_of_scope":
            return "confirm_out_of_scope"
        if intent in ("personal_data_request", "action_request"):
            return "select_tool" if state.get("user_id") and config.SELF_SERVICE_ENABLED else "needs_identity"
        return "retrieve"

    def greet(state: WorkflowState) -> dict:
        return _say(state, greeting_answer(profile), answered=True)

    def confirm_out_of_scope(state: WorkflowState) -> dict:
        """Check the label against the policies, since wrongly refusing a real question is costly."""
        early = prefetched(state)
        if early:
            chunks, confidence = early
            timing: dict = {}
        else:
            chunks, confidence, timing = timed_search(state, state["standalone"], SearchFilters(tenant_id=tenant_id))
        if chunks and confidence >= config.OUT_OF_SCOPE_OVERRIDE_CONFIDENCE:
            return {"intent": "policy_question", "category": "", **timing}
        return timing

    def route_out_of_scope(state: WorkflowState) -> str:
        return "refuse" if state["intent"] == "out_of_scope" else "retrieve"

    def refuse(state: WorkflowState) -> dict:
        return _say(state, out_of_scope_answer(profile), answered=False, abstain_reason="out_of_scope")

    def needs_identity(state: WorkflowState) -> dict:
        return _say(state, needs_identity_answer(profile), answered=False, abstain_reason="needs_identity")

    def retrieve(state: WorkflowState) -> dict:
        given = state.get("filters") or {}
        _status("Searching policies")
        filters = SearchFilters(tenant_id=tenant_id, policy=given.get("policy"), category=given.get("category"),
                                effective_on=given.get("effective_on"),
                                include_history=bool(state.get("asks_about_past")))
        # Narrow by topic the first time only. FAQ and glossary chunks are "general", so they stay in.
        if config.USE_CATEGORY_ROUTING and state.get("category") and not filters.category and not state.get("retried"):
            filters.categories = [state["category"], profile.default_category]
        early = prefetched(state)
        if early and not filters.categories:
            chunks, confidence = early
            timing: dict = {}
        else:
            chunks, confidence, timing = timed_search(state, state.get("search_query") or state["standalone"],
                                                      filters)
        found = {"chunks": [{"id": c.id, "page_content": c.page_content, "metadata": c.metadata} for c in chunks],
                 "confidence": confidence}
        first = state.get("first_search")
        if first and first["chunks"] and (not chunks or first["confidence"] >= confidence):
            return {**first, **timing}  # the retry found nothing better
        return {**found, **timing}

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
        # Only the search is reworded. The model still answers what the employee asked.
        return {"retried": True, "search_query": rewritten if usable else state["standalone"],
                "first_search": {"chunks": state["chunks"], "confidence": state["confidence"]}}

    def abstain(state: WorkflowState) -> dict:
        return _say(state, fallback_answer(profile), answered=False, abstain_reason="no_evidence")

    def generate(state: WorkflowState) -> dict:
        _status("Writing the answer")
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
        _status("Choosing what to do")
        today = date.fromisoformat(state["today"])
        reply = llm.bind_tools(known_tools).invoke(
            build_tool_messages(state["question"], state.get("summary", ""), state.get("history", []), today))
        calls = getattr(reply, "tool_calls", None) or []
        if not calls and state["intent"] != "action_request":
            # No tool fits a question about the employee, so it may really be about the rules
            return {"tool": "", "intent": "policy_question"}
        if not calls:
            # Only a question is passed on, so the model can't state a figure of its own
            text = reply.text.strip()
            return {"tool": "", "answer": text if text.endswith("?") else CLARIFY_ANSWER}
        spec = TOOLS.get(calls[0]["name"])
        if spec and spec.changes_records and state["intent"] != "action_request":
            # Asking for information must never change anything, whatever tool the model picks
            return {"tool": "", "intent": "policy_question"}
        return {"tool": calls[0]["name"], "tool_args": calls[0]["args"] or {}}

    def route_tool(state: WorkflowState) -> str:
        tool = state.get("tool")
        if not tool:
            return "clarify" if state["intent"] == "action_request" else "retrieve"
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
        _status(TOOL_STATUS.get(state["tool"], "Working on it"))
        action = state.get("pending_action") or {}
        outcome = runner.execute(_context(state), state["tool"], state.get("tool_args", {}), action.get("id"))
        return _say(state, outcome.message, answered=True, tool_status=outcome.status, pending_action=None)

    graph = StateGraph(WorkflowState)
    for step in (understand, greet, confirm_out_of_scope, refuse, needs_identity, retrieve, rewrite_query, abstain,
                 generate, select_tool, clarify, prepare_approval, wait_for_approval, declined, execute_tool):
        graph.add_node(step.__name__, timed_step(step))
    graph.add_edge(START, "understand")
    graph.add_conditional_edges("understand", route_intent,
                                ["greet", "confirm_out_of_scope", "needs_identity", "retrieve", "select_tool"])
    graph.add_conditional_edges("confirm_out_of_scope", route_out_of_scope, ["refuse", "retrieve"])
    graph.add_conditional_edges("retrieve", grade_evidence, ["rewrite_query", "generate", "abstain"])
    graph.add_edge("rewrite_query", "retrieve")
    graph.add_conditional_edges("select_tool", route_tool, ["clarify", "retrieve", "prepare_approval", "execute_tool"])
    graph.add_conditional_edges("prepare_approval", route_after_prepare, ["wait_for_approval", END])
    graph.add_conditional_edges("wait_for_approval", route_after_approval, ["execute_tool", "declined"])
    for last in ("greet", "refuse", "needs_identity", "abstain", "generate", "clarify", "declined", "execute_tool"):
        graph.add_edge(last, END)
    return graph.compile(checkpointer=checkpointer)
