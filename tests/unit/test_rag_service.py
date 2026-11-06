"""Retrieval and answer logic, using fakes so no API is called."""
from types import SimpleNamespace

import numpy as np
import pytest
from langchain_core.documents import Document

from app import config
from app.rag.prompt import FALLBACK_ANSWER, GREETING_ANSWER, StructuredAnswer, build_messages
from app.rag.rerank import CohereReranker, get_reranker
from app.rag.retrieval import (
    Candidate,
    SearchFilters,
    evidence_scores,
    keep_usable,
    mmr,
    rrf_scores,
    select_chunks,
    weighted_scores,
)
from app.rag.service import RagService, check_citations

LONG_TEXT = "Confirmed employees get 18 days of Earned Leave every calendar year. " * 3
CITED = StructuredAnswer(answered=True, answer="18 days [1].", cited_chunk_ids=["1"])
REFUSED = StructuredAnswer(answered=False)


def make_chunk(section="4.1. Earned Leave", body=LONG_TEXT):
    return Document(
        id=f"Leave_Policy > {section} > 0",
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
        self.filters = None

    def candidates(self, vector, question, k, use_keywords=True, filters=None):
        self.searched_for.append(question)
        self.filters = filters
        size = max(len(self.pairs), 1)
        return [Candidate(chunk, distance, list(np.eye(size)[index]), index + 1, self.keyword_ranks.get(index),
                          5.0 if distance <= config.MAX_DISTANCE else 0.5)
                for index, (chunk, distance) in enumerate(self.pairs)]


class FakeStructuredModel:
    def __init__(self, llm):
        self.llm = llm

    def invoke(self, messages):
        self.llm.calls.append(messages)
        return self.llm.result

    def stream(self, messages):
        self.llm.calls.append(messages)
        result = self.llm.result
        words = result.answer.split(" ")
        for count in range(1, len(words)):
            yield {"answered": result.answered, "answer": " ".join(words[:count]) + " "}
        yield result


class FakeLLM:
    def __init__(self, result=CITED, rewrite="How much earned leave do I get on probation?", fail_rewrite=False):
        self.result = result
        self.rewrite = rewrite
        self.fail_rewrite = fail_rewrite
        self.calls = []

    def with_structured_output(self, schema):
        assert schema is StructuredAnswer
        return FakeStructuredModel(self)

    def invoke(self, messages):  # only the rewrite step uses plain text
        self.calls.append(messages)
        if self.fail_rewrite:
            raise RuntimeError("quota")
        return SimpleNamespace(text=self.rewrite)


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
    assert result.answered is False
    assert result.abstain_reason == "no_evidence"
    assert llm.calls == []


def test_answer_cites_only_the_chunks_it_used():
    first, second = make_chunk("4.1. Earned Leave"), make_chunk("4.2. Sick Leave")
    result = RagService(FakeStore([(first, 0.2), (second, 0.3)]), FakeLLM()).ask("Leave?")
    assert result.answer == "18 days [1]."
    assert result.sources == [{"ref": 1, "policy": "Leave Policy", "section": "4.1. Earned Leave",
                               "file": "Leave_Policy.md", "chunk_id": first.id, "superseded": False,
                               "text": first.page_content.partition("\n\n")[2].strip()}]
    assert result.sources[0]["text"].startswith("Confirmed employees")
    assert result.chunks == [first, second]


def test_model_saying_it_cannot_answer_abstains_with_no_sources():
    result = RagService(FakeStore([(make_chunk(), 0.2)]), FakeLLM(REFUSED)).ask("Gym?")
    assert result.answer == FALLBACK_ANSWER
    assert result.sources == []
    assert result.answered is False
    assert result.abstain_reason == "model_abstained"


@pytest.mark.parametrize("bad", [
    StructuredAnswer(answered=True, answer="18 days.", cited_chunk_ids=[]),
    StructuredAnswer(answered=True, answer="18 days [9].", cited_chunk_ids=["9"]),
    StructuredAnswer(answered=True, answer="18 days.", cited_chunk_ids=["Leave_Policy > made up > 0"]),
])
def test_answer_without_a_valid_citation_abstains(bad):
    result = RagService(FakeStore([(make_chunk(), 0.2)]), FakeLLM(bad)).ask("Leave?")
    assert result.answer == FALLBACK_ANSWER
    assert result.sources == []
    assert result.abstain_reason == "invalid_citations"


def test_invalid_citation_is_removed_and_a_valid_one_kept():
    chunks = [make_chunk("a"), make_chunk("b")]
    text, sources = check_citations(
        StructuredAnswer(answered=True, answer="One [1] and two [7].", cited_chunk_ids=["1", "7"]), chunks)
    assert text == "One [1] and two."
    assert [source["ref"] for source in sources] == [1]


def test_several_numbers_in_one_citation_are_understood():
    chunks = [make_chunk("a"), make_chunk("b")]
    text, sources = check_citations(StructuredAnswer(answered=True, answer="Both [1, 2] and bad [2, 8] and [9]."), chunks)
    assert text == "Both [1, 2] and bad [2] and."
    assert [source["ref"] for source in sources] == [1, 2]


def test_citation_in_the_text_counts_even_if_the_list_misses_it():
    chunks = [make_chunk("a"), make_chunk("b")]
    _, sources = check_citations(StructuredAnswer(answered=True, answer="Two [2]."), chunks)
    assert [source["section"] for source in sources] == ["b"]


@pytest.mark.parametrize("greeting", ["Hi", "Hii", "hello!", "Hey", "Good morning.", " namaste "])
def test_greetings_get_a_welcome_without_any_api_call(greeting):
    llm = FakeLLM()
    result = RagService(FakeStore([]), llm).ask(greeting)
    assert result.answer == GREETING_ANSWER
    assert llm.calls == []


@pytest.mark.parametrize("question", ["Hi, how much leave do I get?", "Hello, when is salary credited?"])
def test_question_after_a_greeting_is_still_answered(question):
    result = RagService(FakeStore([(make_chunk(), 0.2)]), FakeLLM()).ask(question)
    assert result.answer == "18 days [1]."


def test_prompt_numbers_the_excerpts_and_has_the_question():
    system, human = build_messages("How much leave?", [make_chunk("a"), make_chunk("b")])
    assert "cited_chunk_ids" in system.content
    assert "How much leave?" in human.content
    assert "[1] Leave Policy > a" in human.content
    assert "[2] Leave Policy > b" in human.content


def test_filters_reach_the_search():
    store = FakeStore([(make_chunk(), 0.2)])
    filters = SearchFilters(tenant_id="default", category="leave")
    RagService(store, FakeLLM()).ask("Leave?", filters=filters)
    assert store.filters is filters


def test_search_always_has_a_tenant():
    store = FakeStore([(make_chunk(), 0.2)])
    RagService(store, FakeLLM()).ask("Leave?")
    assert store.filters.tenant_id == config.TENANT_ID


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
    assert result.answer == "18 days [1]."


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

LONGER = StructuredAnswer(answered=True, answer="You get 18 days a year [1].", cited_chunk_ids=["1"])


def test_stream_sends_tokens_then_the_cited_sources():
    events = list(RagService(FakeStore([(make_chunk(), 0.2)]), FakeLLM(LONGER)).stream("Leave?"))
    tokens = [value for kind, value in events if kind == "token"]
    assert len(tokens) > 1
    assert "".join(tokens).startswith("You get 18 days")
    kind, final = events[-1]
    assert kind == "done"
    assert final.answer == "You get 18 days a year [1]."
    assert final.sources[0]["policy"] == "Leave Policy"


def test_streamed_abstention_shows_no_model_text():
    events = list(RagService(FakeStore([(make_chunk(), 0.2)]), FakeLLM(REFUSED)).stream("Gym?"))
    assert [kind for kind, _ in events] == ["done"]
    assert events[-1][1].answer == FALLBACK_ANSWER
    assert events[-1][1].sources == []


def test_streamed_answer_with_bad_citations_is_replaced_at_the_end():
    bad = StructuredAnswer(answered=True, answer="You get a made up amount [9].", cited_chunk_ids=["9"])
    final = list(RagService(FakeStore([(make_chunk(), 0.2)]), FakeLLM(bad)).stream("Leave?"))[-1][1]
    assert final.answer == FALLBACK_ANSWER
    assert final.abstain_reason == "invalid_citations"


def test_streamed_greeting_and_early_refusal_use_no_llm():
    llm = FakeLLM()
    assert list(RagService(FakeStore([]), llm).stream("Hi"))[0] == ("token", GREETING_ANSWER)
    assert list(RagService(FakeStore([(make_chunk(), 0.99)]), llm).stream("Mars?"))[0] == ("token", FALLBACK_ANSWER)
    assert llm.calls == []


# Fusion, evidence and reranking

def test_rrf_adds_up_the_rankings():
    both = Candidate(make_chunk("both"), 0.3, vector_rank=1, keyword_rank=1)
    vector_only = Candidate(make_chunk("vector"), 0.3, vector_rank=2)
    keyword_only = Candidate(make_chunk("keyword"), 0.9, keyword_rank=2)
    scores = rrf_scores([both, vector_only, keyword_only])
    assert scores[0] == pytest.approx(1.0)
    assert scores[1] == pytest.approx(scores[2])
    assert scores[1] == pytest.approx((1 / (config.RRF_K + 2)) / (2 / (config.RRF_K + 1)))


def test_rrf_counts_the_reranker_as_another_ranking():
    first, second = Candidate(make_chunk("a"), 0.3, vector_rank=1), Candidate(make_chunk("b"), 0.3, vector_rank=2)
    assert rrf_scores([first, second], [1, 0])[1] > rrf_scores([first, second])[1]


def test_weighted_score_adds_a_keyword_bonus():
    plain, matched = Candidate(make_chunk("plain"), 0.3), Candidate(make_chunk("matched"), 0.3, keyword_rank=1)
    scores = weighted_scores([plain, matched])
    assert scores[1] == pytest.approx(scores[0] + config.KEYWORD_WEIGHT)


def test_fusion_method_is_a_setting(monkeypatch):
    first, second = make_chunk("first"), make_chunk("second")
    candidates = [Candidate(first, 0.30, list(np.eye(2)[0]), 1), Candidate(second, 0.31, list(np.eye(2)[1]), 2, 1)]
    for method in ("rrf", "weighted"):
        monkeypatch.setattr(config, "FUSION_METHOD", method)
        assert select_chunks(candidates)[0] == second


def test_keyword_match_cannot_get_past_the_evidence_check():
    far = Candidate(make_chunk(), config.MAX_DISTANCE + 0.01, keyword_rank=1)
    assert keep_usable([far]) == []


def test_confidence_mode_uses_the_score_and_ignores_the_distance_limit(monkeypatch):
    monkeypatch.setattr(config, "ABSTENTION_MODE", "confidence")
    monkeypatch.setattr(config, "MIN_CONFIDENCE", 3.0)
    monkeypatch.setattr(config, "MIN_CHUNK_CONFIDENCE", 2.0)
    strong = Candidate(make_chunk("strong"), 0.9, confidence=3.5)
    usable = Candidate(make_chunk("usable"), 0.9, confidence=2.5)
    weak = Candidate(make_chunk("weak"), 0.1, confidence=1.0)
    assert {c.chunk.metadata["section"] for c in keep_usable([strong, usable, weak])} == {"strong", "usable"}


def test_confidence_mode_abstains_if_no_chunk_stands_out_enough(monkeypatch):
    monkeypatch.setattr(config, "ABSTENTION_MODE", "confidence")
    monkeypatch.setattr(config, "MIN_CONFIDENCE", 3.0)
    assert keep_usable([Candidate(make_chunk(), 0.1, confidence=2.9)]) == []


def test_distance_mode_is_still_available(monkeypatch):
    monkeypatch.setattr(config, "ABSTENTION_MODE", "distance")
    near = Candidate(make_chunk("near"), config.MAX_DISTANCE, confidence=0.0)
    far = Candidate(make_chunk("far"), config.MAX_DISTANCE + 0.01, confidence=9.0)
    assert [c.chunk.metadata["section"] for c in keep_usable([near, far])] == ["near"]


def test_evidence_score_measures_how_far_a_chunk_stands_out():
    scores = evidence_scores(np.array([0.9, 0.5]), mean=0.5, spread=0.1)
    assert scores == pytest.approx([4.0, 0.0])
    assert evidence_scores(np.array([0.5]), mean=0.5, spread=0.0) == pytest.approx([0.0])


def test_mmr_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(config, "USE_MMR", False)
    monkeypatch.setattr(config, "RETRIEVE_K", 2)
    same = [1.0, 0.0]
    candidates = [Candidate(make_chunk(str(i)), 0.1 + i / 100, same, i + 1) for i in range(4)]
    assert [c.metadata["section"] for c in select_chunks(candidates)] == ["0", "1"]


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
    assert service.ask("Leave?").answer == "18 days [1]."


class FakeHttp:
    def __init__(self):
        self.sent = {}

    def post(self, url, headers, json):
        self.sent = {"url": url, "headers": headers, "json": json}
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"results": [{"index": 1, "relevance_score": 0.9},
                                                         {"index": 0, "relevance_score": 0.1}]})


