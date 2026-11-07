"""The real API, workflow, security checks and database, with only the model calls simulated.

The free tier allows 500 model calls a day, so a load test can't use the real model. This finds what the
app and database can carry. Search uses the real policies and the golden questions' real saved vectors.

    python -m uvicorn loadtest.server:app --port 8031

LOADTEST_MODEL_SECONDS sets how long each simulated model call waits. See docs/DECISIONS.md, D33.
"""
import json
import os
import random
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

from langchain_core.messages import AIMessage

from app import config
from app.agent.prompts import Understanding
from app.db import get_engine
from app.identity import create_identity
from app.main import app as api
from app.rag.prompt import StructuredAnswer
from app.rag.store import count_chunks, get_database_uri, get_store, run_migrations
from app.security import get_rate_store, rate_limit
from app.workflow import WorkflowRegistry, create_checkpointer

MODEL_SECONDS = float(os.getenv("LOADTEST_MODEL_SECONDS", "1.0"))
EMBEDDING_SECONDS = float(os.getenv("LOADTEST_EMBEDDING_SECONDS", "0.2"))
VECTORS_FILE = config.PROJECT_ROOT / ".cache" / "golden_embeddings.json"


class SavedVectors:
    """Real embeddings saved by the evaluation, so searches are realistic and use no quota."""

    def __init__(self) -> None:
        self.vectors = json.loads(VECTORS_FILE.read_text(encoding="utf-8"))
        self.spare = list(self.vectors.values())

    def embed_query(self, question: str) -> list[float]:
        time.sleep(EMBEDDING_SECONDS)
        return self.vectors.get(question) or random.choice(self.spare)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(value) for value in texts]


class SimulatedModel:
    """Waits like a model, then says it's a policy question with an answer citing the first excerpt."""

    def _wait(self) -> None:
        time.sleep(MODEL_SECONDS * random.uniform(0.7, 1.3))

    def with_structured_output(self, schema):
        def invoke(messages):
            self._wait()
            if schema is Understanding:
                question = messages[1].content.rsplit("Latest message: ", 1)[1]
                return Understanding(intent="policy_question", standalone_question=question)
            return StructuredAnswer(answered=True, answer="The policy covers this [1].", cited_chunk_ids=["1"])

        def stream(messages):
            yield invoke(messages)

        return SimpleNamespace(invoke=invoke, stream=stream)

    def invoke(self, messages):
        self._wait()
        return SimpleNamespace(text="A short summary of the chat.")

    def bind_tools(self, tools):
        return SimpleNamespace(invoke=lambda messages: AIMessage(content="Which details do you mean?"))


@asynccontextmanager
async def lifespan(_):
    uri = get_database_uri()
    run_migrations(uri)
    engine = get_engine(uri)
    registry = WorkflowRegistry(get_store(embeddings=SavedVectors()), SimulatedModel(), engine,
                                create_checkpointer(uri))
    registry.for_tenant(config.TENANT_ID)
    api.state.service = registry
    api.state.identity = create_identity(engine)
    api.state.count_chunks = count_chunks
    # Counted in PostgreSQL as usual, with limits set by RATE_LIMIT_* in the environment
    rate_limit.store = get_rate_store(engine)
    yield
    registry.close()


api.router.lifespan_context = lifespan
app = api
