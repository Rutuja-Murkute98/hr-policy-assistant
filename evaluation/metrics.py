"""Scoring functions for the evaluation. All are deterministic, and none call a model."""
import math


def recall_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    """Share of the relevant chunks found in the top k."""
    return len(set(retrieved[:k]) & relevant) / len(relevant) if relevant else 0.0


def any_hit_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    """1 if at least one relevant chunk is in the top k. Most questions need only one of several places."""
    return 1.0 if set(retrieved[:k]) & relevant else 0.0


def precision_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    """Share of the top k that are relevant. Fewer than k results count against it."""
    return len(set(retrieved[:k]) & relevant) / k if k else 0.0


def reciprocal_rank(retrieved: list[str], relevant: set[str]) -> float:
    for position, chunk_id in enumerate(retrieved, start=1):
        if chunk_id in relevant:
            return 1 / position
    return 0.0


def ndcg_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    gains = sum(1 / math.log2(position + 1) for position, chunk_id in enumerate(retrieved[:k], start=1)
                if chunk_id in relevant)
    ideal = sum(1 / math.log2(position + 1) for position in range(1, min(len(relevant), k) + 1))
    return gains / ideal if ideal else 0.0


def percentile(values: list[float], share: float) -> float:
    """Return the value at this share of the sorted list, such as 0.95 for p95."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(share * len(ordered)) - 1)]


def facts_present(answer: str, facts: list[str]) -> bool:
    return all(fact.lower() in answer.lower() for fact in facts)


def citations_correct(cited: list[str], retrieved: list[str], relevant: set[str]) -> dict[str, bool]:
    """Check citations two ways: all were in the retrieved set, and at least one is a labelled chunk."""
    return {"valid": bool(cited) and set(cited) <= set(retrieved), "on_target": bool(set(cited) & relevant)}


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0
