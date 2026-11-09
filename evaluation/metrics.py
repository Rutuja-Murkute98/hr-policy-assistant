"""Scoring functions for the evaluation. All are deterministic, and none call a model."""
import math
import re
from itertools import combinations

# A break after . ! ? or :, but not before a number, as in "Rs. 5,000", nor after a common abbreviation
SENTENCE_BREAK = re.compile(r"(?<!\bRs\.)(?<!\bNo\.)(?<!\be\.g\.)(?<!\bi\.e\.)(?<=[.!?:])\s+(?!\d)|\n+")
CITATION = re.compile(r"\[\d+(?:\s*,\s*\d+)*\]")
ONLY_CITATIONS = re.compile(r"^(?:\s*\[\d+(?:\s*,\s*\d+)*\][.,;]?)+\s*$")
MIN_STATEMENT_CHARS = 25  # shorter pieces are headings or lead-ins, not claims


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


def statements(answer: str) -> list[tuple[str, bool]]:
    """Split an answer into its statements, each with whether it carries a citation such as [2].

    A citation on its own after a full stop, as in "It's 18 days. [1]", belongs to the statement before it.
    """
    pieces = [piece.strip(" -*•\t") for piece in SENTENCE_BREAK.split(answer) if piece.strip(" -*•\t")]
    found: list[tuple[str, bool]] = []
    for piece in pieces:
        if ONLY_CITATIONS.match(piece) and found:
            found[-1] = (found[-1][0], True)
        elif piece.endswith(":"):
            continue  # a lead-in to a list, such as "Here's what applies:"
        elif len(CITATION.sub("", piece).strip()) >= MIN_STATEMENT_CHARS:
            found.append((piece, bool(CITATION.search(piece))))
    return found


def cited_statement_share(answer: str) -> float | None:
    """Share of an answer's statements that point at an excerpt. None if it has no statements to judge.

    A grounded answer cites what it says, so this catches claims added without a source, with no model needed.
    It can't tell whether the cited excerpt really supports the claim: the judge checks that.
    """
    found = statements(answer)
    return sum(cited for _, cited in found) / len(found) if found else None


def cohen_kappa(first: list[bool], second: list[bool]) -> float | None:
    """How much two judges agree beyond chance: 1 is full agreement, 0 is what chance gives.

    None when there's nothing to compare, or both always gave the same single answer, where chance explains it all.
    """
    if not first or len(first) != len(second):
        return None
    observed = mean([float(a == b) for a, b in zip(first, second, strict=True)])
    yes_first, yes_second = mean([float(a) for a in first]), mean([float(b) for b in second])
    expected = yes_first * yes_second + (1 - yes_first) * (1 - yes_second)
    return None if expected == 1 else (observed - expected) / (1 - expected)


def judge_agreement(verdicts_by_item: list[dict[str, dict]]) -> dict[str, dict]:
    """For each pair of judges, how often they agree, on the items both judged."""
    names = sorted({name for verdicts in verdicts_by_item for name in verdicts})
    agreement = {}
    for first, second in combinations(names, 2):
        both = [verdicts for verdicts in verdicts_by_item if first in verdicts and second in verdicts]
        result: dict = {"both_judged": len(both)}
        for field in ("correct", "faithful"):
            a = [bool(verdicts[first][field]) for verdicts in both]
            b = [bool(verdicts[second][field]) for verdicts in both]
            result[f"{field}_agree"] = round(mean([float(x == y) for x, y in zip(a, b, strict=True)]), 3)
            kappa = cohen_kappa(a, b)
            result[f"{field}_kappa"] = round(kappa, 3) if kappa is not None else None
        agreement[f"{first} vs {second}"] = result
    return agreement
