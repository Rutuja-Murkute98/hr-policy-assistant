"""Retrieval and answer logic, using fakes so no API is called."""
from types import SimpleNamespace

import numpy as np
import pytest
from langchain_core.documents import Document

from app import config
from app.rag.prompt import FALLBACK_ANSWER, GREETING_ANSWER, build_messages
from app.rag.retrieval import Candidate, keep_usable, mmr, relevance_scores, select_chunks
from app.rag.service import RagService

LONG_TEXT = "Confirmed employees get 18 days of Earned Leave every calendar year. " * 3


def make_chunk(section="4.1. Earned Leave", body=LONG_TEXT):
    return Document(
        page_content=f"Leave Policy > {section}\n\n{body}",
        metadata={"policy": "Leave Policy", "section": section, "source": "Leave_Policy.md"},
    )


class FakeStore:
    """Returns the given (chunk, distance) pairs, each with a different embedding."""

    def __init__(self, pairs, keyword_ranks=None):
        self.pairs = pairs
        self.keyword_ranks = keyword_ranks or {}
        self.embeddings = SimpleNamespace(embed_query=lambda question: [0.1, 0.2])
        self.searched_for = []

    def candidates(self, vector, question, k, use_keywords=True):
        self.searched_for.append(question)
        size = max(len(self.pairs), 1)
        return [Candidate(chunk, distance, list(np.eye(size)[index]), self.keyword_ranks.get(index))
                for index, (chunk, distance) in enumerate(self.pairs)]


class FakeLLM:
    def __init__(self, answer="18 days.", rewrite="How much earned leave do I get on probation?", fail_rewrite=False):
        self.answer = answer
        self.rewrite = rewrite
        self.fail_rewrite = fail_rewrite
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        if "Follow-up question:" in messages[1].content:
            if self.fail_rewrite:
                raise RuntimeError("quota")
            return SimpleNamespace(text=self.rewrite)
        return SimpleNamespace(text=self.answer)

    def stream(self, messages):
        self.calls.append(messages)
        for word in self.answer.split(" "):
            yield SimpleNamespace(text=word + " ")
        yield SimpleNamespace(text="")


HISTORY = [{"role": "user", "content": "How much earned leave do I get?"},
           {"role": "assistant", "content": "18 days."}]


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


# Chat memory

def test_no_history_means_no_rewrite():
    llm, store = FakeLLM(), FakeStore([(make_chunk(), 0.2)])
    RagService(store, llm).ask("How much leave?")
    assert store.searched_for == ["How much leave?"]
    assert len(llm.calls) == 1


def test_follow_up_is_rewritten_before_searching_and_answering():
    llm, store = FakeLLM(), FakeStore([(make_chunk(), 0.2)])
    result = RagService(store, llm).ask("What about on probation?", HISTORY)
    assert store.searched_for == ["How much earned leave do I get on probation?"]
    assert result.question_used == "How much earned leave do I get on probation?"
    assert "How much earned leave do I get on probation?" in llm.calls[-1][1].content


def test_earlier_answers_never_reach_the_answer_prompt():
    llm = FakeLLM()
    RagService(FakeStore([(make_chunk(), 0.2)]), llm).ask("What about on probation?",
                                                          [{"role": "assistant", "content": "SECRET EARLIER ANSWER"}])
    assert "SECRET EARLIER ANSWER" in llm.calls[0][1].content  # only the rewrite step sees the chat
    assert "SECRET EARLIER ANSWER" not in llm.calls[-1][1].content


def test_failed_rewrite_falls_back_to_the_original_question():
    llm, store = FakeLLM(fail_rewrite=True), FakeStore([(make_chunk(), 0.2)])
    result = RagService(store, llm).ask("What about on probation?", HISTORY)
    assert store.searched_for == ["What about on probation?"]
    assert result.answer == "18 days."


