"""The whole workflow on a real PostgreSQL, with a scripted LLM so no model API is called."""
from datetime import date, timedelta
from types import SimpleNamespace

import psycopg
import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import text

from app import config
from app.agent.hrms import HRMSRejected, HRMSUnavailable, MockHRMS, working_days
from app.agent.prompts import OUT_OF_SCOPE_ANSWER, Understanding
from app.agent.tools import TOOLS, ActionNotFound, ActionNotPending, ToolContext
from app.db import get_engine
from app.memory import ConversationNotFound
from app.rag.prompt import FALLBACK_ANSWER, GREETING_ANSWER, StructuredAnswer
from app.rag.service import RagService
from app.rag.store import PolicyIndex, get_store, run_migrations
from app.workflow import ChatWorkflow, create_checkpointer
from pipeline.chunk_documents import chunk_text
from pipeline.embed_and_store import DocumentChange, apply_documents, plan_documents
from tests.integration.conftest import WordEmbeddings

pytestmark = pytest.mark.integration

TODAY = date(2026, 11, 2)  # a Monday
NEXT_MONDAY = "2026-11-09"
NEXT_TUESDAY = "2026-11-10"
LEAVE = """# Leave Policy

**Version:** 2.0 | **Effective Date:** 1 April 2026 | **Next Review:** 1 April 2027

## 1. Earned Leave

All confirmed employees are entitled to eighteen days of earned leave per calendar year under this policy.
"""
ASSETS = """# Asset Policy

**Version:** 1.0 | **Effective Date:** 1 April 2026 | **Next Review:** 1 April 2027

## 1. Stolen Laptop

If a company laptop is stolen you must file an FIR with the police within forty eight hours of the theft.
"""
TABLES = ["unanswered_questions", "hr_tickets", "hr_leave_requests", "tool_audit_log", "pending_actions", "messages", "conversations"]


class ScriptedLLM:
    """Plays back each step's output, and records what it was shown."""

    def __init__(self, intent="policy_question", category="", standalone=None, answer=None, tool=None, tool_args=None,
                 tool_text="", retry_text="", summary_text="A summary.", fail_understanding=False,
                 asks_about_past=False):
        self.intent, self.category, self.standalone = intent, category, standalone
        self.answer = answer or StructuredAnswer(answered=True, answer="Eighteen days [1].", cited_chunk_ids=["1"])
        self.tool, self.tool_args, self.tool_text = tool, tool_args or {}, tool_text
        self.retry_text, self.summary_text = retry_text, summary_text
        self.fail_understanding = fail_understanding
        self.asks_about_past = asks_about_past
        self.seen: list[str] = []

    def with_structured_output(self, schema):
        def invoke(messages):
            self.seen.append(messages[1].content)
            if schema is Understanding:
                if self.fail_understanding:
                    raise RuntimeError("quota")
                question = messages[1].content.rsplit("Latest message: ", 1)[1]
                return Understanding(intent=self.intent, category=self.category,
                                     standalone_question=self.standalone or question,
                                     asks_about_past=self.asks_about_past)
            return self.answer

        def stream(messages):
            self.seen.append(messages[1].content)
            yield {"answered": self.answer.answered, "answer": self.answer.answer[:5]}
            yield self.answer

        return SimpleNamespace(invoke=invoke, stream=stream)

    def invoke(self, messages):
        self.seen.append(messages[1].content)
        return SimpleNamespace(text=self.summary_text if "More of the chat" in messages[1].content else self.retry_text)

    def bind_tools(self, tools):
        def invoke(messages):
            self.seen.append(messages[1].content)
            calls = [{"name": self.tool, "args": self.tool_args, "id": "1", "type": "tool_call"}] if self.tool else []
            return AIMessage(content=self.tool_text, tool_calls=calls)

        return SimpleNamespace(invoke=invoke)


@pytest.fixture(scope="module")
def shared(database_uri):
    run_migrations(database_uri)
    store = get_store("workflow_test", database_uri, WordEmbeddings())
    index = PolicyIndex(store)
    documents = [DocumentChange(name, body, chunk_text(body, name))
                 for name, body in [("Leave_Policy.md", LEAVE), ("IT_Asset_and_Equipment_Policy.md", ASSETS)]]
    plan, changed = plan_documents(index, documents)
    apply_documents(index, plan, changed, wait_seconds=0)
    saver = create_checkpointer(database_uri)
    yield SimpleNamespace(index=index, engine=get_engine(database_uri), saver=saver, uri=database_uri)
    saver.conn.close()
    store.delete_collection()


