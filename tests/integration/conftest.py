"""Shared setup for tests on a real PostgreSQL with pgvector. No model API is called."""
import hashlib
import math
import os
import re

import numpy as np
import pytest


class WordEmbeddings:
    """Deterministic embeddings: texts that share words get similar vectors, with no API call."""

    DIMENSIONS = 64

    def _embed(self, value: str) -> list[float]:
        vector = np.zeros(self.DIMENSIONS)
        for word in re.findall(r"\w+", value.lower()):
            vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % self.DIMENSIONS] += 1
        norm = math.sqrt(float(vector @ vector)) or 1.0
        return (vector / norm).tolist()

    def embed_documents(self, texts):
        return [self._embed(value) for value in texts]

    def embed_query(self, value):
        return self._embed(value)


@pytest.fixture(scope="session")
def database_uri(tmp_path_factory):
    """Use TEST_DATABASE_URL if it's set, as in CI. Otherwise start a temporary embedded PostgreSQL."""
    uri = os.getenv("TEST_DATABASE_URL")
    if uri:
        yield uri
        return
    pgserver = pytest.importorskip("pixeltable_pgserver")
    server = pgserver.get_server(tmp_path_factory.mktemp("pg"), cleanup_mode="stop")
    yield server.get_uri()
    server.cleanup()
