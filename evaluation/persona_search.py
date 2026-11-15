"""How often search finds the labelled sections for the persona questions, under different ways of using the tags.

    python -m evaluation.persona_search

Only embeddings are used, never the answer model, so ways of weighting sections can be compared for free. Questions
are searched as asked, as most of them are first messages that the workflow searches unchanged.
"""
import json

from app import config
from app.rag import service as rag_service
from app.rag.retrieval import SearchFilters
from app.workflow import create_workflow
from evaluation.golden import load_golden
from evaluation.metrics import recall_at_k
from evaluation.personas import expand, load_personas
from evaluation.run_eval import EVAL_USER, REPORTS, save_json


def no_preference(audience, persona):
    return 0.0


def fitting_only(audience, persona):
    """Move up what fits the person, and leave the rest where it is."""
    fits = audience.fits(persona) if audience is not None else None
    return config.AUDIENCE_WEIGHT if fits else 0.0


def any_group(audience, persona):
    """Move up every section limited to a group: one that excludes the person tells them so, too."""
    fits = audience.fits(persona) if audience is not None else None
    return 0.0 if fits is None else config.AUDIENCE_WEIGHT


WAYS = {"no preference": no_preference, "up and down (Phase 11 part 1)": rag_service.audience_adjustment,
        "fitting sections up only": fitting_only, "any group-limited section up": any_group}
WEIGHTS = (0.03, 0.06, 0.1)


def main() -> None:
    workflow = create_workflow()
    rag = workflow.rag
    questions = expand(load_personas())
    personas = {question["employee"]: workflow.persona_for(question["employee"]) for question in questions}
    original = rag_service.audience_adjustment
    results: dict = {}
    try:
        for way, adjust in WAYS.items():
            for weight in (WEIGHTS if adjust not in (no_preference,) else (0.0,)):
                config.AUDIENCE_WEIGHT = weight
                rag_service.audience_adjustment = adjust
                recalls = []
                for question in questions:
                    chunks, _ = rag.search(question["question"], SearchFilters(tenant_id=config.TENANT_ID),
                                           persona=personas[question["employee"]])
                    recalls.append(recall_at_k([chunk.id for chunk in chunks], set(question["relevant_chunks"]),
                                               config.RETRIEVE_K))
                results[f"{way}, weight {weight}"] = round(sum(recalls) / len(recalls), 3)
                print(f"{way:34} weight {weight:<5} recall@5 {results[f'{way}, weight {weight}']}", flush=True)
        golden = [item for item in load_golden() if not item["history"] and item["relevant_chunks"]]
        asha = workflow.persona_for(EVAL_USER)
        for way, weight in (("no preference", 0.0), ("any group-limited section up", 0.03),
                            ("any group-limited section up", 0.06)):
            config.AUDIENCE_WEIGHT = weight
            rag_service.audience_adjustment = WAYS[way]
            recalls = []
            for item in golden:
                chunks, _ = rag.search(item["question"], SearchFilters(tenant_id=config.TENANT_ID), persona=asha)
                recalls.append(recall_at_k([chunk.id for chunk in chunks], set(item["relevant_chunks"]),
                                           config.RETRIEVE_K))
            results[f"golden as {EVAL_USER}: {way}, weight {weight}"] = round(sum(recalls) / len(recalls), 3)
            print(f"golden ({len(golden)} first messages) {way:30} weight {weight:<5} recall@5 "
                  f"{results[f'golden as {EVAL_USER}: {way}, weight {weight}']}", flush=True)
    finally:
        rag_service.audience_adjustment = original
        workflow.close()
    save_json(REPORTS / "persona_search.json", results)
    print(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
