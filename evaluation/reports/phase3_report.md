# Phase 3 evaluation report

**Every result here uses draft labels.** The 123 golden questions and 58 agent questions are still `unreviewed`.

- **Answering model:** `gemini-3.5-flash-lite`. **Judge:** `gemini-3.1-flash-lite`.
- **Each run is one sample.** The model isn't fully repeatable, so small differences between phases can be chance. For example, the Friday dress code question was answered in Phase 2 and refused here, with no code change aimed at it.

## Golden set

| Measure | Phase 1, pipeline alone | Phase 2, workflow | Phase 3 | Phase 3 vs Phase 2 |
|---|---:|---:|---:|---|
| Recall@5 | 0.947 | 0.938 | **0.943** | Slightly better |
| Expected facts present (87) | 0.966 | 0.931 | **0.943** | Slightly better |
| Refused when it should (26) | 0.962 | 0.885 | **0.962** | Better, but see below |
| Wrongly refused an answerable question | 0.010 | 0.031 | **0.031** | Same |
| Citations valid | 1.000 | 0.989 | **1.000** | Better |
| Citations include a labelled chunk | 0.969 | 0.957 | **0.968** | Slightly better |
| Judge: faithful | 1.000 | 1.000 | **1.000** | Same |
| Judge: correct | 1.000 | 0.969 | **0.979** | Slightly better |
| Median seconds | 5.21 | 4.32 | **3.82** | Better |
| p95 seconds | 19.52 | 8.58 | **8.08** | Better |

**The refusal score flatters this run.** `unanswerable-009`, the Friday dress code, now counts as a correct refusal, but the Code of Conduct does answer it. If you correct that likely wrong label, it becomes a wrong refusal instead, with refusals at 24 of 25 (96.0%) and wrong refusals at 4 of 98 (4.1%).

**The workflow still scores below the pipeline alone** on recall, facts and wrong refusals, though the gap has narrowed.

### Remaining failures

| Question | What happened | Assessment |
|---|---|---|
| `ambiguous-006` "How long do I have to raise it?" | Guessed it meant a grievance | A real miss. Nothing says what "it" is |
| `normal-054` When do I sign the NDA? | Refused | A real miss, in every phase so far |
| `ambiguous-008` "What's the bonus?" | Refused | A real miss, though the question is vague |
| `follow_up-009` "Is it taxable?" | Refused | A real miss |
| `multi_hop-001` Resigning in the first year, ESOP options | Answered, but without "12 months" as worded in the check | Worth a look when reviewing labels |
| `ambiguous-002` "What is the notice period?" | Right, but wrote "2-week" where the check wants "2 weeks" | The check is too strict |
| `exception-008` Salary advance during notice | Right answer, check failed | The check is too strict |
| `conflict-001` and `conflict-002` | Gave only the Probation Policy's figure | Needs your decision on the contradiction |

The suggested question "When is my salary credited?", which the browser check found failing, now answers correctly in the golden run.

## Agent test

Measured on 58 questions. Intent, routing, tool selection, tool success, approval compliance and leave arguments all scored 1.000. The median was 3.34 seconds.

The first run showed 0.983 on approval compliance. That was a false alarm: the check counted a leave request left over from a browser test. The check now compares before and after, and the affected question was rerun.

## Trying the new features for real

A made-up parking policy was used, with real embeddings and the real chat model, and removed afterwards.

| Check | Result |
|---|---|
| Upload as PDF, then replace with DOCX | Both indexed, and the second embedded only the 1 changed section |
| Version history | Version 2 current, version 1 superseded, with original file names |
| Same text under another file name | Refused with a 409, naming the existing policy |
| "How much does employee parking cost?" | Rs. 500, the current version |
| "What did it say before the last update?" | Free of charge, cited and marked as superseded |
| Status messages | "Understanding your question", "Searching policies", "Writing the answer" |
| Thumbs up and down | Shown in the browser. Ratings were saved and summarised through the API, but the thumbs weren't clicked |
| Admin page | The Policies, Upload, Unanswered and Feedback tabs were opened in a browser with real data. The Indexing jobs tab showed nothing, because the test policy's jobs had been purged |

**Not clicked in a browser:** choosing a file and submitting the upload form, because the browser tool can't drive a file picker. The same API route it calls was used for real.

## Memory

| Point | Windows, no model calls |
|---|---:|
| After startup | 168 MB |
| Peak over 40 searches | 178 MB |
| After reading 40 small documents | 223 MB |

This is under the 450 MB target, but a large PDF would use more, and Render is still unmeasured.

## Latency

Median 3.82 s and p95 8.08 s, which still misses the 6 s target. Each policy question makes two model calls, or three if the search is weak. One question took 3 hours 48 minutes because the daily quota ran out during it and retries waited, so it's excluded from the median and p95 by how percentiles work, and from any claim about speed.

## Quality gate

**The CI quality gate passes** on this code. It passes partly because of the dress code result above, so your label review may change that.

## Targets

| Target | Goal | Result | Met? |
|---|---|---|---|
| Recall@5 | ≥ 0.90 | 0.943 on draft labels | Yes, provisionally |
| Correct refusals | 100% | 96.2% (25 / 26) | **No** |
| Citation correctness | ≥ 95% | 100% valid, 96.8% on target | Yes, provisionally |
| Cross-tenant leakage | 0 | Isolation tests pass | Not assessed until Phase 4 |
| p95 latency | < 6 s | 8.08 s | **No** |
| API memory on Render | < 450 MB | 223 MB on Windows | Not measured on Render |

## What these runs didn't cover

- A failed job and retry were tested with fake embeddings, not a real quota failure during an upload.
- The fallback provider and the Cohere reranker were only tested with fakes.
- OCR isn't built, so scanned PDFs are refused with a clear message.
- The run was split across two API keys because the daily limit ran out, so its timings aren't all from one sitting.
