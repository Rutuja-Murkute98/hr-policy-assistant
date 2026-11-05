"""All project settings in one place."""
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# Models and database
DATABASE_URL = os.getenv("DATABASE_URL", "")  # empty = run PostgreSQL locally, in PGDATA_DIR
PGDATA_DIR = PROJECT_ROOT / "pgdata"
COLLECTION_NAME = "hr_policies_by_section"
EMBEDDING_MODEL = "gemini-embedding-2"
CHAT_MODEL = "gemini-3.5-flash-lite"

# Retrieval
FETCH_K = 20            # MMR looks at this many of the closest chunks...
RETRIEVE_K = 5          # ...and keeps this many that are relevant and different
MMR_LAMBDA = 0.85       # 1.0 = only relevance, 0.0 = only variety. At 0.7, the WFH Scope
                        # section (the probation exception) was dropped for being too similar
MAX_DISTANCE = 0.42     # chunks further away are ignored (0 = identical meaning). Measured on
                        # 13 questions: on-topic chunks were 0.40 or closer, unrelated
                        # questions 0.46 or further
MIN_BODY_CHARS = 100    # ignore chunks that are only a one-line lead-in

# API
API_KEY = os.getenv("HELPDESK_API_KEY", "")
RATE_LIMIT_PER_MINUTE = 10        # for each person
RATE_LIMIT_TOTAL_PER_MINUTE = 30  # for everyone together, to protect the Gemini quota
MAX_QUESTION_CHARS = 500
API_URL = os.getenv("HELPDESK_API_URL", "http://127.0.0.1:8000")
