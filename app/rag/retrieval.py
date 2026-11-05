"""Choose which chunks to send to the LLM from the search candidates.

Vector search finds chunks with similar meaning, and keyword search finds exact terms
such as "FIR" or "UAN". A chunk's score is its similarity to the question, plus small
bonuses from keyword search and the optional reranker. MMR then picks chunks that are
relevant and different from each other.
"""
from dataclasses import dataclass, field

import numpy as np
from langchain_core.documents import Document

from app import config


@dataclass
class Candidate:
    chunk: Document
    distance: float                  # cosine distance from the question (0 = identical meaning)
    embedding: list[float] = field(default_factory=list)
    keyword_rank: int | None = None  # position in the keyword results, if it was found there


def body_of(chunk: Document) -> str:
    """The chunk's text without the heading path on its first line."""
    return chunk.page_content.partition("\n\n")[2]


def keep_usable(candidates: list[Candidate]) -> list[Candidate]:
    """Drop chunks that are too far in meaning, only a lead-in, or repeated text."""
    kept, seen = [], set()
    for candidate in sorted(candidates, key=lambda c: c.distance):
        if candidate.distance > config.MAX_DISTANCE:
            continue  # also applies to keyword matches, so off-topic questions are still refused
        if len(body_of(candidate.chunk)) < config.MIN_BODY_CHARS:
            continue
        if candidate.chunk.page_content in seen:
            continue
        seen.add(candidate.chunk.page_content)
        kept.append(candidate)
    return kept


def relevance_scores(candidates: list[Candidate], rerank_order: list[int] | None = None) -> np.ndarray:
    """Score each candidate: similarity to the question, plus a bonus for ranking well elsewhere.

    The bonuses halve, then third, and so on down each ranking, so only the top places matter.
    """
    scores = np.array([1 - candidate.distance for candidate in candidates])
    for index, candidate in enumerate(candidates):
        if candidate.keyword_rank is not None:
            scores[index] += config.KEYWORD_WEIGHT / candidate.keyword_rank
    for rank, index in enumerate(rerank_order or [], start=1):
        scores[index] += config.RERANKER_WEIGHT / rank
    return scores


def mmr(relevance: np.ndarray, embeddings: np.ndarray, k: int, lambda_mult: float) -> list[int]:
    """Pick k items, balancing relevance against similarity to the items already picked."""
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    unit = embeddings / np.where(norms == 0, 1, norms)
    similarity = unit @ unit.T

    picked = [int(np.argmax(relevance))]
    while len(picked) < min(k, len(relevance)):
        redundancy = similarity[:, picked].max(axis=1)
        scores = lambda_mult * relevance - (1 - lambda_mult) * redundancy
        scores[picked] = -np.inf
        picked.append(int(np.argmax(scores)))
    return picked


def select_chunks(candidates: list[Candidate], rerank_order: list[int] | None = None) -> list[Document]:
    """Return the chunks to send to the LLM, with the most relevant first.

    rerank_order lists positions in candidates, best first. The candidates should already
    have been through keep_usable.
    """
    if not candidates:
        return []
    relevance = relevance_scores(candidates, rerank_order)
    embeddings = np.array([candidate.embedding for candidate in candidates], dtype=float)
    picked = mmr(relevance, embeddings, config.RETRIEVE_K, config.MMR_LAMBDA)
    return [candidates[i].chunk for i in sorted(picked, key=lambda i: -relevance[i])]
