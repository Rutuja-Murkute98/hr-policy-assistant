"""Stage C: find the right chunks, then generate an answer from them."""
import re
from dataclasses import dataclass, field

from langchain_core.documents import Document
from langchain_google_genai import ChatGoogleGenerativeAI

from app import config
from app.rag.prompt import FALLBACK_ANSWER, GREETING_ANSWER, build_messages
from app.rag.store import get_store

GREETING = re.compile(r"(hi+|hel+o+|hey+|namaste|good (morning|afternoon|evening))[\s!.,]*",
                      re.IGNORECASE)


@dataclass
class RagAnswer:
    answer: str
    sources: list[dict] = field(default_factory=list)
    chunks: list[Document] = field(default_factory=list)


def _body(chunk: Document) -> str:
    """The chunk's text without the heading path on its first line."""
    return chunk.page_content.partition("\n\n")[2]


class RagService:
    def __init__(self, store, llm):
        self.store = store
        self.llm = llm

    def _retrieve_pairs(self, question: str) -> list[tuple[Document, float]]:
        """Return (chunk, distance) pairs worth sending to the LLM."""
        vector = self.store.embeddings.embed_query(question)
        # MMR: choose chunks that are relevant AND different from each other
        pairs = self.store.max_marginal_relevance_search_with_score_by_vector(
            vector, k=config.RETRIEVE_K, fetch_k=config.FETCH_K, lambda_mult=config.MMR_LAMBDA,
        )
        kept, seen = [], set()
        for chunk, distance in pairs:
            if distance > config.MAX_DISTANCE:
                continue  # too far in meaning: bad context creates bad answers
            if len(_body(chunk)) < config.MIN_BODY_CHARS:
                continue  # a one-line lead-in has nothing to answer from
            if chunk.page_content in seen:
                continue  # never send the same text twice
            seen.add(chunk.page_content)
            kept.append((chunk, distance))
        return kept

    def retrieve(self, question: str) -> list[Document]:
        return [chunk for chunk, _ in self._retrieve_pairs(question)]

    def ask(self, question: str) -> RagAnswer:
        if GREETING.fullmatch(question.strip()):
            return RagAnswer(GREETING_ANSWER)  # no search or API quota needed

        chunks = self.retrieve(question)
        if not chunks:
            return RagAnswer(FALLBACK_ANSWER)  # nothing relevant: skip the LLM call

        answer = self.llm.invoke(build_messages(question, chunks)).text.strip()
        if answer == FALLBACK_ANSWER:
            return RagAnswer(answer, chunks=chunks)  # a refusal has no sources to cite

        sources, seen = [], set()
        for chunk in chunks:
            key = (chunk.metadata["policy"], chunk.metadata["section"])
            if key not in seen:
                seen.add(key)
                sources.append({"policy": key[0], "section": key[1],
                                "file": chunk.metadata["source"]})
        return RagAnswer(answer, sources, chunks)


def create_service() -> RagService:
    return RagService(get_store(), ChatGoogleGenerativeAI(model=config.CHAT_MODEL))
