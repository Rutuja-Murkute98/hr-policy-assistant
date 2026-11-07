# Phase 1 evaluation report

**Every result here uses draft labels.** All 123 golden questions are `unreviewed`, because the project owner hasn't approved them yet. The numbers can change after that review.

- **Baseline:** the code at commit `c93bac2`, as audited.
- **New:** the code at commit `a836ae7`, using vector + keyword search, weighted fusion, MMR and confidence abstention, with structured answers and checked citations.
- **Answering model:** `gemini-3.5-flash-lite`.
- **Judge:** `gemini-3.1-flash-lite`, a different model from the same vendor.

## Answers

| Measure | Baseline | New | Change |
|---|---:|---:|---|
| Expected facts present (87 questions) | 0.943 | **0.966** | Better |
| Refused when it should (26 questions) | 0.962 | 0.962 | Same |
| Wrongly refused an answerable question | 0.041 | **0.010** | Better |
| Citations valid | 1.000 | 1.000 | Same, but see the note below |
| Citations include a labelled chunk | 0.989 | 0.969 | **Worse**, but see the note below |
| Judge: faithful | 1.000 | 1.000 | Same |
| Judge: correct | 1.000 | 1.000 | Same |
| Median seconds per question | 2.10 | 5.21 | **Worse**, see Latency |
| p95 seconds per question | 3.54 | 19.52 | **Worse**, see Latency |

**The citation rows aren't like for like.** The baseline lists every retrieved chunk as a source, so it's very likely to include a labelled one. The new code lists only what the answer cites, which is a stricter test.

**The judge checks answers only against the excerpts it's given.** It rejected three planted wrong answers and accepted a planted right one, so it can fail answers. It can't notice a contradiction with a policy that wasn't retrieved, so 1.000 doesn't mean every answer is right.

### Remaining failures in the new pipeline

| Question | What happened | Likely cause |
|---|---|---|
| `unanswerable-009` Friday dress code | Answered "casual on Fridays" | **Likely a wrong label.** The Code of Conduct does say this, so the answer appears right and my label wrong. It still counts as a failure until the owner decides |
| `normal-054` When do I sign the NDA? | Refused | A real miss. The answer is in the onboarding guide's Day 1 checklist |
| `conflict-001` Sick leave on probation | Gave 6 days only | The policies disagree: see below |
| `conflict-002` Casual leave on probation | Gave 3 days only | The policies disagree: see below |
| `exception-008` Salary advance during notice | Answer was right, but the check failed | The expected phrase is too strict |

## Retrieval

Measured over 97 answerable questions, of which 96 have labels, using top 5 results. Search time excludes embedding.

| Setting | Recall@5 | Any hit | Precision@5 | MRR | NDCG@5 | Search p50 | Must-refuse reaching the model |
|---|---:|---:|---:|---:|---:|---:|---:|
| Vector only + MMR | 0.923 | 0.959 | 0.258 | 0.890 | 0.878 | 247 ms | 19 / 26 |
| Vector + keyword, weighted + MMR | **0.947** | 0.979 | 0.266 | 0.905 | 0.897 | 438 ms | 19 / 26 |
| Vector + keyword, RRF + MMR | 0.902 | 0.959 | 0.252 | 0.819 | 0.818 | 449 ms | 19 / 26 |
| Vector + keyword, RRF, no MMR | 0.910 | 0.959 | 0.256 | 0.820 | 0.823 | 473 ms | 19 / 26 |
| Vector + keyword, RRF + FlashRank, no MMR | 0.902 | 0.959 | 0.249 | 0.836 | 0.832 | 527 ms | 19 / 26 |
| Vector + keyword, weighted + FlashRank + MMR | 0.942 | **0.990** | 0.262 | **0.920** | 0.904 | 614 ms | 19 / 26 |
| **Chosen default:** weighted + MMR + confidence | **0.947** | 0.979 | 0.266 | 0.905 | 0.897 | 327 ms | 24 / 26 |

