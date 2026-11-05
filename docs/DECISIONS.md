# Decisions

One short entry per technology or design choice. A technology is added here before it's added to the code.

## D1. Settings: Pydantic Settings plus a tenant file

- **Problem:** company details, models and thresholds were hardcoded, and bad values only failed later.
- **Options:** plain environment variables, Pydantic Settings, a config service.
- **Choice:** Pydantic Settings for deployment settings, and `config/tenant.toml` for company details.
- **Reason:** settings are checked at startup, and the UI can read the TOML file without installing the API's packages.
- **Trade-off:** one more file, and the UI reads it separately from the API.

## D2. Migrations: Alembic

- **Problem:** there was no way to change the database schema safely.
- **Options:** hand-written SQL, Alembic.
- **Choice:** Alembic, for tables this project owns. Every new table has `tenant_id`.
- **Reason:** it's the standard tool for SQLAlchemy, which `langchain-postgres` already uses.
- **Trade-off:** LangChain still creates its own tables, so migrations only add to them.

## D3. Keep LangChain's vector tables

- **Problem:** the brief needs metadata filters and full-text search, which could suggest new tables.
- **Options:** a custom chunks table, or keep `langchain_pg_embedding` and query it directly.
- **Choice:** keep them, with filters on JSONB metadata. The full-text index is added when the app connects, because LangChain creates this table itself and it may not exist when migrations run.
- **Reason:** moving would risk working code with no measured gain.
- **Trade-off:** `tenant_id` for chunks lives in metadata, not a column. Phase 4 must revisit this for Row-Level Security.

## D4. Containers: Docker Compose for local development

- **Problem:** setup differs between machines.
- **Options:** Docker Compose, or only the embedded PostgreSQL.
- **Choice:** add Compose, and keep the embedded PostgreSQL for machines without Docker.
- **Reason:** the brief asks for it, and the development PC can't run Docker.
- **Trade-off:** the container files can't be run on that PC, so CI has to build them.

## D5. Code checks: ruff, mypy and pip-audit

- **Problem:** CI only ran tests.
- **Options:** ruff or flake8, mypy or pyright.
- **Choice:** ruff, mypy and pip-audit.
- **Reason:** ruff is fast and covers import order, and mypy is widely known and needs no Node.js.
- **Trade-off:** third-party packages without types are ignored by mypy.

## D6. Reranking: none by default, FlashRank or Cohere by choice

- **Problem:** reranking may improve ranking, but Render's free tier has 512 MB and PyTorch is ruled out.
- **Options:** PyTorch cross-encoders (ruled out), FlashRank (ONNX, local), Cohere Rerank (API).
- **Choice:** a setting with three values, off by default.
- **Reason:** both options fit the rules. Cohere needs a key, and FlashRank's memory on Render hasn't been measured.
- **Trade-off:** the default gives up any ranking gain until one is switched on.

## D7. Search defaults

Filled in from measured results. See the end of [ROADMAP_AND_STATUS.md](ROADMAP_AND_STATUS.md).

## D8. Structured answers with labelled excerpts

- **Problem:** exact-string refusal is brittle, and sources don't show what was used.
- **Options:** parse inline markers from plain text, or structured output.
- **Choice:** structured output with `answered`, `answer` and `cited_chunk_ids`. Excerpts are numbered, such as `[1]`, and the service maps those numbers back to chunk IDs.
- **Reason:** short numbers are harder to mistype than long IDs, and every provider supports structured output.
- **Trade-off:** the answer has to be read from partial JSON while streaming, and a final check can replace streamed text.

## D9. Full-text search through PostgreSQL

- **Problem:** vector search misses exact terms.
- **Options:** PostgreSQL full-text search, a separate search engine, a BM25 extension.
- **Choice:** PostgreSQL `tsvector` with `ts_rank_cd`.
- **Reason:** no new service, and it works on Neon's free tier.
- **Trade-off:** it's BM25-style, not true BM25, and the `english` setting doesn't suit other languages.

## D10. Golden set labels need human review

- **Problem:** the brief says I may only draft ground truth.
- **Choice:** every golden item has `review_status`, and reports state how many are approved.
- **Reason:** labels written by the system's author can be biased, even when checked against the text.
- **Trade-off:** results stay provisional until reviewed.

## D11. Judge model

- **Problem:** the answerer shouldn't grade itself.
- **Options:** another provider, or another model from the same provider.
- **Choice:** a different Gemini model by default, set by `EVAL_JUDGE_MODEL`.
- **Reason:** only a Gemini key is available, and the free tier has to cover the run.
- **Trade-off:** same vendor, so errors may be less independent than with another provider.

## D12. CI quality gate reads a committed report

- **Problem:** CI has no model key, and the real corpus embeddings are too large to keep in the repository.
- **Options:** spend API quota in CI, store embeddings, or check a committed report.
- **Choice:** CI checks the committed metrics against limits, and also checks a fingerprint of the retrieval code and golden set, so a change without a fresh report fails.
- **Reason:** deterministic and free, and it still catches a change that wasn't re-measured.
- **Trade-off:** it trusts the committed report, so it proves a measurement was recorded, not that CI repeated it.
