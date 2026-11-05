"""Retrieval and answer logic, using fakes so no API is called."""
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document

from app import config
from app.rag.prompt import FALLBACK_ANSWER, GREETING_ANSWER, build_messages
from app.rag.service import RagService

LONG_TEXT = "Confirmed employees get 18 days of Earned Leave every calendar year. " * 3


def make_chunk(section="4.1. Earned Leave", body=LONG_TEXT):
    return Document(
        page_content=f"Leave Policy > {section}\n\n{body}",
        metadata={"policy": "Leave Policy", "section": section, "source": "Leave_Policy.md"},
    )


class FakeStore:
    def __init__(self, pairs):
        self.pairs = pairs
        self.embeddings = SimpleNamespace(embed_query=lambda question: [0.1, 0.2])

    def max_marginal_relevance_search_with_score_by_vector(self, vector, k, fetch_k, lambda_mult):
        return self.pairs


class FakeLLM:
    def __init__(self, answer="18 days."):
        self.answer = answer
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return SimpleNamespace(text=self.answer)


def test_chunks_too_far_in_meaning_are_dropped():
    near, far = make_chunk("near"), make_chunk("far")
    service = RagService(FakeStore([(near, 0.2), (far, config.MAX_DISTANCE + 0.01)]), FakeLLM())
    assert service.retrieve("leave?") == [near]


def test_lead_in_chunks_and_duplicates_are_dropped():
    chunk = make_chunk()
    lead_in = make_chunk("lead-in", "The following rules apply.")
    service = RagService(FakeStore([(chunk, 0.2), (chunk, 0.21), (lead_in, 0.2)]), FakeLLM())
    assert service.retrieve("leave?") == [chunk]


def test_no_relevant_chunks_refuses_without_calling_the_llm():
    llm = FakeLLM()
    result = RagService(FakeStore([(make_chunk(), 0.99)]), llm).ask("Capital of France?")
    assert result.answer == FALLBACK_ANSWER
    assert result.sources == []
    assert llm.calls == []


def test_answer_lists_each_section_once():
    first, second = make_chunk("4.1. Earned Leave"), make_chunk("4.1. Earned Leave", LONG_TEXT + "Part 2.")
    result = RagService(FakeStore([(first, 0.2), (second, 0.3)]), FakeLLM()).ask("Leave?")
    assert result.answer == "18 days."
    assert result.sources == [{"policy": "Leave Policy", "section": "4.1. Earned Leave",
                               "file": "Leave_Policy.md"}]


def test_llm_refusal_has_no_sources():
    result = RagService(FakeStore([(make_chunk(), 0.2)]), FakeLLM(FALLBACK_ANSWER)).ask("Gym?")
    assert result.answer == FALLBACK_ANSWER
    assert result.sources == []


@pytest.mark.parametrize("greeting", ["Hi", "Hii", "hello!", "Hey", "Good morning.", " namaste "])
def test_greetings_get_a_welcome_without_any_api_call(greeting):
    llm = FakeLLM()
    result = RagService(FakeStore([]), llm).ask(greeting)
    assert result.answer == GREETING_ANSWER
    assert llm.calls == []


@pytest.mark.parametrize("question", ["Hi, how much leave do I get?", "Hello, when is salary credited?"])
def test_question_after_a_greeting_is_still_answered(question):
    result = RagService(FakeStore([(make_chunk(), 0.2)]), FakeLLM()).ask(question)
    assert result.answer == "18 days."


def test_prompt_contains_context_question_and_fallback():
    system, human = build_messages("How much leave?", [make_chunk()])
    assert FALLBACK_ANSWER in system.content
    assert "How much leave?" in human.content
    assert "18 days of Earned Leave" in human.content
