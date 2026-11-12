# Phase 9 report: speed and quota

Branch `phase-9-speed`, built on `phase-8-evaluation`, 11 and 12 November 2026. Part 1 measured each step and added two
changes that need no new model. Part 2 adds an answer cache, skips the model reading clear first messages, and lets the router
model reword weak searches. **Part 2 is built and tested, but not yet measured with the real model:** Gemini was failing with
"high demand" and timeouts on 12 November. **The p95 target of 6 seconds is not met yet,** and the CI quality gate fails until all
200 golden questions are answered again on the part 2 code.

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
| CI quality gate | **Passes.** All 200 golden questions were answered on the Phase 9 code and judged, then recorded |

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

### All 200 golden questions on the Phase 9 code

| Check | Phase 8 run | Phase 9 run |
|---|---:|---:|
| Recall@5 | 0.925 | 0.941 |
| Expected facts in answers | 0.926 | 0.926 |
| Correct refusals | 1.000 | 0.975 |
| Wrongly refused | 0.044 | 0.037 |
| Citations valid / on a labelled chunk | 1.000 / 0.974 | 1.000 / 0.974 |
| Statements with an inline citation | 0.987 | 0.990 |
| Judge: faithful, correct, relevant | 1.000, 0.980, 1.000 | 0.994, 0.981, 1.000 |
| Seconds per answer, p50 / p95 | 4.72 / 37.66, with retry waits | 4.69 / **12.16** |

- **The one missed refusal is `unanswerable-009`,** the item the Phase 5 report already flagged: the policy appears to answer it,
  so the label is probably wrong. It's waiting for the owner's decision.
- **The p95 of 12.16 s** is the first fair full-set figure, now that retry waits aren't counted. By step, reading the message is
  1.7 s at the median and 6.4 s at p95, and writing the answer 1.7 s and 6.0 s. A weak search is reworded on 22 of 180 searched
  questions, which adds 1.3 s at the median and 5.3 s at p95. Those three are where part 2 has to work.
- Searching was done during reading for most questions: the separate search step is 0 ms at the median.

## Part 2: answer cache, skipping the model for clear first messages, and the router rewording searches

| File | Change | Why |
|---|---|---|
| `app/rag/answer_cache.py`, `migrations/versions/0010_answer_cache.py` | New `answer_cache` table, under Row-Level Security | A repeated first question needs no model call |
| `app/agent/graph.py` | Looks up the cache first. A clear first message with a strong search skips the model reading it. The router model rewords weak searches. Steps whose model call failed are recorded | The two slowest steps after writing the answer |
| `app/workflow.py`, `app/config.py`, `app/metrics.py`, `ui/pages/admin.py` | `CACHE_ANSWERS`, `SKIP_UNDERSTAND_WHEN_CLEAR`, `SKIP_UNDERSTAND_CONFIDENCE`. A cached answer is a step of its own on the Usage tab | Each can be switched off |
| `evaluation/run_eval.py`, `evaluation/run_agent_eval.py` | Cache off unless `--answer-cache`. An answer whose model call fell back is retried, not scored. Settings kept with each report. The agent run can be named | So a re-run measures the model, and a busy model can't pass a fallback off as a decision |
| `evaluation/fast_path_check.py` | Which first messages would skip the model, using real searches and no chat model | To measure the shortcut before turning it on |
| `docs/DECISIONS.md`, `docs/DPDP_NOTES.md` | D48 to D50. Both caches added to the list of stored data | |

### Tests

