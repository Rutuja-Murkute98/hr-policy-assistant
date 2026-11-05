"""Stage C: find the right chunks, then generate an answer that cites them."""
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field

from langchain_core.documents import Document

from app import config
from app.rag.prompt import (
    FALLBACK_ANSWER,
    GREETING_ANSWER,
    StructuredAnswer,
    build_messages,
    build_rewrite_messages,
)
from app.rag.providers import get_chat_model
from app.rag.rerank import get_reranker
from app.rag.retrieval import Candidate, SearchFilters, keep_usable, select_chunks
from app.rag.store import PolicyIndex, get_store

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


def default_filters() -> SearchFilters:
    return SearchFilters(tenant_id=config.TENANT_ID)


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
                   "chunk_id": valid[label].id or valid[label].metadata.get("chunk_id", "")}
                  for label in usable]


def _field(partial: object, name: str, default: object = None) -> object:
    """Read a field from a partial result, which can be a dict or a Pydantic object."""
    return partial.get(name, default) if isinstance(partial, dict) else getattr(partial, name, default)


class RagService:
    def __init__(self, store, llm, reranker=None, hybrid: bool = config.HYBRID_SEARCH):
        self.store = store
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

    def retrieve(self, question: str, filters: SearchFilters | None = None) -> list[Document]:
        """Return the chunks worth sending to the LLM."""
        vector = self.store.embeddings.embed_query(question)
        candidates = keep_usable(self.store.candidates(vector, question, config.FETCH_K, use_keywords=self.hybrid,
                                                       filters=filters or default_filters()))
        return select_chunks(candidates, self._rerank(question, candidates))

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
            yield "token", GREETING_ANSWER
            yield "done", RagAnswer(GREETING_ANSWER, question_used=question)
            return

        # Only this rewritten question reaches the answer prompt, not the chat itself,
        # so an earlier answer can never be treated as a source of policy facts
        standalone = self._standalone_question(question, history)
        chunks = self.retrieve(standalone, filters)
        if not chunks:  # not enough evidence: skip the LLM call
            yield "token", FALLBACK_ANSWER
            yield "done", RagAnswer(FALLBACK_ANSWER, question_used=standalone, answered=False,
                                    abstain_reason="no_evidence")
            return

        result: StructuredAnswer | None = None
        for kind, value in self._structured_stream(build_messages(standalone, chunks), stream_tokens):
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

        yield "done", RagAnswer(FALLBACK_ANSWER, chunks=chunks, question_used=standalone, answered=False,
                                abstain_reason=reason)


def create_service() -> RagService:
    return RagService(PolicyIndex(get_store()), get_chat_model(), get_reranker())
