# Phase 7 report: prove it live

Branch `phase-7-prove-it-live`, started 9 November 2026. **Part 1 of 2: the tools and checks are built and tested on this
PC. Part 2 is the live run on Render, Neon and Streamlit, which needs the owner's approval and access.** Nothing here has
been deployed or pushed yet.

## What changed

| File | Change | Why |
|---|---|---|
| `ops/check_deployment.py` | New | Checks a running deployment from the outside: up, refuses callers without a key, greets without the model, answers golden questions with citations, refuses unanswerable ones, keeps two organisations apart, and times answers |
| `ops/check_database_security.py` | New | Proves Row-Level Security holds on a given database by reading every protected table as the app's role, for each organisation, for none and for an unknown one. Read-only |
| `.github/workflows/ci.yml` | Changed | The containers job now starts the Compose stack and runs the deployment check without the model, instead of only building the images |
| `DEPLOY.md` | Changed | The upgrade steps use both checks, and a new "Checking a deployment" section |
| `docs/DECISIONS.md` | D37 added | Why the checks work this way |
| `tests/unit/test_deployment_check.py` | New, 11 tests | Every check against a pretend API, including failures, leaks and a down API |
| `tests/integration/test_database_security_check.py` | New, 5 tests | Real PostgreSQL, including a table without a policy, an app role that can skip Row-Level Security, and the app role reading password hashes |

No application code changed.

## Tests

| Run | Result |
|---|---|
| Full suite on this PC (embedded PostgreSQL) | **721 passed**, 6 min 13 s |
| ruff | Passed |
| mypy | Passed, 51 files |
| New tests | 16 passed |
| CI on GitHub | **Not run yet.** The branch hasn't been pushed |

## Checks run on this PC

### The CI smoke check, without Docker

Docker isn't installed here, so the Compose job was imitated: a new, empty embedded PostgreSQL, the real API started as a
separate process in demo mode with a fake model key, then `check_deployment --no-model --allow-empty-index`.

| Check | Result |
|---|---|
| API is up | Pass |
| Database reached and migrated (0001 to 0008 from empty) | Pass, no policies indexed, as expected |
| Refuses callers without a key | Pass, 401 |
| Greets without using the model | Pass, 0.06 s |

**The Compose stack itself still hasn't been started anywhere.** The new CI job does that on the next push.

### The full deployment check, with the real model

Against the API on this PC, its local database (539 chunks) and real Gemini, with a temporary API key for the default
organisation, revoked afterwards. Results are in `phase7_local_real_model.json`.

| Check | Result | Seconds |
|---|---|---:|
| normal-001 to normal-005 answered with citations | 5 of 5, expected facts found in all 5 | 3.35, 3.67, 3.82, 13.91, 15.54 |
| unanswerable-001 and unanswerable-002 refused | 2 of 2 | 8.20, 24.17 |
| Answer p50 / p95 (5 answers) | 3.82 s / 15.54 s | |

Five answers is too few for a reliable p95, so this is a sign, not a measurement: two of five took over 13 seconds, against a
6 second target. The second refusal took 24 seconds. Why some questions take this long is for Phase 9's per-step timings.

### Database security on the local database

`check_database_security` on this PC's database passed every check. Results are in `phase7_local_security.json`.

- `helpdesk_app` isn't a superuser, can't skip Row-Level Security, can't sign in, and owns no tables.
- All 25 protected tables have the policy. As the default organisation, 23 were read with no rows from elsewhere. 2, the
  password and session tables, were refused, as they should be.
- With no organisation set, and with a made-up one, no rows were visible in any table.

**This database has one organisation and runs as a superuser, so it isn't Neon.** The integration tests cover two
organisations. The real question, whether Neon's roles behave the same, needs the run below.

### A problem found and fixed while testing

The first real-model run hung for 150 seconds on the greeting. The cause was the test harness, not the app: it captured the
API's output in a pipe it didn't read, so the API stopped when the pipe filled. The same request took 0.12 s in-process, and the
harness now writes the API's output to a file.

## Phase 7 status

| Item from the plan | Status | Evidence or gap |
|---|---|---|
| Merge the finished branches into `main` | Planned | `main` already holds Phases 1 to 5. Password sign-in, profiles and this phase aren't merged. Needs the owner's approval |
| Row-Level Security checked on Neon | Implemented and tested | The check is built and passes locally and in tests. **Not run on Neon** |
| Deploy to Render and Streamlit, record cold start and memory | Planned | Needs the owner |
| Start the Compose stack in CI | Implemented, not tested | Imitated locally without Docker. Runs on GitHub after a push |
| Real OIDC sign-in | Planned | Needs a Google OIDC client from the owner |
| Backup and restore on Neon | Planned | `ops/backup.py` exists and is tested locally. Needs Neon access |
| Load test against the deployment | Planned | Needs the deployment |
| Slack bot with a real workspace | Planned | Optional, needs Slack tokens |

## What the owner must do for part 2

1. **Approve pushing this branch,** so the new CI job runs on GitHub.
2. **Decide what to deploy:** `main` plus password sign-in, profiles and this phase, merged in order.
3. With Neon's **direct** connection string in `DATABASE_URL` (never pasted into a chat), follow "Upgrading a running deployment"
   in `DEPLOY.md`: back up, update the database, then run `ops.check_database_security`.
4. After the merge redeploys, run `ops.check_deployment` against the live URLs, twice for cold and warm timings, and with a second
   organisation for the isolation check.
5. Read the API's memory from Render's Metrics tab after a few questions.
6. Create a Google OIDC client if real sign-in should be tested, following "Signing in" in `DEPLOY.md`.
7. Run `ops.backup create`, then a restore into a Neon branch database, and time it.

Each of these can be done with Claude in a later session, with you supplying access where needed.