@pytest.fixture(autouse=True)
def clean(shared, monkeypatch):
    with shared.engine.begin() as connection:
        for table in [*TABLES, "checkpoints", "checkpoint_writes", "checkpoint_blobs"]:
            connection.execute(text(f"DELETE FROM {table}"))
    # The tiny test corpus can't reach the real confidence levels, so judge by distance
    monkeypatch.setattr(config, "ABSTENTION_MODE", "distance")
    monkeypatch.setattr(config, "MAX_DISTANCE", 0.9)
    monkeypatch.setattr(config, "MIN_BODY_CHARS", 10)
    monkeypatch.setattr(config, "GRADE_MIN_CONFIDENCE", -100.0)
    monkeypatch.setattr(config, "TOOL_BACKOFF_SECONDS", 0)


def make(shared, llm=None, tenant_id=config.TENANT_ID, hrms=None) -> ChatWorkflow:
    llm = llm or ScriptedLLM()
    index = shared.index if tenant_id == config.TENANT_ID else PolicyIndex(shared.index.store, tenant_id)
    rag = RagService(index, llm)
    return ChatWorkflow(rag, llm, shared.engine, shared.saver, hrms=hrms, tenant_id=tenant_id, today=lambda: TODAY)


def rows(shared, table, where="TRUE"):
    with shared.engine.connect() as connection:
        return connection.execute(text(f"SELECT * FROM {table} WHERE {where} ORDER BY 1")).all()


def leave_llm(leave_type="earned", start=NEXT_MONDAY, end=NEXT_TUESDAY):
    return ScriptedLLM(intent="action_request", tool="apply_for_leave",
                       tool_args={"leave_type": leave_type, "start_date": start, "end_date": end})


# Routing

def test_policy_question_gets_a_cited_answer(shared):
    result = make(shared).ask("How many earned leave days do confirmed employees get?")
    assert result.intent == "policy_question"
    assert result.answer == "Eighteen days [1]."
    assert result.answered
    assert len(result.sources) == 1


def test_greeting_uses_no_model_call(shared):
    llm = ScriptedLLM()
    result = make(shared, llm).ask("Hello!")
    assert result.answer == GREETING_ANSWER
    assert llm.seen == []


def test_out_of_scope_is_politely_refused(shared):
    result = make(shared, ScriptedLLM(intent="out_of_scope")).ask("What is the capital of France?")
    assert result.answer == OUT_OF_SCOPE_ANSWER
    assert not result.answered
    assert result.chunks == []  # the check against the policies doesn't send anything to the answer step


def test_failed_understanding_falls_back_to_the_policy_path(shared):
    result = make(shared, ScriptedLLM(fail_understanding=True)).ask("How many earned leave days?")
    assert result.intent == "policy_question"
    assert result.answered


def test_personal_request_without_a_user_is_refused_before_any_tool(shared):
    result = make(shared, ScriptedLLM(intent="personal_data_request", tool="get_leave_balance")).ask("My balance?")
    assert result.abstain_reason == "needs_identity"
    assert rows(shared, "tool_audit_log") == []


def test_self_service_can_be_switched_off(shared, monkeypatch):
    monkeypatch.setattr(config, "SELF_SERVICE_ENABLED", False)
    llm = ScriptedLLM(intent="personal_data_request", tool="get_leave_balance")
    assert make(shared, llm).ask("My balance?", user_id="E1001").abstain_reason == "needs_identity"


def test_category_narrows_the_first_search_when_switched_on(shared, monkeypatch):
    monkeypatch.setattr(config, "USE_CATEGORY_ROUTING", True)
    llm = ScriptedLLM(category="leave", answer=StructuredAnswer(answered=False))
    result = make(shared, llm).ask("laptop stolen FIR police")
    assert {chunk.metadata["source"] for chunk in result.chunks} == {"Leave_Policy.md"}


def test_category_routing_is_off_by_default(shared):
    assert config.USE_CATEGORY_ROUTING is False
    llm = ScriptedLLM(category="leave", answer=StructuredAnswer(answered=False))
    result = make(shared, llm).ask("laptop stolen FIR police")
    assert "IT_Asset_and_Equipment_Policy.md" in {chunk.metadata["source"] for chunk in result.chunks}


def test_weak_evidence_rewrites_and_retries_once_without_the_category(shared, monkeypatch):
    monkeypatch.setattr(config, "USE_CATEGORY_ROUTING", True)
    monkeypatch.setattr(config, "GRADE_MIN_CONFIDENCE", 1000.0)
    llm = ScriptedLLM(category="general", retry_text="stolen company laptop FIR police")
    result = make(shared, llm).ask("zzz")
    assert "IT_Asset_and_Equipment_Policy.md" in {chunk.metadata["source"] for chunk in result.chunks}
    assert sum("Question: zzz" in shown and "excerpts" not in shown for shown in llm.seen) == 1


def test_a_retry_only_rewords_the_search_not_the_question_answered(shared, monkeypatch):
    monkeypatch.setattr(config, "GRADE_MIN_CONFIDENCE", 1000.0)
    llm = ScriptedLLM(retry_text="stolen company laptop FIR police")
    result = make(shared, llm).ask("My laptop went missing")
    assert result.question_used == "My laptop went missing"
    assert "Question: My laptop went missing" in llm.seen[-1]
    assert "stolen company laptop FIR police" not in llm.seen[-1]


