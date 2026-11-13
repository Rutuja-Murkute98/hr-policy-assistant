# Phase 9 report: speed and quota

Branch `phase-9-speed`, built on `phase-8-evaluation`, 11 to 13 November 2026. Part 1 measured each step and added two
changes that need no new model. Part 2 adds an answer cache, skips the model reading clear first messages, and lets the router
model reword weak searches. On the same 20 questions, part 2 cut the median from 3.67 s to
2.65 s, and a repeated question takes 0.16 s. On all 200 golden questions the median fell from 4.69 s to 3.12 s, and the
quality gate passes, but 4 of 40 questions that should be refused were answered, against 1 before. A faster router model was
tried and not kept. **The p95 target of 6 seconds is not met:** the slowest answers are the answer model's own delays.

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
| CI quality gate | **Passes.** All 200 golden questions were answered with part 2 on and judged, then recorded as `phase9_part2` |

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

### The router model, measured on the agent set

The 100 agent messages with `gemini-3.1-flash-lite` reading each one, on 12 and 13 November with a new API key:
`ROUTER_MODEL=gemini-3.1-flash-lite SKIP_UNDERSTAND_WHEN_CLEAR=false python -m evaluation.run_agent_eval --name router`.

| Agent set, 100 messages | Recorded run, answer model reads | `gemini-3.1-flash-lite` reads |
|---|---:|---:|
| Intent and routing | 0.95 | 0.96 |
| Tool choice (made by the answer model in both) | 0.944 | 0.926 |
| Tools that ran, approval rules, leave dates | 1.00, 1.00, 1.00 | 1.00, 1.00, 1.00 |
| Reading a message, p50 / p95 | 1.70 / 6.38 s on the golden set | 1.54 / 6.77 s |

- It reads messages as well, but not faster, so `ROUTER_MODEL` stays unset (D51). Its only gain is a quota of its own.
- The first try, on 12 November, finished 1 message in about 12 minutes: 8 calls failed with 503 "high demand" and 6 with 504
  timeouts. It had also counted fallbacks as the router's decisions, which led to the retry change above. The second try had 8
  errors, all retried.

### Timings with part 2 on

The same 20 golden questions as part 1, answered through the full workflow on this PC, one pass right after another on
13 November, without judging. The answer model was slow and failing at times that night, so one pass waited 10 minutes and resumed.

| Pass | Median | p95 | Mean | Reading the message, p50 |
|---|---:|---:|---:|---:|
| Before part 2 (`SKIP_UNDERSTAND_WHEN_CLEAR=false`, no answer cache) | 3.67 s | 17.70 s | 6.02 s | 1,654 ms |
| Part 2 on, answer cache empty | **2.65 s** | **10.98 s** | **3.99 s** | **405 ms** |
| Part 2 on, the same questions again | **0.16 s** | **0.21 s** | 0.16 s | 8 ms |

Quality was identical in all three: recall 0.975, expected facts 1.000, citations valid 1.000, no answerable question refused.

- **The median fell 28% and the mean by a third.** Most of these questions skipped the model reading them, so that step fell from
  1.65 s to 0.4 s at the median.
- **A repeated question takes 0.16 s,** with no model call.
- **The p95 is still the answer model's own spikes,** worse this night than on 11 November: 17.70 s with the old settings.
  Twenty questions on an unstable night make the p95s rough. The medians are the fairer sign.

### All 200 golden questions with part 2 on

Answered through the full workflow on 13 November with the shortcut on and the answer cache off, since a stored answer would
hide what the model does now. Judged by `gemini-3.1-flash-lite`.

By mistake, the last 39 questions were answered twice in parallel. A run stopped to change the API key kept going in the
background, and the restarted run answered the same questions at the same time. Each run kept its own answers and saved all
of them together, so each run's results are complete. Both shared the first 161 answers. The recorded run is the one that
finished last. The other is kept as `answers_phase9_part2_parallel.json`, and shows how much the same questions vary.

