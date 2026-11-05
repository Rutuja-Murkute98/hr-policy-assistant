"""An optional reranker scores each chunk against the question. It's off unless RERANKER is set.

FlashRank is a small local model that runs on the CPU, with no API costs, but it adds memory
use and a model download. Install it with: pip install flashrank
Cohere is an API, so it adds no memory use, but it needs COHERE_API_KEY and sends text to Cohere.
"""
import httpx

from app import config

COHERE_URL = "https://api.cohere.com/v2/rerank"
COHERE_DEFAULT_MODEL = "rerank-v3.5"
COHERE_TIMEOUT_SECONDS = 15


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


class CohereReranker:
    def __init__(self, api_key: str, model_name: str = COHERE_DEFAULT_MODEL, client: httpx.Client | None = None):
        self.api_key = api_key
        self.model_name = model_name
        self.client = client or httpx.Client(timeout=COHERE_TIMEOUT_SECONDS)

    def rank(self, question: str, texts: list[str]) -> list[int]:
        """Return the positions in texts, with the best match first."""
        response = self.client.post(
            COHERE_URL,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model_name, "query": question, "documents": texts, "top_n": len(texts)},
        )
        response.raise_for_status()
        return [result["index"] for result in response.json()["results"]]


def get_reranker():
    if not config.RERANKER:
        return None
    if config.RERANKER == "cohere":
        return CohereReranker(config.COHERE_API_KEY, config.RERANKER_MODEL or COHERE_DEFAULT_MODEL)
    try:
        return FlashrankReranker()
    except ImportError as error:
        raise ValueError("RERANKER=flashrank needs this package: pip install flashrank") from error