def test_a_retry_that_finds_less_keeps_the_first_results(shared, monkeypatch):
    monkeypatch.setattr(config, "GRADE_MIN_CONFIDENCE", 1000.0)
    llm = ScriptedLLM(retry_text="completely unrelated zebra words")
    result = make(shared, llm).ask("How many earned leave days do confirmed employees get?")
    assert result.chunks[0].metadata["source"] == "Leave_Policy.md"
    assert result.answered


def test_a_strong_search_overrules_an_out_of_scope_label(shared, monkeypatch):
    monkeypatch.setattr(config, "OUT_OF_SCOPE_OVERRIDE_CONFIDENCE", -100.0)
    result = make(shared, ScriptedLLM(intent="out_of_scope")).ask("How many earned leave days do employees get?")
    assert result.intent == "policy_question"
    assert result.answered


def test_a_weak_search_keeps_the_out_of_scope_label(shared, monkeypatch):
    monkeypatch.setattr(config, "OUT_OF_SCOPE_OVERRIDE_CONFIDENCE", 1000.0)
    result = make(shared, ScriptedLLM(intent="out_of_scope")).ask("How many earned leave days do employees get?")
    assert result.intent == "out_of_scope"
    assert result.abstain_reason == "out_of_scope"


def test_no_evidence_after_the_retry_abstains(shared, monkeypatch):
    monkeypatch.setattr(config, "MAX_DISTANCE", 0.0001)
    llm = ScriptedLLM(retry_text="still nothing")
    result = make(shared, llm).ask("completely unrelated words")
    assert result.answer == FALLBACK_ANSWER
    assert result.abstain_reason == "no_evidence"


def test_streaming_sends_tokens_and_the_final_answer(shared):
    events = list(make(shared).stream("How many earned leave days do confirmed employees get?"))
    assert [value for kind, value in events if kind == "token"][0] == "Eight"
    assert events[-1][1].answer == "Eighteen days [1]."


def test_streaming_says_what_its_doing_in_fixed_words(shared):
    events = list(make(shared).stream("How many earned leave days do confirmed employees get?"))
    assert [value for kind, value in events if kind == "status"] == [
        "Understanding your question", "Searching policies", "Writing the answer"]


def test_tool_status_names_the_step_not_the_reasoning(shared):
    llm = ScriptedLLM(intent="personal_data_request", tool="get_leave_balance")
    events = list(make(shared, llm).stream("My balance?", user_id="E1001"))
    assert "Checking leave balance" in [value for kind, value in events if kind == "status"]


def test_stored_answer_has_a_message_id_for_feedback(shared):
    result = make(shared).ask("How many earned leave days?", user_id="E1001")
    assert result.message_id == rows(shared, "messages", "role = 'assistant'")[0].id


def test_a_question_the_bot_cant_answer_is_reported_without_a_user(shared):
    llm = ScriptedLLM(answer=StructuredAnswer(answered=False))
    make(shared, llm).ask("How many earned leave days?", user_id="E1001")
    stored = rows(shared, "unanswered_questions")
    assert [(row.question, row.reason) for row in stored] == [("How many earned leave days?", "model_abstained")]


def test_out_of_scope_questions_arent_reported_as_gaps(shared, monkeypatch):
    monkeypatch.setattr(config, "OUT_OF_SCOPE_OVERRIDE_CONFIDENCE", 1000.0)
    make(shared, ScriptedLLM(intent="out_of_scope")).ask("Capital of France?")
    assert rows(shared, "unanswered_questions") == []


def test_asking_about_the_past_searches_old_versions(shared):
    llm = ScriptedLLM(asks_about_past=True)
    make(shared, llm).ask("What was the leave policy before?")
    assert make(shared, llm).graph is not None


def test_normal_questions_dont_search_old_versions(shared, monkeypatch):
    seen = []
    workflow = make(shared)
    original = workflow.rag.search
    monkeypatch.setattr(workflow.rag, "search", lambda question, filters, timing=None: seen.append(filters) or original(question, filters, timing))
    workflow.ask("How many earned leave days?")
    assert seen[0].include_history is False


def test_past_questions_ask_for_history_in_the_search(shared, monkeypatch):
    seen = []
    workflow = make(shared, ScriptedLLM(asks_about_past=True))
    original = workflow.rag.search
    monkeypatch.setattr(workflow.rag, "search", lambda question, filters, timing=None: seen.append(filters) or original(question, filters, timing))
    workflow.ask("What did the leave policy say before?")
    # The search started while understanding can't know yet, so it's discarded and the real one includes history
    assert seen[-1].include_history is True
    assert all(not filters.include_history for filters in seen[:-1])


