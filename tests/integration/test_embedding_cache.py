"""The question embedding cache, on a real PostgreSQL. No model API is called."""
import pytest
from sqlalchemy import text

from app.db import get_engine
from app.rag.embedding_cache import QuestionEmbeddingCache, embedder_name, question_key
from app.rag.store import run_migrations
from tests.integration.conftest import WordEmbeddings

pytestmark = pytest.mark.integration


class CountingEmbeddings(WordEmbeddings):
    def __init__(self):
        self.asked: list[str] = []

    def embed_query(self, value):
        self.asked.append(value)
        return super().embed_query(value)


class OtherEmbeddings(CountingEmbeddings):
    """Another embedder, whose vectors must never be mixed with the first one's."""


@pytest.fixture
def engine(database_uri):
    run_migrations(database_uri)
    engine = get_engine(database_uri)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM question_embeddings"))
    yield engine
    engine.dispose()


def test_a_question_asked_again_reuses_its_vector(engine):
    cache, model = QuestionEmbeddingCache(engine, "default"), CountingEmbeddings()
    first = cache.embed_query(model, "How many earned leave days?")
    again = cache.embed_query(model, "  How many   earned leave days?  ")  # spacing doesn't matter
    assert again == pytest.approx(first)
    assert model.asked == ["How many earned leave days?"]
    cache.embed_query(model, "how many earned leave days?")  # case can change a vector, so it's asked again
    assert len(model.asked) == 2
    assert (cache.hits, cache.misses) == (1, 2)


def test_vectors_stay_with_their_organisation_and_their_embedder(engine):
    model = CountingEmbeddings()
    QuestionEmbeddingCache(engine, "default").embed_query(model, "Notice period?")
    QuestionEmbeddingCache(engine, "other_org").embed_query(model, "Notice period?")
    other_model = OtherEmbeddings()
    QuestionEmbeddingCache(engine, "default").embed_query(other_model, "Notice period?")
    assert len(model.asked) == 2 and len(other_model.asked) == 1
    assert embedder_name(model) != embedder_name(other_model)


def test_no_question_text_is_stored(engine):
    QuestionEmbeddingCache(engine, "default").embed_query(CountingEmbeddings(), "Can I carry over leave?")
    with engine.connect() as connection:
        row = connection.execute(text("SELECT * FROM question_embeddings")).mappings().one()
    assert row["text_hash"] == question_key("Can I carry over leave?")
    assert all("carry over" not in str(value) for value in row.values())


def test_a_broken_cache_still_embeds_the_question(engine):
    """Such as a database not yet migrated: the cache table is missing, so the question is embedded as usual."""
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE question_embeddings RENAME TO question_embeddings_away"))
    try:
        cache, model = QuestionEmbeddingCache(engine, "default"), CountingEmbeddings()
        assert cache.embed_query(model, "Notice period?") == pytest.approx(model.embed_query("Notice period?"))
        assert cache.misses == 1
    finally:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE question_embeddings_away RENAME TO question_embeddings"))


def test_old_vectors_are_removed_after_the_retention_period(engine):
    cache, model = QuestionEmbeddingCache(engine, "default"), CountingEmbeddings()
    cache.embed_query(model, "Old question?")
    with engine.begin() as connection:
        connection.execute(text("UPDATE question_embeddings SET created_at = now() - interval '100 days'"))
    cache.purge_older_than(90)
    cache.embed_query(model, "Old question?")
    assert len(model.asked) == 2
