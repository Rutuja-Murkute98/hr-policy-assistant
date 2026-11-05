"""Stage C: find the right chunks, then generate an answer from them."""
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field

from langchain_core.documents import Document

from app import config
from app.rag.prompt import (FALLBACK_ANSWER, GREETING_ANSWER, build_messages,
                            build_rewrite_messages)
from app.rag.providers import get_chat_model
from app.rag.rerank import get_reranker
from app.rag.retrieval import Candidate, keep_usable, select_chunks
from app.rag.store import PolicyIndex, get_store

logger = logging.getLogger("hr_helpdesk")

GREETING = re.compile(r"(hi+|hel+o+|hey+|namaste|good (morning|afternoon|evening))[\s!.,]*",
                      re.IGNORECASE)


@dataclass
class RagAnswer:
    answer: str
    sources: list[dict] = field(default_factory=list)
    chunks: list[Document] = field(default_factory=list)
    question_used: str = ""  # the question that was searched for, after any rewriting


def sources_of(chunks: list[Document]) -> list[dict]:
    """List each policy section once."""
    sources, seen = [], set()
    for chunk in chunks:
        key = (chunk.metadata["policy"], chunk.metadata["section"])
        if key not in seen:
            seen.add(key)
            sources.append({"policy": key[0], "section": key[1], "file": chunk.metadata["source"]})
    return sources


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

    def retrieve(self, question: str) -> list[Document]:
        """Return the chunks worth sending to the LLM."""
        vector = self.store.embeddings.embed_query(question)
        candidates = keep_usable(self.store.candidates(vector, question, config.FETCH_K,
                                                       use_keywords=self.hybrid))
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

    def ask(self, question: str, history: list[dict] | None = None) -> RagAnswer:
        answer = None
        for kind, value in self.stream(question, history, stream_tokens=False):
            if kind == "done":
                answer = value
        return answer

    def stream(self, question: str, history: list[dict] | None = None,
               stream_tokens: bool = True) -> Iterator[tuple[str, object]]:
        """Yield ("token", text) for each piece of the answer, then ("done", RagAnswer)."""
        if GREETING.fullmatch(question.strip()):  # no search or API quota needed
            yield "token", GREETING_ANSWER
            yield "done", RagAnswer(GREETING_ANSWER, question_used=question)
            return

        # Only this rewritten question reaches the answer prompt, not the chat itself,
        # so an earlier answer can never be treated as a source of policy facts
        standalone = self._standalone_question(question, history)
        chunks = self.retrieve(standalone)
        if not chunks:  # nothing relevant: skip the LLM call
            yield "token", FALLBACK_ANSWER
            yield "done", RagAnswer(FALLBACK_ANSWER, question_used=standalone)
            return

        messages = build_messages(standalone, chunks)
        if stream_tokens:
            pieces = []
            for piece in self.llm.stream(messages):
                if piece.text:
                    pieces.append(piece.text)
                    yield "token", piece.text
            answer = "".join(pieces).strip()
        else:
            answer = self.llm.invoke(messages).text.strip()
            yield "token", answer

        # A refusal has no sources to cite. They're sent last, so a streamed refusal has none either.
        sources = [] if answer == FALLBACK_ANSWER else sources_of(chunks)
        yield "done", RagAnswer(answer, sources, chunks, standalone)


def create_service() -> RagService:
    return RagService(PolicyIndex(get_store()), get_chat_model(), get_reranker())