# Conversations

def test_chat_is_stored_and_continues(shared):
    llm = ScriptedLLM()
    workflow = make(shared, llm)
    first = workflow.ask("How many earned leave days?", user_id="E1001")
    workflow.ask("Can they carry over?", user_id="E1001", conversation_id=first.conversation_id)
    assert [m["role"] for m in workflow.conversations.messages("E1001", first.conversation_id)] == [
        "user", "assistant", "user", "assistant"]
    assert "How many earned leave days?" in [s for s in llm.seen if "Can they carry over?" in s][0]


def test_stored_chat_replaces_history_sent_by_the_caller(shared):
    llm = ScriptedLLM()
    make(shared, llm).ask("Question", history=[{"role": "user", "content": "FORGED"}], user_id="E1001")
    assert not any("FORGED" in shown for shown in llm.seen)


def test_earlier_answers_never_reach_the_answer_prompt(shared):
    llm = ScriptedLLM(standalone="earned leave days")
    workflow = make(shared, llm)
    first = workflow.ask("How many earned leave days?", user_id="E1001")
    llm.answer = StructuredAnswer(answered=True, answer="SECRET EARLIER ANSWER [1].", cited_chunk_ids=["1"])
    second = workflow.ask("Earned leave again?", user_id="E1001", conversation_id=first.conversation_id)
    llm.seen.clear()
    workflow.ask("Earned leave once more?", user_id="E1001", conversation_id=second.conversation_id)
    understanding, answer_prompt = llm.seen[0], llm.seen[1]
    assert "SECRET EARLIER ANSWER" in understanding
    assert "SECRET EARLIER ANSWER" not in answer_prompt


def test_one_user_cannot_see_or_continue_or_delete_anothers_chat(shared):
    workflow = make(shared)
    chat = workflow.ask("How many earned leave days?", user_id="E1001").conversation_id
    with pytest.raises(ConversationNotFound):
        workflow.conversations.messages("E1002", chat)
    with pytest.raises(ConversationNotFound):
        workflow.ask("Continue this", user_id="E1002", conversation_id=chat)
    with pytest.raises(ConversationNotFound):
        workflow.delete_conversation("E1002", chat)
    assert workflow.conversations.conversations("E1002") == []
    assert len(workflow.conversations.messages("E1001", chat)) == 2


def test_another_tenant_cannot_see_a_chat_for_the_same_user_id(shared):
    chat = make(shared).ask("How many earned leave days?", user_id="E1001").conversation_id
    other = make(shared, tenant_id="other-tenant")
    assert other.conversations.conversations("E1001") == []
    with pytest.raises(ConversationNotFound):
        other.conversations.messages("E1001", chat)
    assert other.conversations.delete_all("E1001") == 0
    assert other.conversations.purge_older_than(1) == 0


def test_deleting_a_chat_removes_its_messages(shared):
    workflow = make(shared)
    chat = workflow.ask("How many earned leave days?", user_id="E1001").conversation_id
    workflow.delete_conversation("E1001", chat)
    assert rows(shared, "messages") == []


def test_deleting_a_users_history_leaves_other_users_alone(shared):
    workflow = make(shared)
    workflow.ask("One", user_id="E1001")
    workflow.ask("Two", user_id="E1001")
    workflow.ask("Three", user_id="E1002")
    assert workflow.delete_user_history("E1001") == 2
    assert workflow.conversations.conversations("E1001") == []
    assert len(workflow.conversations.conversations("E1002")) == 1


def test_retention_removes_only_old_chats(shared):
    workflow = make(shared)
    old = workflow.ask("Old", user_id="E1001").conversation_id
    new = workflow.ask("New", user_id="E1001").conversation_id
    with shared.engine.begin() as connection:
        connection.execute(text("UPDATE conversations SET updated_at = now() - interval '100 days' WHERE id = :id"),
                           {"id": old})
    workflow.run_maintenance(force=True)
    assert [chat["id"] for chat in workflow.conversations.conversations("E1001")] == [new]


def test_long_chats_are_summarised_and_the_summary_is_used(shared, monkeypatch):
    monkeypatch.setattr(config, "SUMMARISE_AFTER_MESSAGES", 4)
    monkeypatch.setattr(config, "KEEP_RECENT_MESSAGES", 2)
    llm = ScriptedLLM(summary_text="They asked about earned leave.")
    workflow = make(shared, llm)
    chat = workflow.ask("One", user_id="E1001").conversation_id
    workflow.ask("Two", user_id="E1001", conversation_id=chat)
    assert rows(shared, "conversations")[0].summary == "They asked about earned leave."
    assert rows(shared, "conversations")[0].summarised_messages == 2
    workflow.ask("Three", user_id="E1001", conversation_id=chat)
    assert any("Summary of the earlier chat:\nThey asked about earned leave." in shown for shown in llm.seen)


