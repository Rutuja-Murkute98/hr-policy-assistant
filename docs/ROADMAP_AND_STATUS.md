# Roadmap and status

Labels: **Implemented and tested** · **Implemented, not tested** · **Partial** · **Planned**.
A feature is only marked as implemented after checking the code and running it.

## Plan

| Phase | Scope | State |
|---|---|---|
| Audit | Baseline, findings, these documents | Done |
| 1 | Foundation, advanced retrieval, evaluation upgrade | Built, waiting for owner review of the golden set |
| 2 | Stored conversation memory, LangGraph workflow, mock HRMS agent | Built, waiting for owner review |
| 3 | Document management, admin page, model settings, UI improvements | Waiting for approval |
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

See [the Phase 2 report](../evaluation/reports/phase2_report.md). In short: recall and citations meet their targets on draft labels, while refusals are at 88.5% and p95 latency is above the limit. Tenant leakage across every table and memory on Render aren't assessed yet.

**The CI quality gate currently fails** on refusals, at 0.885 against a floor of 0.90. Its floor hasn't been lowered, and two of the three failures look like wrong labels awaiting review.

## What the owner must set up or decide

- Review the golden set in `evaluation/data/golden_set.json`, and the agent set in `evaluation/data/agent_set.json`, and set each item to `approved` or `rejected`.
- Decide whether `adversarial-004` should count as correct, since it rejected the false claim and gave the true figure.
- Decide the five policy contradictions listed in the report.
- Decide whether `unanswerable-009` should be corrected, since the policy appears to answer it.
- Push the branch, so CI runs for the first time.
- Add a Cohere key, Slack tokens or other provider keys only if those options are wanted.