@pytest.mark.parametrize("bad_rewrite", ["", "x" * (config.MAX_QUESTION_CHARS + 1)])
def test_unusable_rewrite_falls_back_to_the_original_question(bad_rewrite):
    store = FakeStore([(make_chunk(), 0.2)])
    RagService(store, FakeLLM(rewrite=bad_rewrite)).ask("What about on probation?", HISTORY)
    assert store.searched_for == ["What about on probation?"]


def test_only_recent_history_is_used():
    llm = FakeLLM()
    history = [{"role": "user", "content": f"message {number}"} for number in range(10)]
    RagService(FakeStore([(make_chunk(), 0.2)]), llm).ask("And then?", history)
    assert "message 9" in llm.calls[0][1].content
    assert "message 3" not in llm.calls[0][1].content


# Streaming

def test_stream_sends_tokens_then_the_sources():
    events = list(RagService(FakeStore([(make_chunk(), 0.2)]), FakeLLM("18 days a year.")).stream("Leave?"))
    assert "".join(value for kind, value in events if kind == "token").strip() == "18 days a year."
    assert len([kind for kind, _ in events if kind == "token"]) > 1
    kind, final = events[-1]
    assert kind == "done"
    assert final.answer == "18 days a year."
    assert final.sources[0]["policy"] == "Leave Policy"


def test_streamed_refusal_has_no_sources():
    events = list(RagService(FakeStore([(make_chunk(), 0.2)]), FakeLLM(FALLBACK_ANSWER)).stream("Gym?"))
    assert events[-1][1].sources == []


def test_streamed_greeting_and_early_refusal_use_no_llm():
    llm = FakeLLM()
    assert list(RagService(FakeStore([]), llm).stream("Hi"))[0] == ("token", GREETING_ANSWER)
    assert list(RagService(FakeStore([(make_chunk(), 0.99)]), llm).stream("Mars?"))[0] == ("token", FALLBACK_ANSWER)
    assert llm.calls == []


# Keyword search and the reranker

def test_keyword_match_raises_a_chunks_score():
    plain, matched = Candidate(make_chunk("plain"), 0.3), Candidate(make_chunk("matched"), 0.3, keyword_rank=1)
    scores = relevance_scores([plain, matched])
    assert scores[1] == pytest.approx(scores[0] + config.KEYWORD_WEIGHT)


def test_keyword_match_cannot_get_past_the_distance_limit():
    far = Candidate(make_chunk(), config.MAX_DISTANCE + 0.01, keyword_rank=1)
    assert keep_usable([far]) == []


def test_keyword_match_can_win_a_close_call():
    first, second = make_chunk("first"), make_chunk("second")
    store = FakeStore([(first, 0.30), (second, 0.31)], keyword_ranks={1: 1})
    assert RagService(store, FakeLLM()).retrieve("FIR?") == [second, first]


class FakeReranker:
    def __init__(self, order=None, error=None):
        self.order, self.error = order, error

    def rank(self, question, texts):
        if self.error:
            raise self.error
        return self.order


def test_reranker_can_win_a_close_call():
    first, second = make_chunk("first"), make_chunk("second")
    service = RagService(FakeStore([(first, 0.30), (second, 0.305)]), FakeLLM(), FakeReranker([1, 0]))
    assert service.retrieve("leave?") == [second, first]


def test_failing_reranker_does_not_stop_the_answer():
    service = RagService(FakeStore([(make_chunk(), 0.2)]), FakeLLM(), FakeReranker(error=RuntimeError("no model")))
    assert service.ask("Leave?").answer == "18 days."


def test_mmr_prefers_a_different_chunk_over_a_near_copy():
    embeddings = np.array([[1.0, 0.0], [1.0, 0.001], [0.0, 1.0]])
    assert mmr(np.array([0.9, 0.89, 0.8]), embeddings, k=2, lambda_mult=0.5) == [0, 2]


def test_select_chunks_returns_at_most_retrieve_k_with_the_best_first():
    candidates = [Candidate(make_chunk(str(i)), 0.1 + i / 100, list(np.eye(8)[i])) for i in range(8)]
    chunks = select_chunks(candidates)
    assert len(chunks) == config.RETRIEVE_K
    assert chunks[0].metadata["section"] == "0"
