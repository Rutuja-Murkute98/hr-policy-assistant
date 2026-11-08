# Roadmap and status

Labels: **Implemented and tested** · **Implemented, not tested** · **Partial** · **Planned**.
A feature is only marked as implemented after checking the code and running it.

For the overall picture, see [the final report](FINAL_REPORT.md).

## Plan

| Phase | Scope | State |
|---|---|---|
| Audit | Baseline, findings, these documents | Done |
| 1 | Foundation, advanced retrieval, evaluation upgrade | Built, waiting for owner review of the golden set |
| 2 | Stored conversation memory, LangGraph workflow, mock HRMS agent | Built, waiting for owner review |
| 3 | Document management, admin page, model settings, UI improvements | Built, waiting for owner review |
| 4 | Multi-tenancy, login, Row-Level Security, security hardening | Built, waiting for owner review |
| 5 | Observability, load test, deployment guides, backups | Built, waiting for owner review |
| 6 | Optional advanced features | Only after Phases 1 to 5 |

## Phase 1 status

| Feature | Status | Evidence or gap |
|---|---|---|
| Settings checked at startup | Implemented and tested | `tests/unit/test_settings.py` |
| Company details in a tenant file | Implemented and tested | Includes a test that no company details remain in the code |
| Alembic migrations with `tenant_id` | Implemented and tested | Run on a real PostgreSQL in integration tests |
| Docker Compose | Partial | CI checks the file and builds the images, but the containers have never been started |
| GitHub Actions: tests, ruff, mypy, pip-audit | Implemented and tested | Passing on GitHub, on Linux with PostgreSQL 16 |
| All dependencies pinned | Implemented and tested | Direct dependencies are pinned, and CI's install was last tried before the newest pins |
| Linux and macOS commands | Implemented, not tested | The tests pass on Linux in CI, but the setup commands in the guides haven't been followed there |
| Hybrid search | Implemented and tested | Integration tests, and measured on the golden set |
| Reciprocal Rank Fusion | Implemented and tested | Measured worse here, so it's off by default |
| FlashRank reranking | Implemented and tested | Measured for retrieval, not in a full answer run |
| Cohere reranking | Implemented, not tested | Tested only with fakes, because there's no key |
| Metadata filters: policy, category, effective date, tenant | Implemented and tested | Integration tests on real SQL |
| Confidence-based abstention | Implemented and tested | Measured, but not stronger than a distance limit on this model. Carrying over to other models is untested |
| Structured output | Implemented and tested | Fakes, plus live runs |
| Inline citations checked against retrieved chunks | Implemented and tested | Live numbers still start at the excerpt's position, such as [5] |
| Content hash per document | Implemented and tested | Includes renamed and removed sections |
| Rewrite follow-ups | Implemented and tested | Measured on 10 follow-up questions |
| Rewrite vague questions with no history | Planned | Moved to Phase 2's retry step |
| Golden set of 100+ with chunk labels | Partial | 123 drafted, none approved yet |
| Retrieval metrics | Implemented and tested | Recall, precision, MRR, NDCG and latency |
| Answer metrics with a different judge | Implemented and tested | The judge is from the same vendor |
| Baseline vs new report | Implemented and tested | `evaluation/reports/history/phase1_report.md` |
| CI quality gate | Implemented and tested | Passing on GitHub. It reads a committed result, and doesn't run the model |

## Phase 2 status

| Feature | Status | Evidence or gap |
|---|---|---|
| Conversations and messages per user and tenant | Implemented and tested | Real PostgreSQL. Isolation tests for other users and tenants |
| Rewrite follow-ups from recent history | Implemented and tested | Measured on 10 follow-up questions |
| Summarise long conversations | Implemented and tested | Scripted model only, not a long real chat |
| Retention period and deleting a user's history | Implemented and tested | Real PostgreSQL |
| LangGraph workflow with intent routing | Implemented and tested | 58 agent questions and 123 golden questions |
| Category filter for policy questions | Implemented and tested | Built, but measured worse, so it's off by default |
| Grade evidence, rewrite and retry once | Implemented and tested | Retry rewords only the search |
| Out-of-scope refusal | Implemented and tested | Checked against a search first |
| Mock HRMS with fake employees | Implemented and tested | Fake data only, with a replaceable interface |
| Leave balance, payslip and ticket tools | Implemented and tested | Fixed wording, with figures from the tool |
| Apply for leave with human approval | Implemented and tested | Includes a real browser run. The employee approves, not a manager |
| Pause and resume across a restart | Implemented and tested | Scripted model on real PostgreSQL. Not tried on Render |
| Retries with backoff | Implemented and tested | Simulated failures |
| Audit log for every tool call | Implemented and tested | Arguments are masked, and the app can't change entries |
| Agent evaluation | Implemented and tested | Draft labels, 100% on a small test |
| Real sign-in | Partial | See Phase 4. Built, but not yet run with a real provider |

