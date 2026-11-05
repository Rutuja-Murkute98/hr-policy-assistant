"""Compare retrieval settings by whether search finds the right policy section.

    venv\\Scripts\\python -m evaluation.retrieval_benchmark

No chat model is called. Question embeddings are saved in .cache, so only the first run
uses embedding quota. A hit means the right section is in the chunks sent to the LLM, and
reciprocal rank (1 = first, 0.5 = second...) shows how high up it was.
"""
import json

from app import config
from app.rag.retrieval import keep_usable, select_chunks
from app.rag.store import PolicyIndex, get_store
from evaluation.test_set import EXACT_TERM_QUESTIONS, SECTIONS_FOR_ANSWERABLE, UNANSWERABLE

CACHE_FILE = config.PROJECT_ROOT / ".cache" / "question_embeddings.json"


def embed_all(index: PolicyIndex, questions: list[str]) -> dict[str, list[float]]:
    saved = json.loads(CACHE_FILE.read_text(encoding="utf-8")) if CACHE_FILE.exists() else {}
    missing = [question for question in questions if question not in saved]
    if missing:
        saved.update(zip(missing, index.embeddings.embed_documents(missing), strict=True))
        CACHE_FILE.parent.mkdir(exist_ok=True)
        CACHE_FILE.write_text(json.dumps(saved), encoding="utf-8")
    return saved


def rank_of_answer(chunks, places) -> int | None:
    """Return the position of the first chunk from a right section, starting at 1."""
    for position, chunk in enumerate(chunks, start=1):
        for file, section in places:
            if chunk.metadata["source"] == file and chunk.metadata["section"].startswith(section):
                return position
    return None


def run(index, vectors, questions, hybrid, reranker) -> tuple[list[str], float]:
    misses, reciprocal_ranks = [], []
    for question, places in questions.items():
        candidates = keep_usable(index.candidates(vectors[question], question, config.FETCH_K, use_keywords=hybrid))
        order = reranker.rank(question, [c.chunk.page_content for c in candidates]) if reranker and candidates else None
        rank = rank_of_answer(select_chunks(candidates, order), places)
        reciprocal_ranks.append(1 / rank if rank else 0)
        if not rank:
            misses.append(question)
    return misses, sum(reciprocal_ranks) / len(questions)


def check_places_exist(index: PolicyIndex, questions: dict) -> None:
    """Stop if a section name in the test set doesn't match any stored chunk."""
    stored = [(c.metadata["source"], c.metadata["section"]) for c in index.stored_chunks().values()]
    for question, places in questions.items():
        for file, section in places:
            if not any(f == file and s.startswith(section) for f, s in stored):
                raise SystemExit(f"Test set error for {question!r}: no section {section!r} in {file}")


if __name__ == "__main__":
    from app.rag.rerank import FlashrankReranker

    index = PolicyIndex(get_store())
    check_places_exist(index, {**SECTIONS_FOR_ANSWERABLE, **EXACT_TERM_QUESTIONS})
    vectors = embed_all(index, [*SECTIONS_FOR_ANSWERABLE, *EXACT_TERM_QUESTIONS, *UNANSWERABLE])
    reranker = FlashrankReranker()

    settings = {"vector only": (False, None), "vector + keyword": (True, None),
                "vector + reranker": (False, reranker), "vector + keyword + reranker": (True, reranker)}
    for name, (hybrid, rerank) in settings.items():
        print(f"\n{name}")
        for label, questions in (("policy questions", SECTIONS_FOR_ANSWERABLE), ("exact terms", EXACT_TERM_QUESTIONS)):
            misses, score = run(index, vectors, questions, hybrid, rerank)
            print(f"  {label}: {len(questions) - len(misses)}/{len(questions)} found, reciprocal rank {score:.3f}")
            for question in misses:
                print(f"    missed: {question}")
        reach = sum(bool(keep_usable(index.candidates(vectors[q], q, config.FETCH_K, use_keywords=hybrid)))
                    for q in UNANSWERABLE)
        print(f"  questions it must refuse that still reach the LLM: {reach}/{len(UNANSWERABLE)}")
