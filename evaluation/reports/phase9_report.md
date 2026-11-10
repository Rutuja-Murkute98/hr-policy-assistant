# Phase 9 report: speed and quota

Branch `phase-9-speed`, built on `phase-8-evaluation`, 11 November 2026. **Part 1 of 2: measuring, and the two changes that
need no new model.** Part 2 tries a faster router model, an answer cache for repeated questions, and the async request path.
**The p95 target of 6 seconds is not met yet.**

## What changed

| File | Change | Why |
|---|---|---|
| `app/agent/graph.py` | Every step is timed. A first message is searched while it's being read | To find the slow step, then hide the search inside it |
| `app/rag/embedding_cache.py`, `app/rag/service.py` | New question vector cache | A repeated question needs no embedding call |
| `app/workflow.py`, `app/metrics.py`, `migrations/versions/0009_speed.py` | Step times stored and summarised. New `question_embeddings` table with Row-Level Security | So the Usage tab, and anyone running it, can see where time goes |
| `app/rag/providers.py`, `app/config.py` | `ROUTER_MODEL`, `ROUTER_PROVIDER`, `CACHE_QUESTION_EMBEDDINGS`, `SEARCH_WHILE_UNDERSTANDING` | Each change can be switched off, and a router tried without code changes |
| `evaluation/run_eval.py` | Times only the attempt that worked, and reports step times | The Phase 8 p95 of 37.66 s counted waits between retries |
| `ui/pages/admin.py` | A step table on the Usage tab | Where the time goes, in plain words |
| `docs/DECISIONS.md` | D46 and D47 | |

## Tests

| Run | Result |
|---|---|
| New tests | 6 workflow tests, 5 cache tests, 2 timing tests, 1 screen test |
| Integration tests near the changes | 251 passed, plus the new ones |
| ruff, mypy | Passed |
| CI quality gate | **Fails until all 200 golden questions are run again.** The answering code changed, as it should for this phase. That run needs about 650 model calls, more than a day's free quota, so it's resumed over two days |

## Measured with the real model

The same 20 golden questions (the first 20 of the `check` half), answered three times through the full workflow on this PC, without
judging, one pass after another:

| Pass | Median | p95 | Mean | Reading the message, p50 / p95 | Searching, p50 | Writing, p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|
| Before: both changes off | 3.77 s | 9.22 s | 4.93 s | 1,226 / 6,600 ms | 1,080 ms | 1,484 / 6,776 ms |
| Early search on, empty cache | 3.19 s | 9.41 s | 5.12 s | 1,358 / 6,984 ms | 0 ms | 1,579 / 7,403 ms |
| Early search on, warm cache | **2.94 s** | **8.97 s** | **3.86 s** | 1,322 / 2,038 ms | 0 ms | 1,426 / 6,823 ms |

Quality was identical in all three: recall 0.975, expected facts 1.000, no answerable question refused.

- **Searching cost about a second** per answer, and now costs nothing extra for a first message: it happens while the model reads
  the message. Reading got about 130 ms slower at the median, from waiting for the search to finish.
- **The median fell 22%** with both changes and a warm cache.
- **The p95 didn't move.** The slowest answers are the model's own spikes, up to about 7 seconds for one call, in both the reading
  and the writing step. Gemini was busy again: 7 calls were retried across the passes, and those waits are no longer counted.
- **Twenty questions is a small sample,** run one pass after another while the provider's load changed. The medians are a fair
  sign, the p95s less so.

## Phase 9 status

| Item from the plan | Status | Evidence or gap |
|---|---|---|
| Per-step timings in the metrics | Implemented and tested | Stored, summarised and shown on the Usage tab |
| Skip reading the message for simple first messages | Planned | Part 2. Needs an intent check measured on the agent set first |
| Search while the message is read | Implemented and tested | Measured: median 3.77 s to 3.19 s |
| Separate router model | Implemented, not tested | Setting and wiring tested with a scripted model. No faster model measured yet |
| Question embedding cache | Implemented and tested | Measured warm: median 2.94 s |
| Answer cache for repeated questions | Planned | Part 2 |
| Async request path | Planned | Part 2 |
| Tune output length and prompt size | Planned | Part 2 |
| p95 under 6 s on the deployment | **Not met** | 8.97 s on this PC with a warm cache. The deployment hasn't been measured (Phase 7 part 2) |
| 10 people at once with no quota errors | Not measured | Needs the deployment |

## What the owner must do

1. **Run all 200 golden questions again** so the quality gate passes, over two days because of the free quota:
   `venv\Scripts\python -m evaluation.run_eval answers --name phase9 --workflow`, then the same command the next day to finish,
   then `venv\Scripts\python -m evaluation.quality_gate --record phase9`. Or I can do this in the next session.
2. Decide whether a second key or a paid tier is acceptable for a router model, as the plan asks. Part 2 can measure
   `gemini-3.1-flash-lite`, the judge's model, as a router without one.
