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

## D20. Reading uploads: pypdf, python-docx and the standard library

- **Problem:** HR's documents arrive as PDF, DOCX, TXT, HTML and Markdown, and Render has 512 MB.
- **Options:** `unstructured` or `docling` (heavy, with large models), `pdfplumber`, or small single-purpose libraries.
- **Choice:** `pypdf` and `python-docx`, with Python's own HTML parser. Everything becomes Markdown for the existing chunker.
- **Reason:** they're small and need no PyTorch. DOCX keeps real headings and tables.
- **Trade-off:** PDF headings are guessed from numbering and capitals, and PDF tables come out as plain lines. OCR isn't included, so scanned PDFs are refused with a clear message.

## D21. Versions keep old chunks in place, marked as superseded

- **Problem:** the bot must answer "what was the policy before?" without paying to embed old text again.
- **Options:** a separate history table and search, re-embedding old versions, or copying the old rows.
- **Choice:** copy changed and removed chunks, with their embeddings, into the same table, marked `is_current: false`. Normal searches skip them.
- **Reason:** no extra embedding quota and one search path, and full text of each version stays in `document_versions`.
- **Trade-off:** the table grows with each edit. A section that didn't change has only a current copy, which is also what applied before.

## D22. Indexing jobs run in the API process

- **Problem:** indexing can take minutes, and Render has no free background workers.
- **Options:** Celery + Redis, or FastAPI background tasks with a job table.
- **Choice:** background tasks and an `indexing_jobs` table, as the brief prefers.
- **Reason:** free, simple, and each job's progress can be seen and retried.
- **Trade-off:** a restart stops a running job. It's marked failed at startup, and a retry carries on from what's stored. If Render sleeps mid-job, that's when this applies.

## D23. Unanswered questions store no user

- **Problem:** HR needs to see gaps in the policies, but not who asked what.
- **Choice:** record the question and reason only, and apply the conversation retention period.
- **Trade-off:** a question can still contain personal details, because its text is kept as typed. Phase 4 adds masking.

## D24. A provider fallback is off unless set

- **Problem:** the main provider can fail or run out of quota, but sending HR text to another company should be a decision.
- **Choice:** `LLM_FALLBACK_PROVIDER` and `LLM_FALLBACK_MODEL`, unset by default. A fallback is only used if the main model fails before any text is written.
- **Trade-off:** a failure partway through an answer still fails, because half an answer from each model would be wrong.

## D25. Tenant isolation is enforced by the database, per transaction

**Decision:** Every tenant table has a Row-Level Security policy on `app.tenant_id`. Tenant work runs as the
`helpdesk_app` role, set with `SET LOCAL ROLE` and `set_config(..., true)` inside each transaction. Queries keep their
own tenant filters as well.

**Why:** Application filters alone fail silently the day one query forgets its filter. RLS doesn't apply to a table's
owner or a superuser, which is what the app connects as, so it has to change role. Per-transaction settings can't leak
through a connection pool, and they work behind a transaction-pooling proxy such as Neon's, where session settings
could reach another client. With no tenant set, no rows are visible, so a mistake fails closed.

**Trade-offs:** Each transaction costs one more statement. RLS also stops PostgreSQL using a full-text index before it
has checked the tenant, which added 150 to 200 ms per search here, so `langchain_pg_embedding` gets a stored
`search_text` column, added on startup because the library creates that table. Paused-run storage needed a small subclass of LangGraph's
saver, overriding its private `_cursor` method. An integration test fails if an upgrade breaks it. Looking up which
organisation a login or key belongs to has to run as the owner, in a few reviewed methods in `app/identity.py`.

## D26. Chunk ids are prefixed for every organisation but the first

**Decision:** Chunks share one collection and one id column. Other organisations' ids start with `{tenant}::`.
The first organisation's ids don't change.