Precision@5 can't go much higher here, because most questions have only one or two relevant chunks and five are returned.

**RRF is built but off by default.** The brief's target flow uses RRF, but on this data it scored lower on recall and MRR than weighted fusion. That can be switched with `FUSION_METHOD=rrf`.

## Abstention

Distance limits and confidence scores were compared on the best chunk for each question:

| Gate | Answerable questions blocked | Must-refuse stopped before the model |
|---|---:|---:|
| Distance 0.42 (old) | 0 / 97 | 7 / 26 |
| Distance 0.38 | 0 / 97 | 13 / 26 |
| Confidence 3.0 (new default) | 0 / 97 | 2 / 26 |
| Confidence 3.25 | 1 / 97 | 8 / 26 |
| Confidence 3.5 | 2 / 97 | 14 / 26 |

**The new gate isn't stronger on this model.** It was chosen because the brief wants to replace a limit tied to one embedding model, and because blocking a real question costs more than one extra model call. That it carries over to other embedding models is still an untested assumption, since only one model was available. The model's own `answered: false` does most of the refusing: it accounted for 24 of the 26 refusals in the new run, with the gate stopping the other 2.

The threshold of 3.0 was picked with sight of all the questions. Tuned on the `tune` half alone, 3.25 would have been chosen, and it blocked one real question in the `check` half.

## Latency

- **Full run:** median 5.21 s and p95 19.52 s, including free-tier rate limit retries. The slowest question took 92 s.
- **Paced sample:** 11 questions on the committed code gave a median of 2.29 s, close to the baseline's 2.10 s, with embedding 0.6 s, search 0.3 s and the model 1.35 s.
- **Outliers:** the same sample had spikes of 7.8 s in search and 16 s in the model, and the slowest question took 24.6 s. Other programs were running on this PC at the time, so the cause isn't established.

**The target of p95 under 6 s isn't met or properly measured yet.** It needs the load test in Phase 5, on the real hosting.

## Memory

| Setup | Peak over 40 searches |
|---|---:|
| Default | 178 MB |
| With FlashRank | 274 MB |

Both are under the 450 MB target. They were measured on Windows with no model calls, so Render is still unmeasured.

## Targets

| Target | Goal | Result | Met? |
|---|---|---|---|
| Recall@5 | ≥ 0.90 | 0.947 on draft labels | Yes, provisionally |
| Correct refusals | 100% | 96.2% (25 / 26) | **No.** The one miss looks like a wrong label, which needs owner review |
| Citation correctness | ≥ 95% | 100% valid, 96.9% on target | Yes, provisionally |
| Cross-tenant leakage | 0 | One integration test of the tenant filter passes | Not assessed until Phase 4 |
| p95 latency | < 6 s | 19.52 s in the full run | **No** |
| API memory on Render | < 450 MB | 178 MB on Windows | Not measured on Render |

## Policy contradictions for the owner to decide

| Question | One source | Another source |
|---|---|---|
| Sick leave on probation | Leave Policy: 12 days | Probation Policy: up to 6 days |
| Casual leave on probation | Leave Policy: 6 days | Probation Policy: up to 3 days |
| Referral bonus, non-technical Band 2 | Referral Policy: Rs. 15,000 | Compensation Policy: Rs. 20,000 |
| Time to raise a salary query | Payroll Policy and FAQ: 30 days from the payslip | Compensation Policy: 5 working days from credit |
| ESOP vesting | Summary: 4 years | The section's schedule runs to Year 5 |

## What these runs didn't cover

- RAGAS wasn't rerun on the new pipeline, because the judge replaced it for this comparison.
- FlashRank and Cohere weren't part of the full answer runs. Cohere has only been tested with fakes.
- Answer runs for the baseline were split across two API keys because the first key's daily limit ran out. This doesn't change the method, but timings aren't all from one sitting.