| Check | Phase 9 run | Part 2, recorded | Part 2, parallel |
|---|---:|---:|---:|
| Recall@5 | 0.941 | 0.944 | 0.946 |
| Expected facts in answers | 0.926 | 0.932 | 0.932 |
| Correct refusals | 0.975 | **0.900** | 0.925 |
| Wrongly refused | 0.037 | 0.037 | 0.037 |
| Citations valid / on a labelled chunk | 1.000 / 0.974 | 1.000 / 0.974 | 1.000 / 0.974 |
| Statements with an inline citation | 0.990 | 0.984 | 0.983 |
| Judge: faithful, correct, relevant | 0.994, 0.981, 1.000 | 1.000, 0.968, 1.000 | 1.000, 0.962, 1.000 |
| Seconds per answer, p50 / p95 | 4.69 / 12.16 | **3.12 / 12.06** | 3.15 / 10.91 |
| Reading the message, p50 / p95 | 1,695 / 6,377 ms | 1,152 / 4,801 ms | 1,161 / 3,625 ms |
| Writing the answer, p50 / p95 | 1,696 / 6,019 ms | 1,629 / 7,708 ms | 1,610 / 7,510 ms |

- **The median fell by a third, from 4.69 s to 3.12 s,** mostly because reading the message is faster. The p95 barely moved:
  the answer model's own slow replies set it.
- **Four questions that should be refused were answered,** against one before:
  - `unanswerable-009` is the known label problem, answered in both runs.
  - `unanswerable-011` asks for today's share price. The answer gave the ESOP exercise price, and the judge marked it wrong.
    The shortcut skipped the reading step here. That step would have called it out of scope, but the strong search would have
    overruled that, and nothing from it reaches the answer model, so the answer model saw the same input. It refused before and
    answered in both part 2 runs.
  - `ambiguous-006` and `adversarial-014` went through the reading step as before. The answer model answered them this time:
    one gives the grievance time limits, and the other says background check reports are confidential and only HR can see them.
- **One question is not enough to tell chance from a change in behaviour.** The two part 2 runs differ on `adversarial-014`
  alone, so these questions do vary between runs. The gate's floors still pass.

## Phase 9 status

| Item from the plan | Status | Evidence or gap |
|---|---|---|
| Per-step timings in the metrics | Implemented and tested | Stored, summarised and shown on the Usage tab |
| Skip reading the message for simple first messages | Implemented and tested, on by default | Same intent as the model on all 75 golden and 16 agent messages that skip it. Median 3.67 s to 2.65 s on 20 questions |
| Search while the message is read | Implemented and tested | Measured: median 3.77 s to 3.19 s |
| Separate router model | Implemented, measured, not kept | `gemini-3.1-flash-lite`: intent 0.96, but no faster (D51). It also rewords weak searches when set |
| Question embedding cache | Implemented and tested | Measured warm: median 2.94 s |
| Answer cache for repeated questions | Implemented and tested | Exact wording only, as no similarity was measured safe (D48). A repeated question takes 0.16 s |
| Async request path | Deferred, with a reason | Requests already run on a 40-thread pool. The load test decides (D50) |
| Tune output length and prompt size | Measured, no change | Answers are short, and length doesn't predict time (D50) |
| p95 under 6 s on the deployment | **Not met** | 12.06 s on all 200 questions with part 2 on, median 3.12 s, on this PC. The deployment hasn't been measured (Phase 7 part 2) |
| 10 people at once with no quota errors | Not measured | Needs the deployment |

## What the owner must do

1. Decide whether `unanswerable-009` should stay a refusal or become an answerable question with the chunk that answers it.
2. Decide whether a second key or a paid tier is acceptable. No router model is needed (D51), but one 200-question run
   with judging uses more than the free 500 requests a day for the answer model, so long evaluations stop partway.
3. Decide whether 4 of 40 wrongly answered refusal questions is acceptable, or whether answers should be made stricter
   about questions the policies only partly cover, such as today's share price. The gate's floor allows it.
