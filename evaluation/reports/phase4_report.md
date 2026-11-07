# Phase 4 evaluation report

**Every result here uses draft labels.** The 123 golden questions and 58 agent questions are still `unreviewed`.

- **Answering model:** `gemini-3.5-flash-lite`. **Judge:** `gemini-3.1-flash-lite`.
- **Each run is one sample.** The model isn't fully repeatable, so small differences between phases can be chance.
- **Sample data only.** The policies and employees are fake. Nothing here has run with real people or a real sign-in.

## Golden set

| Measure | Phase 3 | Phase 4 | Phase 4 vs Phase 3 |
|---|---:|---:|---|
| Recall@5 | 0.943 | **0.943** | Same |
| Expected facts present (87) | 0.943 | **0.954** | Slightly better |
| Refused when it should (26) | 0.962 | **0.923** | **Worse: see below** |
| Wrongly refused an answerable question | 0.031 | **0.031** | Same |
| Citations valid | 1.000 | **1.000** | Same |
| Citations include a labelled chunk | 0.968 | **0.968** | Same |
| Judge: faithful | 1.000 | **1.000** | Same |
| Judge: correct | 0.979 | **0.979** | Same |
| Median seconds | 3.82 | **3.58** | Slightly better |
| p95 seconds | 8.08 | **7.29** | Slightly better, still over target |

**The refusal drop is one question: `unanswerable-009`.** It asks about the Friday dress code, and the label says the
bot should refuse. This time it answered "casual on Fridays". The Phase 3 report had already said this label looks wrong,
because the Code of Conduct policy appears to answer it, and that Phase 3's refusal flattered its score. The label hasn't
been changed, as only the owner can approve that. If it's corrected, refusals are 24 of 25, or 96.0%.

The other wrong answer is unchanged from Phase 3: `ambiguous-006`, "How long do I have to raise it?", where the workflow
assumed a grievance rather than asking what "it" means. **So the target of 100% correct refusals isn't met.**

The refusal floor in the quality gate is 0.90, so the gate passes.

### Adversarial questions

All 10 adversarial questions in the golden set were refused with the real model. They try things like changing the
bot's rules, so they cover injection through the question. They don't cover poisoned documents, which were only tested
with a scripted model (see below).

## Agent test

All 58 questions ran on Phase 4 code, with the real model, on draft labels.

| Measure | Phase 3 | Phase 4 |
|---|---:|---:|
| Intent | 100% | **100%** |
| Route | 100% | **100%** |
| Tool chosen | 100% | **100%** |
| Tool ran successfully (27 calls) | 100% | **100%** |
| Approval needed and respected (9 actions) | 100% | **100%** |
| Leave details correct | 100% | **100%** |
| Median seconds | 3.34 | **4.11** |
| Mean seconds | 3.68 | **5.08** |

**These questions took longer than in Phase 3, and that wasn't traced.** The golden set got faster over the same code, so
this may be the model service at that moment, but one run can't say. Phase 5's load test should measure it properly.

The run was split across three API keys because of Gemini's daily limit: 2 questions on the first two, and 56 on the last.

An earlier "100%" from this run was wrong: the script reloaded Phase 3's saved results instead of running. It was noticed
because the timings matched exactly. Those results were archived, and the numbers above are from the real run.

## Retrieval, and a slowdown found and fixed

| Chosen search settings | Recall@5 | MRR | p50 ms | p95 ms |
|---|---:|---:|---:|---:|
| Last measured, in Phase 1 | 0.947 | | 327 | 770 |
| Phase 4, first attempt | 0.933 | 0.895 | 745 | 1,067 |
| Phase 4, final | **0.933** | **0.895** | **166** | **224** |

**Search got slower because of Row-Level Security, and that's fixed.** PostgreSQL has to check the tenant before it can
use a full-text index, so every search parsed the text of every chunk. Searches now read a stored column of parsed text.
Timings vary from run to run on this PC: repeated runs of the same code ranged from about 170 to 320 ms.

