"""All project settings in one place."""
import os
import re
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# Models and database
DATABASE_URL = os.getenv("DATABASE_URL", "")  # empty = run PostgreSQL locally, in PGDATA_DIR
PGDATA_DIR = PROJECT_ROOT / "pgdata"

# Which models to use. See app/rag/providers.py for the supported providers.
DEFAULT_CHAT_MODELS = {"google": "gemini-3.5-flash-lite", "anthropic": "claude-sonnet-5-5"}
DEFAULT_EMBEDDING_MODELS = {"google": "gemini-embedding-2"}
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "google")
CHAT_MODEL = os.getenv("LLM_MODEL") or DEFAULT_CHAT_MODELS.get(LLM_PROVIDER, "")
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "google")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODELS.get(EMBEDDING_PROVIDER, "")

# Vectors from different embedding models can't be compared, so each model has its own collection
COLLECTION_NAME = (
    "hr_policies_by_section" if (EMBEDDING_PROVIDER, EMBEDDING_MODEL) == ("google", "gemini-embedding-2")
    else re.sub(r"\W+", "_", f"hr_policies_{EMBEDDING_PROVIDER}_{EMBEDDING_MODEL}")
)

# Retrieval
HYBRID_SEARCH = os.getenv("HYBRID_SEARCH", "true").lower() == "true"  # add keyword search to vector search
FETCH_K = 20            # MMR looks at this many of the closest chunks from each search...
RETRIEVE_K = 5          # ...and keeps this many that are relevant and different
MMR_LAMBDA = 0.85       # 1.0 = only relevance, 0.0 = only variety. At 0.7, the WFH Scope
                        # section (the probation exception) was dropped for being too similar
MAX_DISTANCE = float(os.getenv("MAX_DISTANCE", "0.42"))
                        # chunks further away are ignored (0 = identical meaning). Measured on
                        # 13 questions: on-topic chunks were 0.40 or closer, unrelated
                        # questions 0.46 or further. Measure again for another embedding model.
MIN_BODY_CHARS = 100    # ignore chunks that are only a one-line lead-in
RERANKER = os.getenv("RERANKER", "")  # empty = off. "flashrank" also needs: pip install flashrank
RERANKER_MODEL = "ms-marco-TinyBERT-L-2-v2"
KEYWORD_WEIGHT = 0.05   # small on purpose. evaluation/retrieval_benchmark.py lost the WFH Scope
RERANKER_WEIGHT = 0.02  # section at a keyword weight of 0.12
RERANKER_CACHE_DIR = PROJECT_ROOT / ".cache" / "flashrank"

# API
API_KEY = os.getenv("HELPDESK_API_KEY", "")
RATE_LIMIT_PER_MINUTE = 10        # for each person
RATE_LIMIT_TOTAL_PER_MINUTE = 30  # for everyone together, to protect the Gemini quota
MAX_QUESTION_CHARS = 500
MAX_HISTORY_MESSAGES = 6    # earlier chat messages used to understand a follow-up question
MAX_MESSAGE_CHARS = 2000    # the longest earlier message the API accepts
ADMIN_KEY = os.getenv("HELPDESK_ADMIN_KEY", "")  # empty = the admin endpoints are switched off
MAX_UPLOAD_CHARS = 60_000
API_URL = os.getenv("HELPDESK_API_URL", "http://127.0.0.1:8000")
