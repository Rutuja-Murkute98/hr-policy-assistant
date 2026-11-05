# HR Helpdesk RAG

A chatbot that answers employee questions from the 33 Rutuja Talent Nexus HR policy documents, and refuses when the answer isn't in them.

> This is a starting README. The guide's Week 4 milestone asks you to rewrite it in your own words.

## How it works

```
Offline, once:  policies -> chunk by heading -> embed with Gemini -> store in pgvector
Each question:  embed question -> find relevant chunks -> strict prompt -> Gemini -> answer + sources
```

| Stage | What it does | File |
|---|---|---|
| A | Heading-based chunking with metadata | `pipeline/chunk_documents.py` |
| B | Offline indexing, resumes if interrupted | `pipeline/embed_and_store.py` |
| C | Retrieval: MMR, distance limit, deduplication | `app/rag/service.py` |
| D | Strict prompt with a fixed refusal sentence | `app/rag/prompt.py` |
| E | API: key check, rate limit, request logging | `app/main.py`, `app/security.py` |
| F | Chat screen that calls the API | `ui/streamlit_app.py` |
| G | Evaluation on 25 questions | `evaluation/rag_evaluation.py` |

`mini_rag.py` is the original one-file version from Week 1.

## Setup

```bash
python -m venv venv
venv\Scripts\pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your Gemini key and any long random text for `HELPDESK_API_KEY`.

PostgreSQL and pgvector run inside Python through `pixeltable-pgserver`, so Docker isn't needed. The data is kept in `pgdata/`.

## Run it

Run each command from the project folder. Indexing is only needed once.

```bash
venv\Scripts\python -m pipeline.embed_and_store
```

```bash
venv\Scripts\uvicorn app.main:app --port 8000
```

In a second terminal:

```bash
venv\Scripts\streamlit run ui\streamlit_app.py
```

The chat screen opens at http://localhost:8501, and the API's test page is at http://127.0.0.1:8000/docs.

## Test it

```bash
venv\Scripts\python -m pytest tests -q
```

```bash
venv\Scripts\python -m evaluation.rag_evaluation
```

## Free-tier limits

Gemini's free tier allows 100 embeddings a minute and 1,000 a day per project. A full re-index uses 539, so settle chunking changes before re-indexing. After editing a policy, run the indexing script with `--rebuild`, because existing sections aren't re-embedded otherwise.
