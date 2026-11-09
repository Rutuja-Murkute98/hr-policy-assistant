# Phase 8 report: evaluation you can trust

Branch `phase-8-evaluation`, built on `phase-7-prove-it-live`, started 9 November 2026. **Part 1 of 2: the tools are built
and tested.** Part 2 grows the golden and agent sets and drafts questions for new uploads. **The review of the labels, the
biggest task in this phase, is the owner's,** and none of it has happened yet: all 123 golden items and 58 agent items are
still unreviewed.

## What changed

| File | Change | Why |
|---|---|---|
| `evaluation/review.py`, `evaluation/review_app.py` | New | A local screen to approve, reject or correct each label, showing the policy text and what the bot said. It refuses labels the tests would reject |
| `evaluation/candidates.py`, `evaluation/data/candidates.json` | New | Questions from real use wait here until the reviewer promotes and labels them |
| `app/knowledge.py`, `app/admin.py` | New `GET /admin/test-candidates` | Unanswered questions, and answers rated down paired with the question before them. Already masked when stored |
| `ui/pages/admin.py` | Changed | A "Prepare test questions" download on the Feedback tab |
| `evaluation/run_eval.py` | Changed | Judges from any provider (`--judge provider:model`, `EVAL_JUDGES`), all kept side by side, a relevance verdict, `--split` and `--limit`, and results for approved items only |
| `evaluation/metrics.py` | Changed | `statements_cited`, Cohen's kappa and agreement between judges |
| `evaluation/quality_gate.py` | Changed | `--run NAME` checks a fresh run against the floors. A metric with nothing to measure is reported as unmeasured, not as 0 |
| `.github/workflows/nightly-evaluation.yml` | New | The real-model evaluation every night, with the index saved between runs |
| `.github/workflows/ci.yml`, `pyproject.toml`, `requirements-dev.txt` | Changed | Coverage measured, with a floor |
| `docs/DECISIONS.md` | D41 to D43 added, Phase 7's entry renumbered to D40 | D37 was already taken |

The answering code didn't change, so the committed evaluation result still matches and the CI quality gate still passes.

## Tests

| Run | Result |
|---|---|
| Full suite on this PC, final code | **755 passed**, 8 min 31 s |
| Full suite with coverage, run just before the last fixes | 754 passed, 15 min 44 s |
| Coverage of `app`, `pipeline`, `evaluation`, `integrations` and `ops` | **87%** (4,652 statements, 582 not run). CI now fails below 85% |
| ruff, mypy, quality gate | Passed |
| New tests | 21 for the review tool, 11 for judges and scores, 1 for the admin endpoint, 1 for the gate |

## What was tried for real

### The review screen

Opened in a real browser against the real label files, only looked at, nothing clicked: it showed 123 unreviewed items, the
labelled chunk's text, the bot's answer from the last saved run and the judge's verdict. **This found a bug the automated tests
missed:** started with `streamlit run`, the screen couldn't import the evaluation package, because Streamlit only adds the
script's own folder to the import path. It's fixed, and the label files were unchanged afterwards.

A check added with the tool found that every chunk labelled in the golden set still exists.

### The citation score, on saved answers

`statements_cited` was computed from the answers saved by two earlier runs, with no model calls:

| Run | Statements with an inline citation |
|---|---:|
| Password sign-in run, 123 questions | 0.979 |
| Phase 5 run, 123 questions | 1.000 |

Two answers in the sign-in run had no inline citation at all (`normal-040` and `normal-047`). They cited through the structured
field, so every earlier metric passed them. The first version of the score split "Rs. 5,000" as if "Rs." ended a sentence, which
the real answers showed. That's fixed and tested.

### The nightly chain, rehearsed

The workflow's steps were run on this PC: the indexed database was backed up, restored into a new empty PostgreSQL as the cached
index would be, then 4 golden questions were answered through the full workflow with the real model and judged.

| Result | Value |
|---|---|
| Restore | 976 chunk rows, checked against the backup's counts |
| Answers | 4 of 4 answered, recall 1.0, facts 1.0, citations valid 1.0, statements cited 1.0 |
| Judge (Gemini, with the new relevance verdict) | faithful, correct and relevant on all 4 |
| Seconds per answer, p50 / p95 | 20.9 / 23.3 |
| Gate | Failed, correctly: no refusal questions were in 4 items, so refusals weren't measured |

The answers took about five times longer than in Phase 7's check earlier the same day (median 3.8 s), with the same model and
data, so the provider was probably busy. Phase 9's per-step timings are needed to say more.

**Not tried:** a judge from another vendor (no key), the workflow itself on GitHub (it needs the secret, and scheduled runs only
happen on `main`), and the admin screen's download button.

## What the owner must do

1. **Review the labels.** Open the review screen with `venv\Scripts\streamlit run evaluation/review_app.py`. Start with the
   `normal` and `unanswerable` categories, which carry the most weight, and commit the label files when done. The plan's target
   is 90% of items decided.
2. **Settle the open questions** in the Phase 5 report: `adversarial-004`, `unanswerable-009` and the five policy contradictions.
3. **For the nightly run,** add a `GOOGLE_API_KEY` repository secret. It uses about 200 model calls a night from the same free
   quota as the live app, so decide if that's acceptable, or change the schedule in `nightly-evaluation.yml`.
4. **For a judge from another vendor,** add its key as a secret (`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`), and set repository
   variables `EVAL_JUDGES`, such as `anthropic:claude-sonnet-5-5,google:gemini-3.1-flash-lite`, and `EVAL_JUDGE_PACKAGES` with
   the provider's LangChain package at a version you choose.
5. **To use real questions,** click "Prepare test questions" on the admin screen's Feedback tab, download the file, then run
   `venv\Scripts\python -m evaluation.candidates import test_candidates.json`.

## Part 2, next session

- Grow the golden set to 200 items and the agent set to 100, filling multi-hop, conflicting policies, exceptions, follow-ups and
  injection attempts. All new items stay unreviewed.
- Draft test questions for the new sections of an uploaded policy, as candidates for review.