**Why:** `langchain_pg_embedding.id` is a global primary key. Without the prefix, two organisations uploading
`Leave_Policy.md` would have the same ids, and one upload could overwrite the other's chunks. Keeping the first
organisation's ids means its 539 embeddings and the golden set's labels stay valid, with no quota spent. All writes
also run under RLS, so if an id ever did clash, the database would refuse the write rather than overwrite.

**Trade-offs:** LangChain's own write methods run as the owner, so `PolicyIndex` now writes with its own SQL.

## D27. Sign-in uses OpenID Connect, checked by the API with PyJWT

**Decision:** Streamlit's `st.login` (which needs Authlib) handles sign-in with the provider and passes the ID token to
the API. The API checks its signature, issuer, audience and expiry with PyJWT against the provider's published keys,
and looks up the user by verified email. Accounts must be added by an admin first. Integrations use hashed per-tenant API
keys. `AUTH_MODE=demo` keeps today's shared keys for local work and the sample deployment.

**Why:** The API is the security boundary, so it can't trust a screen's word for who someone is. OIDC means this project
stores no passwords. PyJWT is small and well maintained, and adds no web framework. No self-service sign-up, because an
HR helpdesk shouldn't accept anyone with a Google account. The first sign-in pins the provider's subject id to the
account, and `email_verified` is required, so an unproven email can't claim an account.

**Trade-offs:** An ID token lasts about an hour with most providers, after which the person signs in again, as there's
no refresh yet. A real login needs a client from a provider, which the owner has to create. Tests use a fake provider.

## D37. Password sign-in and self-service sign-up, as a third AUTH_MODE

**Decision:** `AUTH_MODE=password` adds accounts with an email and password, on a sign-in and sign-up page. Passwords are
hashed with Argon2id (`argon2-cffi`), at OWASP's minimum of 19 MB and 2 passes. A session is a random token kept in a browser
cookie, and the database holds only its hash. People who forget a password, or whom HR added, use a one-time code from an HR
admin. The first admin comes from `ops/create_admin.py`. `demo` and `oidc` still work as before.

**Why:** The owner asked for real sign-in with a sign-up page, without needing a client from a provider. This changes D27's
"stores no passwords" and "no self-service sign-up" for this mode, so these limits apply: a new account is always an employee
in the default organisation, with no link to HR records, and an email that already exists can't be claimed, so nobody can take
over an admin by signing up first. The tables holding hashes and sessions are closed to the app's tenant role.

**Trade-offs:** Emails aren't verified, because there's no email service, so anyone can register with any address unless
`SIGNUP_EMAIL_DOMAINS` or `ALLOW_SIGNUP=false` limit it, and even a domain limit isn't proof of who someone is. Forgotten
passwords need an HR admin. Streamlit can't set cookies from the server, so the page sets it with JavaScript, which means it
isn't HttpOnly: a script injected into the page could read it. Sessions can be ended, and they expire, which limits that.

## D38. Each person chooses whether their chats are saved

**Decision:** Saving chats is on by default, with a switch in Settings, kept per person in `user_preferences`. The API reads
it on each question, so no screen can store a chat someone said not to. With it off, nothing goes to `conversations` or
`messages`, the screen sends the recent messages so follow-ups still work, and an unanswered question isn't kept either. The
chat screen always opens on a fresh chat, and earlier chats are opened from History.

**Why:** The owner asked for the same control people have in ChatGPT and Claude. The wording of a question is the personal
part, so it's the part left out.

**Trade-offs:** Actions such as leave requests and tickets are still recorded, because they are real actions and need an audit
trail. Counts and timings still go to the metrics. A chat that isn't saved can't be rated, as there's no stored answer to rate.
Reopened chats have no sources, because sources aren't stored with messages.

## D39. Profiles with a photo, stored in PostgreSQL

**Decision:** A signed-in person can change their name and add a job title, department, work location and photo. The name
stays in `users.display_name`, and the rest is in `user_profiles`. The chat screen crops a photo to a square and shrinks it
with Pillow before sending it, and the API only takes PNG, JPEG or WebP under 300 KB, recognised by their first bytes.

