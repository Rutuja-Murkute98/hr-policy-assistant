# Phase 5 evaluation report

**Every result here uses draft labels.** The 123 golden questions and 58 agent questions are still `unreviewed`.

- **Answering model:** `gemini-3.5-flash-lite`. **Judge:** `gemini-3.1-flash-lite`.
- **Each run is one sample.** The model isn't fully repeatable, and its speed changes with demand on Google's service.
- **This ran on one Windows PC with an embedded database.** It isn't evidence of how Render and Neon will behave.

## Golden set

| Measure | Phase 4 | Phase 5 | Phase 5 vs Phase 4 |
|---|---:|---:|---|
| Recall@5 | 0.943 | **0.938** | One question: `normal-047` |
| Expected facts present (87) | 0.954 | **0.966** | Slightly better |
| Refused when it should (26) | 0.923 | **0.923** | Same two questions |
| Wrongly refused an answerable question | 0.031 | **0.031** | Same three questions |
| Citations valid | 1.000 | **1.000** | Same |
| Citations include a labelled chunk | 0.968 | **0.957** | Slightly worse |
| Judge: faithful | 1.000 | **1.000** | Same |
| Judge: correct | 0.979 | **1.000** | Slightly better |
| Median seconds | 3.58 | **4.36** | **Worse: see below** |
| p95 seconds | 7.29 | **14.87** | **Much worse: see below** |

**The p95 of 14.87 s overstates it.** Four questions failed with a Google error and the evaluation script waited 30 seconds
before trying them again, and its stopwatch includes that. The app's own metrics, which only count answers that
succeeded, give a p50 of 4.17 s and a **p95 of 10.78 s**. That's still worse than Phase 4 and over the target of 6 s.

**This is mostly Google's service, not this code, but one run can't prove that.** Google was returning "high demand" errors
that day. The first attempt at this run was so affected that it's kept separately in `answers_phase5_busy_provider.json`:
its p95 was 15.4 s, and one answer took 312 s, which led to the new time limit. The code added in this phase is a small
amount of timing around existing steps and one database write, which is included in the "everything else" row below.

**Refusals:** `unanswerable-009`, the Friday dress code, is probably a wrong label. `ambiguous-006` is a real miss. **The
100% target isn't met.**

## Agent test

| Measure | Phase 3 | Phase 4 | Phase 5 |
|---|---:|---:|---:|
| Intent, route, tool, tool success, approvals, leave details | 100% | 100% | **100%** |
| Median seconds | 3.34 | 4.11 | **3.64** |
| Mean seconds | 3.68 | 5.08 | **3.83** |

Phase 4's slower result didn't repeat, and no code that would speed it up changed in between, so it was probably
variation in the model's speed. This run happened before the time limit was added. That limit only affects very slow calls,
and the test wasn't run again.

## Where the time goes

From the new metrics, for the 123 questions:

| Step | p50 ms | p95 ms | Share of the median |
|---|---:|---:|---:|
| Model calls | 2,716 | 8,489 | 65% |
| Embedding the question | 729 | 1,350 | 17% |
| Searching and selecting chunks | 411 | 1,159 | 10% |
| Everything else, including all database writes | 157 | 223 | 4% |
| **Total** | **4,168** | **10,783** | |

Shares don't add up exactly, because each step's median comes from a different answer.

| Model calls per answer | Answers | p50 ms | p95 ms |
|---:|---:|---:|---:|
| 1 | 15 | 3,848 | 6,701 |
| 2 | 95 | 4,069 | 8,463 |
| 3, when a weak search is retried | 12 | 9,097 | 18,364 |
| 4 | 1 | 9,363 | 9,363 |

**So the p95 target depends on the model.** Even if every other step took no time, model calls alone have a p95 of about 8.5 s.
The 10% of questions that retry a weak search are the slowest. A paid tier may be faster and steadier, but that hasn't been
measured. Making retries rarer would trade speed against recall, so it needs its own evaluation.

## Load test, simulated model

The real API, workflow, database, security checks and rate limit counting, with each model call replaced by a wait of about
1.5 s and embedding by 0.2 s. Each step ran for 60 seconds, with users waiting 2 to 6 seconds between requests.

