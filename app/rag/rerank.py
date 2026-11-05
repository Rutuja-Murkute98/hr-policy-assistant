"""An optional reranker: a small local model that scores each chunk against the question.

It's off unless RERANKER is set, because it adds memory use and a model download. FlashRank
runs on the CPU and has no API costs. Install it with: pip install flashrank
"""
from app import config


class FlashrankReranker:
    def __init__(self, model_name: str = config.RERANKER_MODEL or "ms-marco-TinyBERT-L-2-v2"):
        from flashrank import Ranker

        config.RERANKER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.ranker = Ranker(model_name=model_name, cache_dir=str(config.RERANKER_CACHE_DIR))

    def rank(self, question: str, texts: list[str]) -> list[int]:
        """Return the positions in texts, with the best match first."""
        from flashrank import RerankRequest

        passages = [{"id": index, "text": text} for index, text in enumerate(texts)]
        results = self.ranker.rerank(RerankRequest(query=question, passages=passages))
        return [result["id"] for result in results]


def get_reranker():
    if not config.RERANKER:
        return None
    if config.RERANKER != "flashrank":
        raise ValueError(f"RERANKER={config.RERANKER!r} isn't supported. Choose flashrank or leave it unset.")
    try:
        return FlashrankReranker()
    except ImportError as error:
        raise ValueError("RERANKER=flashrank needs this package: pip install flashrank") from error