def test_failed_summary_doesnt_stop_the_answer(shared, monkeypatch):
    monkeypatch.setattr(config, "SUMMARISE_AFTER_MESSAGES", 2)
    monkeypatch.setattr(config, "KEEP_RECENT_MESSAGES", 1)
    llm = ScriptedLLM()
    llm.invoke = lambda messages: (_ for _ in ()).throw(RuntimeError("quota"))
    assert make(shared, llm).ask("How many earned leave days?", user_id="E1001").answered


def test_finished_runs_leave_no_checkpoint(shared):
    make(shared).ask("How many earned leave days?", user_id="E1001")
    with psycopg.connect(shared.uri) as connection:
        assert connection.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 0


# Tools

def test_leave_balance_uses_the_signed_in_employee(shared):
    llm = ScriptedLLM(intent="personal_data_request", tool="get_leave_balance")
    result = make(shared, llm).ask("How much leave do I have?", user_id="E1002")
    assert "1 earned, 12 sick and 0 casual" in result.answer  # Rohan's numbers, not anyone else's
    assert result.tool == "get_leave_balance"
    assert result.tool_status == "success"


def test_tool_arguments_cant_name_another_employee(shared):
    llm = ScriptedLLM(intent="personal_data_request", tool="get_latest_payslip", tool_args={"employee_id": "E1002"})
    result = make(shared, llm).ask("Show E1002's payslip", user_id="E1001")
    assert "73,800" in result.answer  # the asker's own payslip
    assert "132,600" not in result.answer


def test_a_figure_from_the_model_is_never_shown_as_a_record(shared):
    llm = ScriptedLLM(intent="personal_data_request", tool_text="Your net pay is Rs. 999,999.")
    result = make(shared, llm).ask("How many earned leave days do confirmed employees get?", user_id="E1001")
    assert "999,999" not in result.answer


def test_a_question_no_tool_fits_goes_to_the_policy_search(shared):
    llm = ScriptedLLM(intent="personal_data_request", tool_text="Which details do you want?")
    result = make(shared, llm).ask("How many earned leave days do confirmed employees get?", user_id="E1001")
    assert result.intent == "policy_question"
    assert result.answer == "Eighteen days [1]."
    assert result.sources


def test_a_clarifying_question_is_passed_on(shared):
    llm = ScriptedLLM(intent="action_request", tool_text="Which dates would you like?")
    result = make(shared, llm).ask("Apply for leave", user_id="E1001")
    assert result.answer == "Which dates would you like?"
    assert rows(shared, "pending_actions") == []


def test_ticket_is_raised_and_audited(shared):
    llm = ScriptedLLM(intent="action_request", tool="raise_hr_ticket",
                      tool_args={"subject": "Wrong PAN", "description": "My PAN is wrong in HRMS"})
    result = make(shared, llm).ask("Raise a ticket about my PAN", user_id="E1001")
    assert "HR ticket" in result.answer
    assert rows(shared, "hr_tickets")[0].employee_id == "E1001"
    audit = rows(shared, "tool_audit_log")
    assert (audit[0].tool, audit[0].status, audit[0].attempts, audit[0].user_id) == ("raise_hr_ticket", "success", 1, "E1001")


@pytest.mark.parametrize("tool,args", [
    ("raise_hr_ticket", {"subject": "Manager address", "description": "Their home address"}),
    ("apply_for_leave", {"leave_type": "earned", "start_date": NEXT_MONDAY, "end_date": NEXT_TUESDAY}),
])
def test_a_request_for_information_can_never_change_records(shared, tool, args):
    llm = ScriptedLLM(intent="personal_data_request", tool=tool, tool_args=args)
    result = make(shared, llm).ask("What is my manager's home address?", user_id="E1001")
    assert result.tool == ""
    assert rows(shared, "hr_tickets") == []
    assert rows(shared, "pending_actions") == []
    assert rows(shared, "tool_audit_log") == []


def test_unknown_tool_is_refused_and_audited(shared):
    llm = ScriptedLLM(intent="action_request", tool="delete_all_employees")
    result = make(shared, llm).ask("Delete everyone", user_id="E1001")
    assert result.tool_status == "unknown_tool"
    assert rows(shared, "tool_audit_log")[0].status == "unknown_tool"


def test_bad_ticket_arguments_are_refused_and_audited(shared):
    llm = ScriptedLLM(intent="action_request", tool="raise_hr_ticket", tool_args={"subject": "x"})
    result = make(shared, llm).ask("Raise a ticket", user_id="E1001")
    assert result.tool_status == "invalid_arguments"
    assert rows(shared, "hr_tickets") == []


