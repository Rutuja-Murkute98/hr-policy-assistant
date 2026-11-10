"""Stage C: find the right chunks, then generate an answer that cites them."""
import logging
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

from langchain_core.documents import Document

from app import config
from app.rag.prompt import (
    StructuredAnswer,
    build_messages,
    build_rewrite_messages,
    fallback_answer,
    greeting_answer,
)
from app.rag.retrieval import Candidate, SearchFilters, keep_usable, select_chunks

logger = logging.getLogger("hr_helpdesk")

GREETING = re.compile(r"(hi+|hel+o+|hey+|namaste|good (morning|afternoon|evening))[\s!.,]*",
                      re.IGNORECASE)
CITATION = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")  # such as [1] or [2, 4]


@dataclass
class RagAnswer:
    answer: str
    sources: list[dict] = field(default_factory=list)
    chunks: list[Document] = field(default_factory=list)  # everything sent to the LLM
    question_used: str = ""  # the question that was searched for, after any rewriting
    answered: bool = True    # false when the bot abstained
    abstain_reason: str = ""


def check_citations(result: StructuredAnswer, chunks: list[Document]) -> tuple[str, list[dict]] | None:
    """Return (answer text, sources) using only valid citations, or None if none are valid.

    A citation is valid if it refers to a chunk that was actually sent to the LLM. Numbers
    in the text and in cited_chunk_ids both count, and invalid ones are removed from the text.
    """
    valid = {str(number): chunk for number, chunk in enumerate(chunks, start=1)}
    cited_in_text = {label.strip() for group in CITATION.findall(result.answer) for label in group.split(",")}
    cited = {str(label).strip("[] ") for label in result.cited_chunk_ids} | cited_in_text
    usable = sorted((label for label in cited if label in valid), key=int)
    if not usable:
        return None

    def keep_valid(match: re.Match) -> str:
        labels = [label.strip() for label in match[1].split(",") if label.strip() in valid]
        return f"[{', '.join(labels)}]" if labels else ""

    text = re.sub(r" +([.,;:])", r"\1", CITATION.sub(keep_valid, result.answer)).strip()
    return text, [{"ref": int(label), "policy": valid[label].metadata["policy"],
                   "section": valid[label].metadata["section"], "file": valid[label].metadata["source"],
                   "chunk_id": valid[label].id or valid[label].metadata.get("chunk_id", ""),
                   # The policy wording itself, without the heading path on the chunk's first line
                   "text": valid[label].page_content.partition("\n\n")[2].strip(),
                   "superseded": valid[label].metadata.get("is_current") == "false"}
                  for label in usable]


def _field(partial: object, name: str, default: object = None) -> object:
    """Read a field from a partial result, which can be a dict or a Pydantic object."""
    return partial.get(name, default) if isinstance(partial, dict) else getattr(partial, name, default)


