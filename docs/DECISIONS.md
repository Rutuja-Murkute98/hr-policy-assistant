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

- **Problem:** the brief's target flow uses RRF and a reranker, and later steps depend on which search settings are used.
- **Options:** six combinations of vector, keyword, RRF, weighted fusion, MMR and FlashRank, measured in the [Phase 1 report](../evaluation/reports/phase1_report.md).
- **Choice:** vector + keyword search, weighted fusion and MMR, with no reranker by default. RRF is a setting, and off.
- **Reason:** this had the best recall at 0.947, against 0.902 to 0.910 for RRF. FlashRank had the best MRR at 0.920, but adds about 100 MB of memory and more search time.
- **Trade-off:** this departs from the brief's RRF flow because of measurements on draft labels, so it should be re-checked after the golden set is reviewed.

## D7a. Confidence score in place of a fixed distance limit

- **Problem:** the distance limit is tied to one embedding model.
- **Options:** keep the limit, or measure how far the best chunk stands out from all chunks for the question.
- **Choice:** the confidence score, with a gate of 3.0. The distance limit stays as `ABSTENTION_MODE=distance`.
- **Reason:** it's meant to carry over between embedding models, and at 3.0 it blocked no answerable question.
- **Trade-off:** on this model it stops fewer off-topic questions before the model is called: 2 of 26, against 7 for the old limit. The model does most of the refusing, and the carry-over is untested.

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
- **Choice:** `gemini-3.1-flash-lite` by default, set by `EVAL_JUDGE_MODEL`. The answering model is `gemini-3.5-flash-lite`.
- **Reason:** only a Gemini key is available, and the free tier has to cover the run. Other models were retired or allow only 20 requests a day.
- **Trade-off:** same vendor, so errors may be less independent than with another provider.

## D13. Workflow: LangGraph, with one RAG node

- **Problem:** requests now go down several paths, and a paused action has to resume later.
- **Options:** plain Python routing, LangGraph, or a separate agent per category.
- **Choice:** LangGraph, as the brief asks, with one policy path and a category filter instead of per-category agents.
- **Reason:** each step can be tested alone, and pausing for approval and resuming after a restart are built in.
- **Trade-off:** one more dependency, and each policy question now uses one extra model call to understand it.

## D14. Checkpoints only while a run is paused

- **Problem:** Render sleeps and restarts, so a pending approval can't live in memory. But checkpoints copy the chat, which would dodge the retention rules.
- **Options:** keep every checkpoint, a table of our own, or `PostgresSaver` with finished runs deleted.
- **Choice:** `PostgresSaver`, saved only when a run exits or pauses, with finished runs' checkpoints deleted immediately.
- **Reason:** paused runs survive a restart, there are fewer database writes on Neon, and chats live in one place only.
- **Trade-off:** a crash mid-run can't resume, so the question has to be asked again. The library's tables have no `tenant_id` column, so run IDs start with the tenant, and Phase 4 must cover them in Row-Level Security.

## D15. Who approves an action

- **Problem:** the brief says applying for leave needs human approval, but not whose.
- **Options:** the employee confirms, or a manager approves.
- **Choice:** the employee confirms what the bot is about to submit.
- **Reason:** this stops the LLM acting on a misunderstanding, and manager approval needs roles, which come in Phase 4.
- **Trade-off:** this isn't manager sign-off. The mock HR system records the request as `submitted`, not approved.

## D16. Tool results use fixed wording

- **Problem:** an LLM asked to phrase a balance or payslip could change a number.
- **Options:** have the LLM phrase results, or use templates.
- **Choice:** templates. The LLM only picks the tool and its arguments.
- **Reason:** figures can't be changed in the wording, and it saves a model call.
- **Trade-off:** replies are plainer.

## D17. Employee identity from the request, never from the LLM

- **Problem:** a crafted prompt mustn't reach another employee's data.
- **Options:** an employee ID tool argument, or identity from the request only.
- **Choice:** tools have no employee argument. The ID comes from the `X-User-Id` header.
- **Reason:** the model has no way to name another employee.
- **Trade-off:** until Phase 4, anyone holding the API key can set that header, so this must stay on fake data.

## D18. Topic filter off by default

- **Problem:** the brief routes policy questions through a category filter.
- **Options:** always filter, filter and retry, or don't filter.
- **Choice:** built as `USE_CATEGORY_ROUTING`, and off by default.
- **Reason:** on the golden set, a perfect choice of every relevant topic only moved recall from 0.933 to 0.954, and limiting to one topic, as the workflow does, dropped it to 0.869. Many questions cross topics.
- **Trade-off:** this departs from the brief. Other topics stay searchable, so a stray chunk from another policy can appear.

## D19. An "out of scope" label is checked against the policies

- **Problem:** the understanding step refused real questions such as "Who is the CEO?".
- **Options:** trust the model, tune the prompt only, or check with a search.
- **Choice:** refuse only if a search also finds little, with a confidence below 4.5. The search uses an embedding call, not a chat call.
- **Reason:** those questions scored 6.48 and 4.75, while eight off-topic examples scored at most 3.81.
- **Trade-off:** an off-topic question that happens to match a policy reaches the answer step, which can still refuse it.

## D12. CI quality gate reads a committed report

- **Problem:** CI has no model key, and the real corpus embeddings are too large to keep in the repository.
- **Options:** spend API quota in CI, store embeddings, or check a committed report.
- **Choice:** CI checks the committed metrics against limits, and also checks a fingerprint of the retrieval code and golden set, so a change without a fresh report fails.
- **Reason:** deterministic and free, and it still catches a change that wasn't re-measured.
- **Trade-off:** it trusts the committed report, so it proves a measurement was recorded, not that CI repeated it.