**The recall gap isn't from Phase 4.** Search returned identical chunks and scores with and without Row-Level Security
on all 123 questions. This comparison hadn't been re-run since Phase 1, so the fall from 0.947 to 0.933 came from a change
somewhere in Phases 2 or 3. That wasn't traced. Recall in the full workflow is higher, at 0.943, because it can retry.

## Isolation

**No test found a way for one organisation to reach another's data.** That covers:

- **The database alone.** With no tenant set, the app's role sees no rows in any of the 20 protected tables. A query with
  no filter only sees its own tenant. It can't write, change or move a row across tenants. A test fails if a future table
  with a `tenant_id` column has no policy.
- **Connection pools.** The role and tenant don't survive their transaction, including after a failure, tested on a pool
  of one connection.
- **Stores.** Two organisations can have a file with the same name. One can't overwrite, delete, relabel or archive
  another's chunks, even when given the exact id. Paused runs can't be seen or deleted across tenants.
- **The API, with signed tokens.** Conversations, messages, feedback, approvals, documents, versions, jobs, users, keys and
  audit entries all return 404 or an empty list across tenants. A header or request field can't change the tenant.
- **Eight crafted prompts**, such as naming the other tenant, SQL in the question, and closing the excerpt fence. The
  scripted model records everything it's shown, and the other tenant's text never reached it. A matching test proves the
  same text is found when it's allowed, so this check can really fail.

**What this doesn't prove:** these use a scripted model, because the isolation is below the model: it can't be shown what
it never receives. Two real organisations haven't been run through a live model.

## Security checks

| Check | Result |
|---|---|
| Bad tokens: wrong audience, issuer, signature, expiry, unsigned, using a shared secret, unverified email | All refused |
| Unknown or deactivated person, suspended organisation | Refused |
| A different person using an email already claimed | Refused |
| Shared demo keys when sign-in is on | Refused |
| Employee on admin routes, HR admin on platform routes, making a platform admin | Refused |
| Revoked API key, or using an API key on admin routes | Refused |
| Rate limits across a restart and a second process | Counts kept |
| 15 requests at once against a limit of 5 | Exactly 5 allowed |
| Personal data in questions, chats, unanswered questions, feedback, tool audit, logs | Masked. The model doesn't see it either |
| Poisoned upload | Flagged, can't close its fence, invisible characters removed, no tool triggered |
| The app's database role changing or deleting audit entries | Refused |

## Running app

The API and chat screen ran in demo mode on the real policies and were checked in a browser: health, `/me`, refusals with
no key or a wrong key, a greeting stored and deleted, and rate limit counts written to the database. The migration ran
on the real local database, with 539 chunks intact and 20 tables protected.

**Not run in a browser:** the new People, API keys and Audit log tabs. Admin routes weren't checked on the running app,
because this PC has no `HELPDESK_ADMIN_KEY` set. They're tested against a real database.

## Tests

566 tests pass on a clean copy of the branch, up from 397 in Phase 3: 329 unit tests and 237 on a real PostgreSQL. Ruff,
mypy, pip-audit and the quality gate also pass. GitHub Actions and Docker have still never run.

## Success targets

| Target | Goal | Result | Met? |
|---|---|---|---|
| Recall@5 | ≥ 0.85 | 0.943 | Yes, provisionally |
| Expected facts | Not set as a target | 0.954 | |
| Correct refusals | 100% | 92.3% (24 / 26) | **No** |
| Citation correctness | ≥ 95% | 100% valid, 96.8% on target | Yes, provisionally |
| Cross-tenant leakage | 0 | 0 in every test | Yes in tests. Not tried on a live deployment |
| p95 latency | < 6 s | 7.29 s | **No** |
| API memory on Render | < 450 MB | 172 MB after startup on Windows | Not measured on Render. Peak not re-measured |