## Phase 3 status

| Feature | Status | Evidence or gap |
|---|---|---|
| Upload PDF, DOCX, TXT, Markdown, HTML through the API | Implemented and tested | PDF and DOCX uploaded for real, others in tests |
| Upload through the admin screen | Implemented, not tested | The form opens, but a browser tool can't drive the file picker. The API route it calls was tested for real |
| Validate file type and size | Implemented and tested | Includes fake files and a DOCX that unpacks to something huge |
| Extract text, headings and tables | Implemented and tested | PDF headings are guessed, and PDF tables come out as plain lines |
| OCR | Planned | Scanned PDFs are refused with a clear message. Off by design because of memory |
| Title, category, version, effective date, uploaded by | Implemented and tested | "Uploaded by" is typed in until roles arrive in Phase 4 |
| Only re-embed changed chunks | Implemented and tested | Measured live: 1 of 2 sections |
| Respect the embedding rate limit | Implemented and tested | Batches and waits, tested with fakes |
| Resume after failure | Implemented and tested | Fake embeddings failing mid-job, not a real quota failure |
| Versions, keeping old ones | Implemented and tested | Real PostgreSQL |
| Answer "what was the policy before?" | Implemented and tested | Real model, on one made-up policy |
| Duplicate detection by content hash | Implemented and tested | Live and in tests |
| Job status in a table, shown on the admin page | Implemented and tested | The table of jobs on the admin page was only seen empty |
| Admin page: documents, versions, jobs, retry, unanswered, feedback | Partial | Tabs opened in a browser. The retry and remove buttons weren't clicked |
| Admin role only | Implemented and tested | HR admin role with sign-in. Demo mode still has one shared admin key |
| Chat, embedding and rerank providers by setting | Partial | Only Gemini and FlashRank have run live |
| Temperature, max tokens, timeouts and retries | Implemented, not tested | Mapped per provider with fakes. Not exercised live |
| Store embedding model and dimension, refuse to mix | Implemented and tested | Real PostgreSQL |
| Fallback provider only if enabled | Implemented, not tested | Fakes only, with no second provider set up |
| Real streaming over SSE | Implemented and tested | Short answers arrive in few pieces |
| Clickable citations showing the source section | Implemented and tested | Built by the UI session, with tests |
| Thumbs up/down per answer | Partial | Shown in a browser and saved through the API, but not clicked |
| Workflow status messages | Implemented and tested | Fixed wording, so no reasoning or prompts are shown |

## Phase 4 status

| Feature | Status | Evidence or gap |
|---|---|---|
| Organisations, users and roles | Implemented and tested | Real PostgreSQL through the API. The new admin tabs weren't opened in a browser |
| OIDC login for the UI | Partial | Token checks tested against a fake provider, and the screens with a pretend sign-in. Never run with a real provider, which needs a client from the owner |
| API keys per tenant | Implemented and tested | Hashed, revocable and limited to their organisation. The Slack bot hasn't been run with one |
| Row-Level Security on all tenant tables | Implemented and tested | 20 tables, on embedded PostgreSQL. Not run on Neon, which needs its role permission checked |
| Application checks as well | Implemented and tested | Stores refuse another tenant's data, and the tenant comes only from the caller's identity |
| Isolation tests, including crafted prompts | Implemented and tested | Scripted model, showing what it's given. Not two real organisations on a live model |
| Rate limits that survive restarts and multiple workers | Implemented and tested | PostgreSQL, with 15 requests at once against a limit of 5. Not load tested |
| Rate limits in Redis | Implemented, not tested | Only against a fake client, because there's no Redis here |
| Prompt injection defences | Partial | Tools never see retrieved text, excerpts are fenced, and uploads are flagged. Poisoned documents were only tested with a scripted model. Pattern checks can be reworded around |
| PII detection and masking | Implemented and tested | Before the model and storage, and in logs. Names and free-text addresses aren't caught |
| Audit log | Implemented and tested | Logins, document changes, approvals, people, keys and chat deletions. The app's role can't change entries |
| DPDP Act | Partial | Documented in `docs/DPDP_NOTES.md`. Several requirements aren't built, and there's been no legal review |
| Agent evaluation on Phase 4 code | Implemented and tested | Draft labels, 100% on a small test. Slower than Phase 3, not traced |

