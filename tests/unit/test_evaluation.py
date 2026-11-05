"""The evaluation's scoring, the golden set's labels and the quality gate. No model is called."""
import pytest

from evaluation.golden import CATEGORIES, REVIEW_STATES, load_golden, review_summary, split_of
from evaluation.metrics import (
    any_hit_at_k,
    citations_correct,
    facts_present,
    ndcg_at_k,
    percentile,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)
from evaluation.quality_gate import FLOORS, check
from pipeline.chunk_documents import load_chunks

RETRIEVED = ["a", "b", "c", "d", "e", "f"]


def test_recall_counts_relevant_chunks_in_the_top_k():
    assert recall_at_k(RETRIEVED, {"a", "z"}, 5) == 0.5
    assert recall_at_k(RETRIEVED, {"f"}, 5) == 0
    assert recall_at_k(RETRIEVED, set(), 5) == 0


def test_any_hit_needs_just_one():
    assert any_hit_at_k(RETRIEVED, {"a", "z"}, 5) == 1
    assert any_hit_at_k(RETRIEVED, {"f"}, 5) == 0


def test_precision_counts_missing_results_against_it():
    assert precision_at_k(["a", "b"], {"a"}, 5) == 0.2


def test_reciprocal_rank_uses_the_first_hit():
    assert reciprocal_rank(RETRIEVED, {"b", "c"}) == 0.5
    assert reciprocal_rank(RETRIEVED, {"z"}) == 0


def test_ndcg_is_1_for_a_perfect_order_and_lower_otherwise():
    assert ndcg_at_k(["a", "b"], {"a", "b"}, 5) == pytest.approx(1)
    assert 0 < ndcg_at_k(["x", "a"], {"a"}, 5) < 1
    assert ndcg_at_k(["x"], {"a"}, 5) == 0


def test_percentile():
    assert percentile([1, 2, 3, 4], 0.5) == 2
    assert percentile(list(range(1, 101)), 0.95) == 95
    assert percentile([], 0.95) == 0


def test_facts_ignore_case_and_need_every_fact():
    assert facts_present("You get 18 Days", ["18 days"])
    assert not facts_present("You get 18 days", ["18 days", "confirmed"])
    assert facts_present("Anything", [])


def test_citations_are_checked_both_ways():
    assert citations_correct(["a"], ["a", "b"], {"a"}) == {"valid": True, "on_target": True}
    assert citations_correct(["b"], ["a", "b"], {"a"}) == {"valid": True, "on_target": False}
    assert citations_correct(["z"], ["a", "b"], {"z"}) == {"valid": False, "on_target": True}
    assert citations_correct([], ["a"], {"a"}) == {"valid": False, "on_target": False}


@pytest.fixture(scope="module")
def golden():
    return load_golden()


@pytest.fixture(scope="module")
def chunks():
    return {chunk.id: chunk for chunk in load_chunks()}


def test_golden_set_is_big_enough_and_covers_every_category(golden):
    assert len(golden) >= 100
    assert {item["category"] for item in golden} == CATEGORIES


def test_golden_ids_are_unique_and_status_is_clear(golden):
    ids = [item["id"] for item in golden]
    assert len(ids) == len(set(ids))
    assert all(item["review_status"] in REVIEW_STATES for item in golden)
    assert sum(review_summary(golden).values()) == len(golden)


def test_golden_labels_point_to_real_chunks(golden, chunks):
    for item in golden:
        missing = set(item["relevant_chunks"]) - set(chunks)
        assert not missing, (item["id"], missing)


def test_answerable_items_have_labels_and_refusals_do_not(golden):
    for item in golden:
        if item["must_refuse"]:
            assert not item["must_contain"], item["id"]
        else:
            assert item["relevant_chunks"], item["id"]


def test_expected_facts_really_are_in_the_labelled_chunks(golden, chunks):
    for item in golden:
        text = " ".join(chunks[chunk_id].page_content.lower() for chunk_id in item["relevant_chunks"])
        for fact in item["must_contain"]:
            assert fact.lower() in text, (item["id"], fact)


def test_follow_ups_have_history(golden):
    for item in golden:
        assert bool(item["history"]) == (item["category"] == "follow_up"), item["id"]


def test_split_is_fixed_and_uses_both_groups(golden):
    assert {split_of(item) for item in golden} == {"tune", "check"}
    assert split_of({"id": "normal-002"}) == "tune"
    assert split_of({"id": "normal-003"}) == "check"


GOOD = {name: floor for name, floor in FLOORS.items()}


def test_gate_passes_on_good_current_results():
    assert check({"fingerprint": "same", "metrics": GOOD}, "same") == []


def test_gate_fails_when_the_code_changed_since_the_evaluation():
    assert "changed since" in check({"fingerprint": "old", "metrics": GOOD}, "new")[0]


def test_gate_fails_on_a_low_or_missing_metric():
    low = {**GOOD, "recall_at_5": 0.1}
    assert "below the floor" in check({"fingerprint": "same", "metrics": low}, "same")[0]
    assert "wasn't recorded" in check({"fingerprint": "same", "metrics": {}}, "same")[0]
