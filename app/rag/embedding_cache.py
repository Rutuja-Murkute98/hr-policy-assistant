"""Keep the vector of each question already asked, so asking it again needs no embedding call.

Employees ask the same questions often, and every embedding call costs time and free-tier quota. The cache is keyed
by a hash of the question's masked text and the embedder that made the vector, so a new model never reuses an old
model's vectors.
It holds no question text. Rows belong to one organisation, under Row-Level Security, and are removed after the
conversation retention period. If the cache can't be read or written, the question is embedded as usual.
"""
import hashlib
import logging
import re

from sqlalchemy import text

from app.tenancy import TenantEngine, tenant_scoped

logger = logging.getLogger("hr_helpdesk")
LOOKUP = text("SELECT embedding FROM question_embeddings "
              "WHERE tenant_id = :tenant_id AND model = :model AND text_hash = :text_hash")
STORE = text("INSERT INTO question_embeddings (tenant_id, model, text_hash, embedding) "
             "VALUES (:tenant_id, :model, :text_hash, :embedding) ON CONFLICT DO NOTHING")
PURGE = text("DELETE FROM question_embeddings WHERE tenant_id = :tenant_id "
             "AND created_at < now() - make_interval(days => :days)")


def embedder_name(embeddings) -> str:
    """Name the embedder by its class and model, so vectors from two different embedders never mix."""
    kind = type(embeddings)
    return f"{kind.__module__}.{kind.__name__}:{getattr(embeddings, 'model', '') or ''}"


def question_key(question: str) -> str:
    """The same words, spaced differently, are the same question. Case is kept, as it can change a vector."""
    return hashlib.sha256(re.sub(r"\s+", " ", question).strip().encode()).hexdigest()


class QuestionEmbeddingCache:
    def __init__(self, engine, tenant_id: str):
        self.engine: TenantEngine = tenant_scoped(engine, tenant_id)
        self.tenant_id = tenant_id
        self.hits = 0
        self.misses = 0

    def _scope(self, embeddings, question: str) -> dict:
        return {"tenant_id": self.tenant_id, "model": embedder_name(embeddings), "text_hash": question_key(question)}

    def embed_query(self, embeddings, question: str) -> list[float]:
        """Return the question's vector from the cache, or embed it with the model and keep it."""
        scope = self._scope(embeddings, question)
        try:
            with self.engine.begin() as connection:
                found = connection.execute(LOOKUP, scope).scalar()
        except Exception as error:
            logger.warning("Couldn't read the question embedding cache: %s", type(error).__name__)
            found = None
        if found is not None:
            self.hits += 1
            return list(found)
        self.misses += 1
        vector = embeddings.embed_query(question)
        try:
            with self.engine.begin() as connection:
                connection.execute(STORE, {**scope, "embedding": list(vector)})
        except Exception as error:  # only a later question loses out
            logger.warning("Couldn't save to the question embedding cache: %s", type(error).__name__)
        return vector

    def purge_older_than(self, days: int) -> None:
        with self.engine.begin() as connection:
            connection.execute(PURGE, {"tenant_id": self.tenant_id, "days": days})