class FlakyHRMS(MockHRMS):
    def __init__(self, *args, failures, **kwargs):
        super().__init__(*args, **kwargs)
        self.failures = failures
        self.calls = 0

    def leave_balance(self, employee_id):
        self.calls += 1
        if self.calls <= self.failures:
            raise HRMSUnavailable
        return super().leave_balance(employee_id)


def test_temporary_failures_are_retried_with_backoff(shared, monkeypatch):
    monkeypatch.setattr(config, "TOOL_BACKOFF_SECONDS", 0.5)
    hrms = FlakyHRMS(shared.engine, config.TENANT_ID, failures=2)
    llm = ScriptedLLM(intent="personal_data_request", tool="get_leave_balance")
    workflow = make(shared, llm, hrms=hrms)
    waits = []
    workflow.runner.sleep = waits.append
    result = workflow.ask("My balance?", user_id="E1001")
    assert result.tool_status == "success"
    assert waits == [0.5, 1.0]
    assert rows(shared, "tool_audit_log")[0].attempts == 3


def test_lasting_failure_gives_a_clean_message_and_is_audited(shared):
    hrms = FlakyHRMS(shared.engine, config.TENANT_ID, failures=99)
    llm = ScriptedLLM(intent="personal_data_request", tool="get_leave_balance")
    result = make(shared, llm, hrms=hrms).ask("My balance?", user_id="E1001")
    assert result.tool_status == "failed"
    assert "isn't available" in result.answer
    assert rows(shared, "tool_audit_log")[0].status == "failed"


# Approval

def test_leave_isnt_applied_until_approved(shared):
    result = make(shared, leave_llm()).ask("Apply for leave next Monday and Tuesday", user_id="E1001")
    assert result.pending_action["summary"] == f"apply for earned leave from {NEXT_MONDAY} to {NEXT_TUESDAY}"
    assert rows(shared, "hr_leave_requests") == []
    assert rows(shared, "tool_audit_log") == []
    assert rows(shared, "pending_actions")[0].status == "pending"


def test_approving_applies_the_leave_once_and_audits_it(shared):
    workflow = make(shared, leave_llm())
    action = workflow.ask("Apply for leave", user_id="E1001").pending_action["id"]
    result = workflow.decide("E1001", action, approve=True)
    assert "has been submitted" in result.answer
    requests = rows(shared, "hr_leave_requests")
    assert [(r.employee_id, r.leave_type, r.days) for r in requests] == [("E1001", "earned", 2)]
    audit = rows(shared, "tool_audit_log")
    assert (audit[0].status, audit[0].approval_id) == ("success", action)
    assert workflow.runner.hrms.leave_balance("E1001")["earned"] == 11


def test_declining_changes_nothing_and_says_so(shared):
    workflow = make(shared, leave_llm())
    action = workflow.ask("Apply for leave", user_id="E1001").pending_action["id"]
    result = workflow.decide("E1001", action, approve=False)
    assert "Nothing was changed" in result.answer
    assert rows(shared, "hr_leave_requests") == []
    assert rows(shared, "pending_actions")[0].status == "rejected"


def test_approval_survives_a_restart(shared):
    chat = make(shared, leave_llm()).ask("Apply for leave", user_id="E1001")
    restarted = make(shared, leave_llm())  # a new workflow, as after Render sleeps
    result = restarted.decide("E1001", chat.pending_action["id"], approve=True)
    assert "has been submitted" in result.answer
    assert len(rows(shared, "hr_leave_requests")) == 1


def test_only_the_employee_can_decide_their_action(shared):
    workflow = make(shared, leave_llm())
    action = workflow.ask("Apply for leave", user_id="E1001").pending_action["id"]
    with pytest.raises(ActionNotFound):
        workflow.decide("E1002", action, approve=True)
    assert rows(shared, "hr_leave_requests") == []


def test_another_tenant_cannot_decide_an_action(shared):
    action = make(shared, leave_llm()).ask("Apply for leave", user_id="E1001").pending_action["id"]
    with pytest.raises(ActionNotFound):
        make(shared, leave_llm(), tenant_id="other-tenant").decide("E1001", action, approve=True)


def test_an_action_can_only_be_decided_once(shared):
    workflow = make(shared, leave_llm())
    action = workflow.ask("Apply for leave", user_id="E1001").pending_action["id"]
    workflow.decide("E1001", action, approve=True)
    with pytest.raises(ActionNotPending):
        workflow.decide("E1001", action, approve=True)
    assert len(rows(shared, "hr_leave_requests")) == 1


def test_an_expired_action_cant_be_approved(shared):
    workflow = make(shared, leave_llm())
    action = workflow.ask("Apply for leave", user_id="E1001").pending_action["id"]
    with shared.engine.begin() as connection:
        connection.execute(text("UPDATE pending_actions SET expires_at = now() - interval '1 hour'"))
    with pytest.raises(ActionNotPending):
        workflow.decide("E1001", action, approve=True)
    workflow.run_maintenance(force=True)
    assert rows(shared, "pending_actions")[0].status == "expired"
    assert rows(shared, "hr_leave_requests") == []