**Why:** The owner asked for a profile with a photo shown on the chat screen. The free stack has no file storage, and a
shrunk photo is about 20 KB, so a database column is simplest. Checking the bytes rather than the file name keeps out SVG,
which can hold a script.

**Trade-offs:** Photos make backups a little larger. Pillow is only needed by the chat screen, so the API doesn't grow.

## D28. Rate limits are counted in PostgreSQL, or Redis if it's set up

**Decision:** Limits per client, per organisation and overall, over a sliding 60 seconds. Counts live in
`rate_limit_hits`, with advisory locks. Setting `REDIS_URL` moves them to Redis (`requirements-redis.txt`).

**Why:** The in-memory limiter forgot everything on a restart, and each server process counted separately. Every
deployment already has PostgreSQL, and the free tiers here have no Redis, so a new service shouldn't be required.

**Trade-offs:** Each limited request adds a database transaction. That's small next to a model call, but Redis is the
better choice at real volume. The Redis path is tested against a fake client, not a real server.

## D29. Personal data is masked before it's stored or sent to a model

**Decision:** Questions are masked at the start of the workflow, so stored chats, prompts, tickets and audit entries
only ever have the masked text. Patterns cover email, Indian phone numbers, PAN, Aadhaar, IFSC, card numbers (with a
Luhn check) and bank accounts when they're labelled. A logging filter applies the same masking to every log line.

**Why:** On Gemini's free tier, what's sent may be used by the provider, so masking only the stored copy would miss the
biggest risk. One place to mask means no path can forget. The assistant has no need for someone's Aadhaar number to
answer an HR question.

**Trade-offs:** If an employee puts a phone number in a ticket request, the ticket says `[PHONE]`. Names and free-text
addresses aren't caught, so this reduces risk rather than removing it. **The rule of sample data only on the free tier
still stands.** The HR email in an answer is kept, as it's public and answers would be useless without it.

## D30. Prompt injection is mainly stopped by what the model can't do

**Decision:** The self-service path, the only one offered tools, never sees retrieved text. Identity comes from the
request, never from the model. Risky actions need a recorded approval. On top of that, excerpts are fenced in the prompt
and can't close their own fence, invisible characters are removed, and uploads that look like instructions get a
warning and an audit entry.

**Why:** No prompt wording reliably stops injection, so the design assumes it can succeed, and limits what success can
do: at worst, a poisoned policy can give a wrong answer with a citation that points at the poisoned text.

**Trade-offs:** Pattern matching on uploads is a signal for a person, not a defence, and it'll miss rewording. Uploads
with warnings are still indexed, because HR text can legitimately say things like "ignore the above rule", and only
HR admins can upload.

## D31. Metrics are rows in PostgreSQL, not a metrics service

**Decision:** Each answer adds a row to `answer_metrics` with counts and times, and HR admins read a summary at
`/admin/metrics` and on the admin screen. No Prometheus or similar.

**Why:** The free stack has nowhere to run a scraper. A free server sleeps and loses anything in memory, and several
server processes would each hold their own counts. PostgreSQL is already there, survives all of that, and Row-Level
Security keeps each organisation's numbers apart. The rows hold no question or answer text.

**Trade-offs:** One more small write per answer. Percentiles are worked out on request, which is fine at this size but
isn't how you'd run a large service. Only chat answers are counted, so other routes are only in the logs. Embedding tokens
aren't counted, because the embedding call doesn't report them.

## D32. Tracing with Langfuse is optional and off by default

**Decision:** Setting both Langfuse keys sends model calls to Langfuse, using a LangChain callback. It's installed
separately (`requirements-tracing.txt`), and masks personal data before anything is sent.

**Why:** Tracing is useful for debugging prompts, but it copies prompts, including policy text, to another service, so that
has to be a deliberate choice. Keeping it out of the API's requirements saves memory on a 512 MB server. Answers must
still work if Langfuse is unreachable, so a failure to start only switches it off.