## Phase 5 status

| Feature | Status | Evidence or gap |
|---|---|---|
| Structured JSON logs with request IDs | Implemented and tested | Seen on the running API. Render's log view hasn't been checked |
| Never log question text or PII | Implemented and tested | Tests on success and failure. Exception messages are left out, which makes some failures harder to diagnose |
| Metrics: requests, latency, tokens, cost, refusals, tool failures, feedback | Implemented and tested | On real model runs. Embedding tokens aren't counted, and the cost uses prices you set |
| Usage tab on the admin screen | Implemented, not tested | Its API is tested, and the tab opens in a screen test, but it hasn't been opened in a browser |
| LLM tracing with Langfuse | Implemented, not tested | Only against a fake, because there's no account |
| Load test with Locust | Partial | Real results on this PC, with a simulated model and a small real-model sample. Not run against a deployment |
| Deployment guide for the free stack | Partial | Updated, but the upgrade from earlier phases hasn't been run on Render or Neon |
| Optional AWS and GCP guide | Implemented, not tested | Never run: there's no account, and the images have never been built here |
| Backup and restore | Partial | Real restore on the local database, and automated tests. Not run on Neon, and nothing schedules it |
| Time limit on model calls | Implemented and tested | 20 seconds and one retry, on a real run with the provider under load |

## Password sign-in

| Feature | Status | Evidence or gap |
|---|---|---|
| Sign-up, sign-in and sign-out with an email and password | Implemented and tested | Real PostgreSQL through the API, and clicked through in a real browser, locally |
| Staying signed in between visits, and the last chat restored | Implemented and tested | In a real browser, locally. Not run on Streamlit Community Cloud |
| Set-up codes for people HR adds or who forget a password | Implemented and tested | API on real PostgreSQL, and the admin screen and form clicked through in a real browser, locally |
| Fresh chat on every visit, with Back, History and Settings | Implemented and tested | Clicked through in a real browser, locally |
| Choosing whether chats are saved | Implemented and tested | Real PostgreSQL through the API, and in a real browser, locally |
| Email verification | Planned | There's no email service, so an email address isn't proven |

## Earlier work

| Feature | Status | Evidence or gap |
|---|---|---|
| FastAPI app, key check, rate limits, logging | Implemented and tested | Limits are stored in PostgreSQL. See Phase 4 |
| Streaming answers | Implemented and tested | Short answers arrive in very few pieces |
| Admin endpoints and screen | Partial | Endpoints tested on a real database. Upload and remove buttons not clicked in a browser |
| Swappable providers | Partial | Only Gemini has run live |
| Slack bot | Implemented, not tested | Tested only with fakes, because there are no Slack tokens |
| Chat history in the request | Implemented and tested | Not stored on the server until Phase 2 |

## Success targets

See [the Phase 5 report](../evaluation/reports/phase5_report.md). The Phase 3 summary was: recall and citations meet their targets on draft labels, while refusals are at 96.2% and p95 latency is above the limit. Tenant leakage across every table and memory on Render aren't assessed yet.

**The CI quality gate passes on GitHub**, but partly because one likely wrong label now counts in its favour, so the owner's review may change that.

## What the owner must set up or decide

- Review the golden set in `evaluation/data/golden_set.json`, and the agent set in `evaluation/data/agent_set.json`, and set each item to `approved` or `rejected`.
- Decide whether `adversarial-004` should count as correct, since it rejected the false claim and gave the true figure.
- Decide the five policy contradictions listed in the report.
- Decide whether `unanswerable-009` should be corrected, since the policy appears to answer it. It now lowers the refusal score.
- Decide about Phase 6, which is optional.
- Run a backup and restore against the hosted database, and decide how often to take backups.
- On the free tier, consider lowering `RATE_LIMIT_TOTAL_PER_MINUTE`, as 3 active people hit the model's limit in testing.
- For real sign-in: create an OIDC client and follow "Signing in" in `DEPLOY.md`.
- Before real employee data: work through the list at the end of `docs/DPDP_NOTES.md`, including legal advice.
- Add a Cohere key, Slack tokens or other provider keys only if those options are wanted.