def test_execution_is_blocked_without_a_recorded_approval(shared):
    """Even if something reached the execution step directly, the database check stops it."""
    workflow = make(shared)
    context = ToolContext(config.TENANT_ID, "E1001", None, "run", TODAY)
    args = {"leave_type": "earned", "start_date": NEXT_MONDAY, "end_date": NEXT_TUESDAY}
    for approval in (None, "made-up-id"):
        outcome = workflow.runner.execute(context, "apply_for_leave", args, approval)
        assert outcome.status == "blocked"
    assert rows(shared, "hr_leave_requests") == []
    assert [row.status for row in rows(shared, "tool_audit_log")] == ["blocked", "blocked"]


def test_someone_elses_approval_cant_be_reused(shared):
    workflow = make(shared, leave_llm())
    action = workflow.ask("Apply for leave", user_id="E1001").pending_action["id"]
    with shared.engine.begin() as connection:
        connection.execute(text("UPDATE pending_actions SET status = 'approved'"))
    context = ToolContext(config.TENANT_ID, "E1002", None, "run", TODAY)
    args = {"leave_type": "sick", "start_date": NEXT_MONDAY, "end_date": NEXT_MONDAY}
    assert workflow.runner.execute(context, "apply_for_leave", args, action).status == "blocked"


def test_only_leave_needs_approval_today():
    assert {name for name, spec in TOOLS.items() if spec.requires_approval} == {"apply_for_leave"}


@pytest.mark.parametrize("llm_args,expected", [
    ({"leave_type": "earned", "start_date": "2026-11-05", "end_date": "2026-11-04"}, "before the start"),
    ({"leave_type": "earned", "start_date": "2020-01-06", "end_date": "2020-01-06"}, "in the past"),
    ({"leave_type": "holiday", "start_date": NEXT_MONDAY, "end_date": NEXT_MONDAY}, "must be one of"),
    ({"leave_type": "earned", "start_date": "2026-11-07", "end_date": "2026-11-08"}, "working day"),
    ({"leave_type": "casual", "start_date": NEXT_MONDAY, "end_date": "2026-11-20"}, "you have 5 left"),
])
def test_impossible_leave_is_refused_before_asking_for_approval(shared, llm_args, expected):
    llm = ScriptedLLM(intent="action_request", tool="apply_for_leave", tool_args=llm_args)
    result = make(shared, llm).ask("Apply for leave", user_id="E1001")
    assert expected in result.answer
    assert result.pending_action is None
    assert rows(shared, "pending_actions") == []


def test_missing_leave_details_ask_again(shared):
    llm = ScriptedLLM(intent="action_request", tool="apply_for_leave", tool_args={"leave_type": "earned"})
    result = make(shared, llm).ask("Apply for leave", user_id="E1001")
    assert result.tool_status == "invalid_arguments"
    assert result.pending_action is None


def test_deleting_a_chat_cancels_its_waiting_action(shared):
    workflow = make(shared, leave_llm())
    chat = workflow.ask("Apply for leave", user_id="E1001")
    workflow.delete_conversation("E1001", chat.conversation_id)
    assert rows(shared, "pending_actions")[0].status == "cancelled"
    with pytest.raises(ActionNotPending):
        workflow.decide("E1001", chat.pending_action["id"], approve=True)
    with psycopg.connect(shared.uri) as connection:
        assert connection.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 0


def test_same_key_cant_apply_twice(shared):
    hrms = MockHRMS(shared.engine, config.TENANT_ID)
    start, end = date(2026, 11, 9), date(2026, 11, 10)
    first = hrms.apply_for_leave("E1001", "earned", start, end, "", "key-1", TODAY)
    second = hrms.apply_for_leave("E1001", "earned", start, end, "", "key-1", TODAY)
    assert first == second
    assert len(rows(shared, "hr_leave_requests")) == 1


def test_unknown_employee_is_refused(shared):
    with pytest.raises(HRMSRejected):
        MockHRMS(shared.engine, config.TENANT_ID).leave_balance("E9999")


def test_working_days_skip_weekends():
    friday = date(2026, 11, 6)
    assert working_days(friday, friday + timedelta(days=3)) == 2


# Choosing not to save chats

def test_a_chat_that_is_not_saved_stores_nothing(shared):
    workflow = make(shared)
    result = workflow.ask("How much earned leave do I get?", user_id="E1001", save_history=False)
    assert result.answer
    assert result.conversation_id is None
    assert result.message_id is None
    assert workflow.conversations.conversations("E1001") == []
    assert rows(shared, "messages") == []


