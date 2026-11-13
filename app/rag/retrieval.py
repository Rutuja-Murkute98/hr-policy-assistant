"""Choose which chunks to send to the LLM from the search candidates.

Vector search finds chunks with similar meaning, and keyword search finds exact terms such
as "FIR" or "UAN". Their rankings are combined, an optional reranker can add its own opinion,
and chunks without enough evidence are dropped. If nothing is left, the bot abstains.
"""
from dataclasses import dataclass, field

import numpy as np
from langchain_core.documents import Document

from app import config
from app.config import Tenant
from app.org_settings import setting


@dataclass
class SearchFilters:
    """Limits on what a search can find. The tenant isn't optional."""

    tenant_id: str
    policy: str | None = None
    category: str | None = None
    categories: list[str] | None = None  # any of these, for routing by topic
    effective_on: str | None = None  # an ISO date: the policies in effect on that date, including old versions
    include_history: bool = False    # also search superseded versions


@dataclass
class Candidate:
    chunk: Document
    distance: float                  # cosine distance from the question (0 = identical meaning)
    embedding: list[float] = field(default_factory=list)
    vector_rank: int | None = None   # position in the vector results, if it was found there
    keyword_rank: int | None = None  # position in the keyword results, if it was found there
    confidence: float = 0.0          # how much it stands out: see evidence_scores


def evidence_scores(similarities: np.ndarray, mean: float, spread: float) -> np.ndarray:
    """Measure how far each chunk stands out from all chunks for this question, in standard deviations.

    Raw distances depend on the embedding model. This compares each chunk with the whole
    collection, so the scale carries over better between models.
    """
    return (similarities - mean) / (spread or 1.0)


def body_of(chunk: Document) -> str:
    """The chunk's text without the heading path on its first line."""
    return chunk.page_content.partition("\n\n")[2]


def has_evidence(candidate: Candidate, profile: Tenant | None = None) -> bool:
    if config.ABSTENTION_MODE == "confidence":
        return candidate.confidence >= setting(profile, "min_chunk_confidence")
    return candidate.distance <= config.MAX_DISTANCE


def enough_evidence_overall(candidates: list[Candidate], profile: Tenant | None = None) -> bool:
    """Decide whether the question is worth answering at all, from its strongest chunk."""
    if config.ABSTENTION_MODE != "confidence":
        return True  # "distance" mode only judges each chunk
    return any(candidate.confidence >= setting(profile, "min_confidence") for candidate in candidates)


def keep_usable(candidates: list[Candidate], profile: Tenant | None = None) -> list[Candidate]:
    """Drop chunks without enough evidence, and ones that are only a lead-in or repeated text.

    The thresholds are the organisation's, if the profile has its own.
    """
    if not enough_evidence_overall(candidates, profile):
        return []
    kept, seen = [], set()
    for candidate in sorted(candidates, key=lambda c: c.distance):
        if not has_evidence(candidate, profile):
            continue  # also applies to keyword matches, so off-topic questions are still refused
        if len(body_of(candidate.chunk)) < config.MIN_BODY_CHARS:
            continue
        if candidate.chunk.page_content in seen:
            continue
        seen.add(candidate.chunk.page_content)
        kept.append(candidate)
    return kept


def rrf_scores(candidates: list[Candidate], rerank_order: list[int] | None = None) -> np.ndarray:
    """Reciprocal rank fusion: add 1 / (RRF_K + rank) from each ranking the chunk appears in."""
    scores = np.zeros(len(candidates))
    for index, candidate in enumerate(candidates):
        for rank in (candidate.vector_rank, candidate.keyword_rank):
            if rank is not None:
                scores[index] += 1 / (config.RRF_K + rank)
    for rank, index in enumerate(rerank_order or [], start=1):
        scores[index] += 1 / (config.RRF_K + rank)
    return scores / scores.max() if scores.max() > 0 else scores


def weighted_scores(candidates: list[Candidate], rerank_order: list[int] | None = None) -> np.ndarray:
    """Similarity to the question, plus small bonuses for ranking well elsewhere."""
    scores = np.array([1 - candidate.distance for candidate in candidates])
    for index, candidate in enumerate(candidates):
        if candidate.keyword_rank is not None:
            scores[index] += config.KEYWORD_WEIGHT / candidate.keyword_rank
    for rank, index in enumerate(rerank_order or [], start=1):
        scores[index] += config.RERANKER_WEIGHT / rank
    return scores


def relevance_scores(candidates: list[Candidate], rerank_order: list[int] | None = None) -> np.ndarray:
    fuse = rrf_scores if config.FUSION_METHOD == "rrf" else weighted_scores
    return fuse(candidates, rerank_order)


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


def select_chunks(candidates: list[Candidate], rerank_order: list[int] | None = None,
                  profile: Tenant | None = None) -> list[Document]:
    """Return the chunks to send to the LLM, with the most relevant first.

    rerank_order lists positions in candidates, best first. The candidates should already
    have been through keep_usable.
    """
    if not candidates:
        return []
    relevance = relevance_scores(candidates, rerank_order)
    retrieve_k = setting(profile, "retrieve_k")
    if config.USE_MMR:
        embeddings = np.array([candidate.embedding for candidate in candidates], dtype=float)
        picked = mmr(relevance, embeddings, retrieve_k, config.MMR_LAMBDA)
    else:
        picked = list(range(len(candidates)))
    best_first = sorted(picked, key=lambda i: -relevance[i])
    return [candidates[i].chunk for i in best_first[:retrieve_k]]