| Run | Result |
|---|---|
| New tests | 9 workflow tests (cache hit, document change, organisations, what's never cached, switching off, the shortcut, the word check, the router rewording), 3 cache key tests, 1 retry test |
| Full suite with coverage | 783 passed and 2 failed, then the 2 fixed: a cached answer from an earlier test answered their question. All 327 integration tests then passed. Coverage 87.17% |
| ruff, mypy | Passed |
| CI quality gate | **Fails:** the workflow code changed since the last recorded run |

A test caught one real bug while building the word check: `SYSTEM:` didn't match, because a word boundary can't follow a colon.

### The shortcut, measured with real searches

`python -m evaluation.fast_path_check` searches every first message in both sets, with no chat model, and compares with what the
model decided in the recorded runs (`phase9` for the golden set, the Phase 8 agent run).

| Search strength needed | Golden first messages that skip it (of 180) | Different from the model's reading | Agent messages that skip it (of 100) | Different from the model's reading |
|---:|---:|---:|---:|---:|
| 4.0 | 82 | 1 (`unanswerable-023`) | 19 | 0 |
| **4.5, the setting** | **75** | **0** | **16** | **0** |
| 5.0 | 60 | 0 | 14 | 0 |
| 6.0 | 24 | 0 | 3 | 0 |

- 99 golden and 26 agent first messages pass the word check. Every personal and action request in the agent set fails it.
- Two agent messages that skip it are labelled off-topic: "Recommend a good movie for tonight" and "What's the share price of
  Infosys?". The recorded workflow already sent both to the policy path, through the rule that a strong search overrules an
  off-topic label, and the answer model said it couldn't find them. Nothing changes for them.
- Seven golden questions that must be refused would skip it at 4.5. The model read all seven as policy questions in the recorded
  run too, so the answer model has to refuse them either way.
- 4.5 is also the strength at which a search overrules an off-topic label, which is why it's the setting (D49).

### Answer length

Over the 246 answers stored in three days of evaluation runs: 119 output tokens at the median, 200 at p95 and 280 at most, from about
1,450 input tokens. The time to write an answer barely followed its length (correlation 0.04), so `LLM_MAX_TOKENS` and the prompt
are unchanged (D50).

### Not measured yet

- **The router model.** The trial of `gemini-3.1-flash-lite` reading the 100 agent messages, `ROUTER_MODEL=gemini-3.1-flash-lite
  SKIP_UNDERSTAND_WHEN_CLEAR=false python -m evaluation.run_agent_eval --name router`, finished 1 message in about 12 minutes on
  12 November. Of its calls, 8 failed with 503 "high demand" and 6 with 504 timeouts, and the main model was also busy. A first try
  had counted fallbacks as the router's decisions, which led to the retry change above. `ROUTER_MODEL` stays unset.
- **Timings and quality with part 2 on.** The same 20 questions as part 1, then all 200 for the quality gate, wait for the model to
  be usable.

## Phase 9 status

| Item from the plan | Status | Evidence or gap |
|---|---|---|
| Per-step timings in the metrics | Implemented and tested | Stored, summarised and shown on the Usage tab |
| Skip reading the message for simple first messages | Implemented and tested, on by default | Same intent as the model on all 75 golden and 16 agent messages that skip it. Time saved not measured yet |
| Search while the message is read | Implemented and tested | Measured: median 3.77 s to 3.19 s |
| Separate router model | Implemented and tested with a scripted model | It now rewords weak searches too. The `gemini-3.1-flash-lite` trial was stopped by provider errors |
| Question embedding cache | Implemented and tested | Measured warm: median 2.94 s |
| Answer cache for repeated questions | Implemented and tested | Exact wording only, as no similarity was measured safe (D48). Hit time not measured yet |
| Async request path | Deferred, with a reason | Requests already run on a 40-thread pool. The load test decides (D50) |
| Tune output length and prompt size | Measured, no change | Answers are short, and length doesn't predict time (D50) |
| p95 under 6 s on the deployment | **Not met** | 8.97 s on this PC with a warm cache. The deployment hasn't been measured (Phase 7 part 2) |
| 10 people at once with no quota errors | Not measured | Needs the deployment |

## What the owner must do

1. Decide whether `unanswerable-009` should stay a refusal or become an answerable question with the chunk that answers it.
1. Nothing else is needed for part 2's measurements: they need a day when Gemini isn't overloaded, and the free quota.
2. Decide whether a second key or a paid tier is acceptable for a router model, as the plan asks. Part 2 can measure
   `gemini-3.1-flash-lite`, the judge's model, as a router without one.