def test_cohere_reranker_sends_the_right_request_and_reads_the_order():
    http = FakeHttp()
    assert CohereReranker("key", "some-model", http).rank("q", ["a", "b"]) == [1, 0]
    assert http.sent["headers"] == {"Authorization": "Bearer key"}
    assert http.sent["json"] == {"model": "some-model", "query": "q", "documents": ["a", "b"], "top_n": 2}


def test_reranker_setting_chooses_the_reranker(monkeypatch):
    monkeypatch.setattr(config, "RERANKER", "")
    assert get_reranker() is None
    monkeypatch.setattr(config, "RERANKER", "cohere")
    monkeypatch.setattr(config, "COHERE_API_KEY", "key")
    assert isinstance(get_reranker(), CohereReranker)


def test_mmr_prefers_a_different_chunk_over_a_near_copy():
    embeddings = np.array([[1.0, 0.0], [1.0, 0.001], [0.0, 1.0]])
    assert mmr(np.array([0.9, 0.89, 0.8]), embeddings, k=2, lambda_mult=0.5) == [0, 2]


def test_select_chunks_returns_at_most_retrieve_k_with_the_best_first():
    candidates = [Candidate(make_chunk(str(i)), 0.1 + i / 100, list(np.eye(8)[i]), i + 1) for i in range(8)]
    chunks = select_chunks(candidates)
    assert len(chunks) == config.RETRIEVE_K
    assert chunks[0].metadata["section"] == "0"