class RagService:
    def __init__(self, store, llm, reranker=None, hybrid: bool = config.HYBRID_SEARCH, profile=None,
                 query_cache=None):
        self.store = store
        self.query_cache = query_cache  # reuses the vectors of questions asked before, if set
        self.profile = profile or config.tenant
        self.tenant_id = getattr(store, "tenant_id", config.TENANT_ID)
        self.llm = llm
        self.reranker = reranker
        self.hybrid = hybrid

    def _rerank(self, question: str, candidates: list[Candidate]) -> list[int] | None:
        if not self.reranker or not candidates:
            return None
        try:
            return self.reranker.rank(question, [c.chunk.page_content for c in candidates])
        except Exception:
            # Answers still work without the reranker, so this shouldn't stop them
            logger.exception("The reranker failed, so it was skipped")
            return None

    def search(self, question: str, filters: SearchFilters | None = None,
               timing: dict | None = None) -> tuple[list[Document], float]:
        """Return the chunks worth sending to the LLM, and how strongly the best chunk stood out.

        If timing is given, it's filled with the milliseconds spent on embedding and on the search itself.
        """
        started = time.perf_counter()
        if self.query_cache is not None:
            vector = self.query_cache.embed_query(self.store.embeddings, question)
        else:
            vector = self.store.embeddings.embed_query(question)
        embedded = time.perf_counter()
        found = self.store.candidates(vector, question, config.FETCH_K, use_keywords=self.hybrid,
                                      filters=filters or SearchFilters(tenant_id=self.tenant_id))
        candidates = keep_usable(found)
        confidence = max((candidate.confidence for candidate in found), default=0.0)
        selected = select_chunks(candidates, self._rerank(question, candidates))
        if timing is not None:
            timing["embedding_ms"] = (embedded - started) * 1000
            timing["retrieval_ms"] = (time.perf_counter() - embedded) * 1000
        return selected, confidence

    def retrieve(self, question: str, filters: SearchFilters | None = None) -> list[Document]:
        return self.search(question, filters)[0]

    def _standalone_question(self, question: str, history: list[dict] | None) -> str:
        """Rewrite a follow-up so it can be searched for without the chat around it."""
        if not history:
            return question
        recent = history[-config.MAX_HISTORY_MESSAGES:]
        try:
            rewritten = self.llm.invoke(build_rewrite_messages(question, recent)).text.strip()
        except Exception:
            # The original question may still work, so this shouldn't stop the answer
            logger.exception("Rewriting a follow-up question failed, so the original was used")
            return question
        if not rewritten or len(rewritten) > config.MAX_QUESTION_CHARS:
            return question
        return rewritten

    def ask(self, question: str, history: list[dict] | None = None,
            filters: SearchFilters | None = None) -> RagAnswer:
        for kind, value in self.stream(question, history, filters, stream_tokens=False):
            if kind == "done":
                assert isinstance(value, RagAnswer)
                return value
        raise RuntimeError("The answer stream ended without a final answer")

    def _structured_stream(self, messages: list, stream_tokens: bool) -> Iterator[tuple[str, object]]:
        """Yield ("token", text) as the answer field grows, then ("result", StructuredAnswer)."""
        model = self.llm.with_structured_output(StructuredAnswer)
        if not stream_tokens:
            yield "result", model.invoke(messages)
            return

        sent, latest = "", None
        for partial in model.stream(messages):
            latest = partial
            answer = str(_field(partial, "answer", "") or "")
            # Don't show text until the model has said it's answering, and only ever add to what's shown
            if _field(partial, "answered") is True and answer.startswith(sent) and len(answer) > len(sent):
                yield "token", answer[len(sent):]
                sent = answer
        yield "result", (latest if isinstance(latest, StructuredAnswer) else StructuredAnswer(**(latest or {})))

    def stream(self, question: str, history: list[dict] | None = None, filters: SearchFilters | None = None,
               stream_tokens: bool = True) -> Iterator[tuple[str, object]]:
        """Yield ("token", text) for each piece of the answer, then ("done", RagAnswer).

        The final answer is the one to keep, because citation checks can change the streamed text.
        """
        if GREETING.fullmatch(question.strip()):  # no search or API quota needed
            yield "token", greeting_answer(self.profile)
            yield "done", RagAnswer(greeting_answer(self.profile), question_used=question)
            return

        # Only this rewritten question reaches the answer prompt, not the chat itself,
        # so an earlier answer can never be treated as a source of policy facts
        standalone = self._standalone_question(question, history)
        chunks = self.retrieve(standalone, filters)
        if not chunks:  # not enough evidence: skip the LLM call
            yield "token", fallback_answer(self.profile)
            yield "done", RagAnswer(fallback_answer(self.profile), question_used=standalone, answered=False,
                                    abstain_reason="no_evidence")
            return

        yield from self.answer_from_chunks(standalone, chunks, stream_tokens)

    def answer_from_chunks(self, standalone: str, chunks: list[Document],
                           stream_tokens: bool = True) -> Iterator[tuple[str, object]]:
        """Write the answer, check its citations, and yield tokens, then ("done", RagAnswer)."""
        result: StructuredAnswer | None = None
        for kind, value in self._structured_stream(build_messages(standalone, chunks, self.profile), stream_tokens):
            if kind == "token":
                yield "token", value
            else:
                assert isinstance(value, StructuredAnswer)
                result = value
        assert result is not None

        checked = check_citations(result, chunks) if result.answered and result.answer.strip() else None
        if not result.answered or not result.answer.strip():
            reason = "model_abstained"
        elif checked is None:
            reason = "invalid_citations"
            logger.warning("An answer had no valid citations, so the bot abstained")
        else:
            yield "done", RagAnswer(checked[0], checked[1], chunks, standalone)
            return

        yield "done", RagAnswer(fallback_answer(self.profile), chunks=chunks, question_used=standalone, answered=False,
                                abstain_reason=reason)