**Trade-offs:** It's only been tested against a fake, because using it needs an account. Masking won't catch names.

## D33. Load tests use Locust against the real stack with a simulated model

**Decision:** `loadtest/server.py` runs the real API, workflow, database and security checks, and replaces only the
model and embedding calls, with a delay based on measured model times. A small separate run uses the real model.

**Why:** The free tier allows 500 model calls a day, so a real load test can't be run on it, and it would mostly measure
Google's limits. A simulated model finds what this code and database can carry. Locust is installed in its own
environment, because it isn't needed to run the app.

**Trade-offs:** It shows the app's own limit, not Gemini's. It ran on this PC with an embedded database, so **it isn't
evidence of how Render's small server will behave.**

## D34. Backups use pg_dump, and restores rebuild the app's database role

**Decision:** `python -m ops.backup` wraps `pg_dump` and `pg_restore`, writes a list of row counts beside each backup, and
checks the restored data against it. A restore refuses a database that already has tables.

**Why:** Hosted point-in-time recovery varies by plan, and a plain dump is something the owner can hold and test.
Database roles and the owner's privileges don't belong in a dump, because they differ between hosts, so grants are
applied again on restore. Without that, Row-Level Security would be in place but the app couldn't read anything.

**Trade-offs:** A dump is a full copy of the data, unencrypted, so where it's kept is up to the owner. Restoring loses
anything since the last backup. Nothing schedules backups, as the free stack has no background workers.

## D35. Model calls have a time limit by default

**Decision:** Each chat model call stops after 20 seconds, and is retried once. Both are settings.

**Why:** With the provider's own defaults, one answer took 312 seconds during a spike in demand, and the chat screen gives
up at 150. A clear failure the person can retry is better than a long wait, and the workflow already handles a
failed step.

**Trade-offs:** While a provider is very busy, more questions fail rather than eventually getting through. A long answer
from a slow model could hit the limit, so a slower provider may need a higher value.

## D36. Failed tests appear as annotations on GitHub

**Decision:** CI uses `pytest-github-actions-annotate-failures`, which reports each failed test as an annotation.

**Why:** GitHub only shows a run's logs to someone signed in, but annotations can be read without logging in. So a failure
can be diagnosed from the run's page or API, by a person or a tool without the owner's GitHub login. It does nothing outside
GitHub Actions.

**Trade-offs:** One more test dependency, and GitHub limits how many annotations a step shows, so a large failure may not
list every test.

## D37. A deployment is checked from the outside, and its database by trying to read across organisations

**Decision:** Two scripts check a deployment. `ops/check_deployment.py` calls the running API over HTTP, as the chat screen does,
and needs only `httpx`. `ops/check_database_security.py` connects to the database, switches to the app's role, and counts rows it
can see from other organisations, in transactions that are rolled back. CI starts the Compose stack and runs the first script
without the model.

**Why:** Phase 7 has to show the deployed system works, and tests on one PC can't show that. Reading the database's settings isn't
enough on its own, because hosts differ in what roles can do, and a table's owner skips Row-Level Security unless it's forced.
Trying the reads as the app does is the direct proof. A greeting goes through sign-in, rate limits, the database and the workflow without a
model call, so CI can start the real containers with no model key.

**Trade-offs:** The full deployment check uses about 25 model calls, so it's run by hand after a deploy, not on a schedule. The CI
check proves the stack starts and the request path works, not that answers are good: that stays with the evaluation. The database
check reads every protected table, which is slow on a large database.

## D12. CI quality gate reads a committed report

- **Problem:** CI has no model key, and the real corpus embeddings are too large to keep in the repository.
- **Options:** spend API quota in CI, store embeddings, or check a committed report.
- **Choice:** CI checks the committed metrics against limits, and also checks a fingerprint of the retrieval code and golden set, so a change without a fresh report fails.
- **Reason:** deterministic and free, and it still catches a change that wasn't re-measured.
- **Trade-off:** it trusts the committed report, so it proves a measurement was recorded, not that CI repeated it.