| Users | Requests a second | `/ask` p50 ms | `/ask` p95 ms | Failures | Peak memory MB |
|---:|---:|---:|---:|---:|---:|
| 1 | 0.15 | 3,300 | 5,500 | 0 | 174 |
| 5 | 0.64 | 3,800 | 5,800 | 0 | 177 |
| 10 | 1.06 | 3,800 | 16,000 | 0 | 180 |
| 20 | 2.61 | 3,900 | 6,100 | 0 | 190 |
| 40 | 5.21 | 3,800 | 6,100 | 0 | 203 |
| 80 | 6.36 | 8,600 | 13,000 | 7 | 255 |

- **Up to 40 users, the app adds very little.** Above that it saturates: at 80, answers took more than twice as long and 7
  requests failed with dropped connections.
- **The 16 s at 10 users didn't repeat.** Three more runs at 10 users gave p95s of 5.7, 5.8 and 5.9 s. One search took 12.8 s
  in that minute, and the cause wasn't traced.
- **Memory stayed well under the 450 MB target**, at 255 MB, on Windows. It hasn't been measured on Render.

## Load test, real model

| Users | `/ask` requests | `/ask` p50 ms | `/ask` p95 ms | Failures | Peak memory MB |
|---:|---:|---:|---:|---:|---:|
| 1 | 6 | 5,700 | 6,700 | 0 | 193 |
| 3 | 29 | 4,500 | 13,000 | **8** | 200 |

**The real limit on the free tier is the model's rate limit, not the app.** With only 3 people asking about every 10 seconds, 8
of 29 questions failed with Google's rate limit error. Each question uses about 2 model calls, so `RATE_LIMIT_TOTAL_PER_MINUTE`,
30 by default, lets through more than the free tier allows. On the free tier, set it to about half your model's per-minute
limit, so people see "the helpdesk is busy" rather than a failure. The default wasn't changed.

These are small samples, of 6 and 29 questions, so treat them as a sign and not as a measurement.

## Backup and restore

A real backup of the local database was restored into an empty one, using the same commands people would use:

| Measure | Result |
|---|---|
| Size | 25 tables, 5,366 rows, 8.8 MB |
| Backup, and restore with checks | 5.0 s and 4.3 s |
| Row counts after restoring | All match |
| Default organisation's chunks after restoring | 539 |
| Another tenant's view of those chunks | 0 |

Automated tests also restore a database, run the app on it, refuse to overwrite one with tables, and notice missing rows.
**It hasn't been run against Neon.**

## Running app

The API ran with the real model and wrote JSON logs with request ids, and errors had their type and stack, with no message.
Tests check that a question's text never reaches the logs, on success or failure.

**Not checked:** the Usage tab in a browser, Langfuse with a real account, Docker, GitHub Actions, Render or Neon.

## Bugs this phase found

- **Usage counting broke every question on the real model.** LangChain keeps callbacks in a set, and the new class couldn't
  go in one. Every test with a fake model passed, and it was only found on the first real run. It's fixed, with a test.
- **A busy model could hold an answer for 5 minutes.** Model calls now stop after 20 seconds, and retry once. See D35.
- **A busy judge model could stall the evaluation with no error.** It now has a time limit too.
- **Phase 4's report and my summary gave the wrong recall target**: 0.85, which is the quality gate's floor. The target is 0.90.
  It's corrected, and the result still meets it.

## Success targets

| Target | Goal | Result | Met? |
|---|---|---|---|
| Recall@5 | ≥ 0.90 | 0.938 | Yes, provisionally |
| Correct refusals | 100% | 92.3% (24 / 26) | **No** |
| Citation correctness | ≥ 95% | 100% valid, 95.7% on target | Yes, provisionally |
| Cross-tenant leakage in tests | 0 | 0 | Yes, in tests |
| p95 answer latency, warm | < 6 s | 10.78 s, or 14.87 s with retries | **No** |
| API memory on Render | < 450 MB | 255 MB at most, on Windows | Not measured on Render |

**This isn't evidence of production readiness.** The load test and backup ran on one PC, and nothing has been deployed.