def test_a_follow_up_still_works_when_chats_are_not_saved(shared):
    llm = ScriptedLLM()
    workflow = make(shared, llm)
    workflow.ask("What about sick leave?", [{"role": "user", "content": "How much earned leave?"},
                                            {"role": "assistant", "content": "Twenty days."}],
                 user_id="E1001", save_history=False)
    assert any("How much earned leave?" in seen for seen in llm.seen)
    assert workflow.conversations.conversations("E1001") == []


def test_an_unanswered_question_is_not_kept_when_chats_are_not_saved(shared, monkeypatch):
    llm = ScriptedLLM()
    llm.answer = StructuredAnswer(answered=False)
    workflow = make(shared, llm)
    workflow.ask("How much earned leave do I get?", user_id="E1001", save_history=False)
    assert rows(shared, "unanswered_questions") == []


def test_saving_chats_is_on_until_someone_turns_it_off(shared):
    store = make(shared).conversations
    assert store.saves_history("E1001") is True
    store.set_saves_history("E1001", False)
    assert store.saves_history("E1001") is False
    assert store.saves_history("E1002") is True
    store.set_saves_history("E1001", True)
    assert store.saves_history("E1001") is True


def test_a_saved_chat_is_listed_with_its_first_question_as_the_title(shared):
    workflow = make(shared)
    workflow.ask("How much earned leave do I get?", user_id="E1001")
    assert workflow.conversations.conversations("E1001")[0]["title"] == "How much earned leave do I get?"


# Speed: step timings, the search made while understanding, and a separate router model

def counting_searches(monkeypatch, workflow, fail_first=False):
    """Record each search's query, and optionally make the first one fail."""
    queries: list[str] = []
    original = workflow.rag.search

    def search(question, filters, timing=None):
        queries.append(question)
        if fail_first and len(queries) == 1:
            raise RuntimeError("database busy")
        return original(question, filters, timing)

    monkeypatch.setattr(workflow.rag, "search", search)
    return queries


def test_each_step_of_an_answer_is_timed_and_kept_with_its_metrics(shared):
    workflow = make(shared)
    answer = workflow.ask("How many earned leave days?")
    assert {"understand", "retrieve", "generate"} <= set(answer.step_ms)
    assert all(milliseconds >= 0 for milliseconds in answer.step_ms.values())
    with shared.engine.connect() as connection:
        stored = connection.execute(text("SELECT step_ms FROM answer_metrics ORDER BY id DESC LIMIT 1")).scalar()
    assert set(stored) == set(answer.step_ms)
    steps = workflow.metrics.summary()["steps_ms"]
    assert steps["understand"]["count"] >= 1 and steps["generate"]["p50"] is not None


def test_a_first_message_is_searched_once_while_it_is_understood(shared, monkeypatch):
    for early in (True, False):
        monkeypatch.setattr(config, "SEARCH_WHILE_UNDERSTANDING", early)
        workflow = make(shared)
        queries = counting_searches(monkeypatch, workflow)
        answer = workflow.ask("How many earned leave days?")
        assert queries == ["How many earned leave days?"], early
        assert answer.sources and answer.sources[0]["file"] == "Leave_Policy.md"


def test_a_follow_up_is_only_searched_after_it_is_rewritten(shared, monkeypatch):
    llm = ScriptedLLM(standalone="How many earned leave days do employees on probation get?")
    workflow = make(shared, llm)
    queries = counting_searches(monkeypatch, workflow)
    workflow.ask("And on probation?", history=[{"role": "user", "content": "How many earned leave days?"},
                                              {"role": "assistant", "content": "Eighteen."}])
    assert queries == ["How many earned leave days do employees on probation get?"]


def test_an_early_search_is_dropped_for_a_tool_request(shared, monkeypatch):
    llm = ScriptedLLM(intent="personal_data_request", tool="get_leave_balance")
    workflow = make(shared, llm)
    queries = counting_searches(monkeypatch, workflow)
    answer = workflow.ask("How many leave days do I have left?", user_id="E1001")
    assert answer.tool == "get_leave_balance"
    assert len(queries) == 1  # the early search ran, and only cost that
    assert not answer.sources


def test_a_failed_early_search_is_simply_done_again(shared, monkeypatch):
    workflow = make(shared)
    queries = counting_searches(monkeypatch, workflow, fail_first=True)
    answer = workflow.ask("How many earned leave days?")
    assert len(queries) == 2
    assert answer.answered and answer.sources


def test_a_router_model_reads_messages_and_the_main_model_writes_answers(shared):
    router, writer = ScriptedLLM(), ScriptedLLM()
    rag = RagService(shared.index, writer)
    workflow = ChatWorkflow(rag, writer, shared.engine, shared.saver, today=lambda: TODAY, router=router)
    answer = workflow.ask("How many earned leave days?")
    assert answer.answered
    assert any("Latest message: How many earned leave days?" in seen for seen in router.seen)
    assert not any("Latest message:" in seen for seen in writer.seen)
    assert writer.seen  # the answer itself came from the main model
