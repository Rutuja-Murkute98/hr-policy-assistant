"""Answers for the person asking, through the whole workflow on a real PostgreSQL. No model API is called.

Asha (E1001) is confirmed, and Meera (E1003) is on probation, in the fake HR data. The work from home policy has one
section for everyone and one only for employees on probation.
"""
import pytest
from sqlalchemy import text

from app import config
from app.agent.hrms import MockHRMS
from app.audience import Audience, AudienceStore
from app.persona import Persona
from app.rag.prompt import StructuredAnswer
from pipeline.chunk_documents import chunk_text
from pipeline.embed_and_store import DocumentChange, apply_documents, plan_documents
from tests.integration import test_workflow as workflow_tests
from tests.integration.test_workflow import ScriptedLLM, make

pytestmark = pytest.mark.integration
shared = workflow_tests.shared  # the workflow tests' policies and database
clean = workflow_tests.clean

WFH = """# Work From Home Policy

**Version:** 1.0 | **Effective Date:** 1 April 2026 | **Next Review:** 1 April 2027

## 1. Eligibility

Confirmed employees can work from home up to two days a week with their manager's approval under the work from home rules.

## 2. Employees on Probation

Employees on probation cannot work from home during their probation period under the work from home rules, except with HR approval.
"""
QUESTION = "Can I work from home under the work from home rules?"


@pytest.fixture
def wfh(shared, monkeypatch):
    """Index the work from home policy, and tag its probation section, as HR would."""
    chunks = chunk_text(WFH, "Work_From_Home_Policy.md")
    plan, changed = plan_documents(shared.index, [DocumentChange("Work_From_Home_Policy.md", WFH, chunks)])
    apply_documents(shared.index, plan, changed, wait_seconds=0)
    store = AudienceStore(shared.engine, config.TENANT_ID)
    probation = next(chunk for chunk in chunks if "Probation" in chunk.id)
    store.save(probation, Audience(probation="on_probation"), "hr", "hr-admin")
    monkeypatch.setattr(config, "AUDIENCE_WEIGHT", 1.0)  # the tiny corpus' scores are close, so make the order clear
    yield store
    with shared.engine.begin() as connection:
        connection.execute(text("DELETE FROM section_audiences"))


def excerpts_order(prompt: str) -> list[str]:
    return [name for _, name in sorted((prompt.index(name), name) for name in ("Eligibility", "Employees on Probation")
                                       if name in prompt)]


def test_each_employee_gets_their_own_details_and_rules_first(shared, wfh):
    llm = ScriptedLLM()
    workflow = make(shared, llm)
    meera = workflow.ask(QUESTION, user_id="E1003", save_history=False)
    meera_prompt = llm.seen[-1]
    assert "About the employee asking: Band 2, based in Nashik, a permanent employee, on probation." in meera_prompt
    assert "(applies to: employees on probation)" in meera_prompt
    assert excerpts_order(meera_prompt) == ["Employees on Probation", "Eligibility"]
    assert meera.persona == {"band": 2, "location": "Nashik", "employment_type": "permanent", "on_probation": True}

    asha = workflow.ask(QUESTION, user_id="E1001", save_history=False)
    asha_prompt = llm.seen[-1]
    assert "confirmed, not on probation" in asha_prompt and "Nashik" not in asha_prompt
    assert excerpts_order(asha_prompt) == ["Eligibility", "Employees on Probation"]  # moved down, not removed
    assert asha.persona["on_probation"] is False


def test_an_anonymous_question_is_answered_as_before(shared, wfh):
    llm = ScriptedLLM()
    answer = make(shared, llm).ask(QUESTION, save_history=False)
    assert "About the employee" not in llm.seen[-1] and "applies to" not in llm.seen[-1]
    assert answer.persona is None


def test_the_employees_correction_is_used(shared, wfh):
    llm = ScriptedLLM()
    answer = make(shared, llm).ask(QUESTION, user_id="E1001", save_history=False,
                                   about_me=Persona(on_probation=True, location="Mumbai"))
    assert "Band 3, based in Mumbai, a permanent employee, on probation." in llm.seen[-1]
    assert answer.persona["location"] == "Mumbai"
    assert excerpts_order(llm.seen[-1]) == ["Employees on Probation", "Eligibility"]


def test_a_correction_never_reaches_another_persons_question(shared, wfh):
    llm = ScriptedLLM()
    workflow = make(shared, llm)
    workflow.ask(QUESTION, user_id="E1001", save_history=False, about_me=Persona(band=7))
    workflow.ask("How much earned leave do I get?", user_id="E1002", save_history=False)
    assert "Band 5, based in Mumbai" in llm.seen[-1] and "Band 7" not in llm.seen[-1]


def test_one_persons_cached_answer_is_never_served_to_another(shared, wfh):
    llm = ScriptedLLM(answer=StructuredAnswer(answered=True, answer="Not on probation [1].", cited_chunk_ids=["1"]))
    workflow = make(shared, llm)
    workflow.ask(QUESTION, user_id="E1003", save_history=False)
    asked = len(llm.seen)
    workflow.ask(QUESTION, user_id="E1001", save_history=False)
    assert len(llm.seen) > asked  # a different person, so the answer was written again
    asked = len(llm.seen)
    again = workflow.ask(QUESTION, user_id="E1003", save_history=False)
    assert len(llm.seen) == asked and again.intent == "policy_question"  # the same person, so it was reused


def test_an_hr_system_failure_still_gets_the_general_answer(shared, wfh):
    class BrokenHRMS(MockHRMS):
        def persona(self, employee_id, today):
            raise RuntimeError("HR system down")

    llm = ScriptedLLM()
    workflow = make(shared, llm, hrms=BrokenHRMS(shared.engine, config.TENANT_ID))
    answer = workflow.ask(QUESTION, user_id="E1003", save_history=False)
    assert answer.answered and answer.persona is None and "About the employee" not in llm.seen[-1]


def test_another_organisations_employee_has_no_details_here(shared, wfh):
    llm = ScriptedLLM()
    answer = make(shared, llm, tenant_id="acme").ask(QUESTION, user_id="E1003", save_history=False)
    assert answer.persona is None  # E1003 at Acme is a different person, with no record in this HR data


def test_a_tag_belongs_to_the_text_it_was_made_from(shared, wfh):
    chunks = chunk_text(WFH, "Work_From_Home_Policy.md")
    probation = next(chunk for chunk in chunks if "Probation" in chunk.id)
    assert wfh.for_chunks([probation])[probation.id] == Audience(probation="on_probation")
    assert not wfh.save(probation, Audience(), "model")  # HR's correction is kept
    edited = probation.model_copy(update={"page_content": probation.page_content + " Updated."})
    assert wfh.for_chunks([edited]) == {}  # the text changed, so it needs checking again
    assert wfh.untagged([edited]) == [edited]
    assert wfh.save(edited, Audience(bands=[1]), "model")  # and the model may tag the new text
    assert wfh.for_chunks([edited])[edited.id] == Audience(bands=[1])


def test_one_organisations_tags_are_hidden_from_another(shared, wfh):
    chunks = chunk_text(WFH, "Work_From_Home_Policy.md")
    assert AudienceStore(shared.engine, "acme").for_chunks(chunks) == {}
    assert AudienceStore(shared.engine, "acme").listed() == []
    assert len(wfh.listed("Work_From_Home_Policy.md")) == 1
