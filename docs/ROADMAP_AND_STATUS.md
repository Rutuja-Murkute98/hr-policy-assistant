# Roadmap and status

Labels: **Implemented and tested** · **Implemented, not tested** · **Partial** · **Planned**.
A feature is only marked as implemented after checking the code and running it.

## Plan

| Phase | Scope | State |
|---|---|---|
| Audit | Baseline, findings, these documents | Done |
| 1 | Foundation, advanced retrieval, evaluation upgrade | Built, waiting for owner review of the golden set |
| 2 | Stored conversation memory, LangGraph workflow, mock HRMS agent | Built, waiting for owner review |
| 3 | Document management, admin page, model settings, UI improvements | Built, waiting for owner review |
| 4 | Multi-tenancy, login, Row-Level Security, security hardening | Waiting for approval |
| 5 | Observability, load test, deployment guides, backups | Waiting for approval |
| 6 | Optional advanced features | Only after Phases 1 to 5 |

## Phase 1 status

| Feature | Status | Evidence or gap |
|---|---|---|
| Settings checked at startup | Implemented and tested | `tests/unit/test_settings.py` |
| Company details in a tenant file | Implemented and tested | Includes a test that no company details remain in the code |
| Alembic migrations with `tenant_id` | Implemented and tested | Run on a real PostgreSQL in integration tests |
| Docker Compose | Implemented, not tested | This PC has no Docker. CI will build it on the first push |
| GitHub Actions: tests, ruff, mypy, pip-audit | Partial | All pass locally, but the workflow has never run on GitHub |
| All dependencies pinned | Implemented and tested | Direct dependencies are pinned, and CI's install was last tried before the newest pins |
| Linux and macOS commands | Implemented, not tested | No Linux machine was available |
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
| Baseline vs new report | Implemented and tested | `evaluation/reports/phase1_report.md` |
| CI quality gate | Partial | Works locally on a clean copy, but hasn't run on GitHub |

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
| Audit log for every tool call | Implemented and tested | Arguments are stored unmasked until Phase 4 |
| Agent evaluation | Implemented and tested | Draft labels, 100% on a small test |
| Real sign-in | Planned | Phase 4. Until then, anyone with the API key can name any user, so this must stay on fake data |

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
| Admin role only | Partial | One shared admin key until Phase 4 |
| Chat, embedding and rerank providers by setting | Partial | Only Gemini and FlashRank have run live |
| Temperature, max tokens, timeouts and retries | Implemented, not tested | Mapped per provider with fakes. Not exercised live |
| Store embedding model and dimension, refuse to mix | Implemented and tested | Real PostgreSQL |
| Fallback provider only if enabled | Implemented, not tested | Fakes only, with no second provider set up |
| Real streaming over SSE | Implemented and tested | Short answers arrive in few pieces |
| Clickable citations showing the source section | Implemented and tested | Built by the UI session, with tests |
| Thumbs up/down per answer | Partial | Shown in a browser and saved through the API, but not clicked |
| Workflow status messages | Implemented and tested | Fixed wording, so no reasoning or prompts are shown |

## Earlier work

| Feature | Status | Evidence or gap |
|---|---|---|
| FastAPI app, key check, rate limits, logging | Implemented and tested | Rate limits are in memory until Phase 4 |
| Streaming answers | Implemented and tested | Short answers arrive in very few pieces |
| Admin endpoints and screen | Partial | Endpoints tested on a real database. Upload and remove buttons not clicked in a browser |
| Swappable providers | Partial | Only Gemini has run live |
| Slack bot | Implemented, not tested | Tested only with fakes, because there are no Slack tokens |
| Chat history in the request | Implemented and tested | Not stored on the server until Phase 2 |

## Success targets

See [the Phase 3 report](../evaluation/reports/phase3_report.md). In short: recall and citations meet their targets on draft labels, while refusals are at 96.2% and p95 latency is above the limit. Tenant leakage across every table and memory on Render aren't assessed yet.

**The CI quality gate passes locally**, but partly because one likely wrong label now counts in its favour, so the owner's review may change that.

## What the owner must set up or decide

- Review the golden set in `evaluation/data/golden_set.json`, and the agent set in `evaluation/data/agent_set.json`, and set each item to `approved` or `rejected`.
- Decide whether `adversarial-004` should count as correct, since it rejected the false claim and gave the true figure.
- Decide the five policy contradictions listed in the report.
- Decide whether `unanswerable-009` should be corrected, since the policy appears to answer it.
- Push the branch, so CI runs for the first time.
- Add a Cohere key, Slack tokens or other provider keys only if those options are wanted.
