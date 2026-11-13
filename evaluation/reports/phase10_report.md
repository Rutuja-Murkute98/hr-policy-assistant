# Phase 10 report: settings per organisation, changed while running

Branch `phase-10-settings`, built on `phase-9-speed`, 13 November 2026. **Part 1 is built and tested.** Each organisation
can now change its own branding, categories, answer model, search thresholds, wording and switches on the admin screen,
and the next question uses them, with no restart. Every change is checked, stored with Row-Level Security, and written to
the audit log. With nothing changed, answers are made exactly as before. Part 2 has the model suggest a category for each
upload, and re-indexes when a setting needs it.

## Baseline

The plan asks each phase to start from a fresh evaluation. The `phase9_part2` run was recorded on exactly the code this
phase started from, so the quality gate's fingerprint matched, and it is the baseline: recall@5 0.944, expected facts 0.932,
correct refusals 0.900, median 3.12 s. Repeating it would have used a day of the free model quota for the same result.

## What changed

| File | Change | Why |
|---|---|---|
| `migrations/versions/0011_tenant_settings.py` | New `tenant_settings` table, one row per organisation, with Row-Level Security | Settings live in the database, not the environment |
| `app/org_settings.py` | What an organisation can change, `setting()` to read any of them, and a store that rereads every 10 seconds and clears on a change | One place decides each value: the organisation's, else the environment's, else the default |
| `app/rag/retrieval.py`, `app/rag/service.py`, `app/agent/graph.py` | Thresholds, number of excerpts and switches come from the organisation | So two organisations can search and answer differently |
| `app/rag/prompt.py`, `app/agent/prompts.py` | Tone, answer length, refusal wording and categories come from the organisation | With the default tone and length, the prompt is identical, checked byte for byte |
| `app/rag/providers.py`, `app/workflow.py` | An organisation can pick another of the deployment's models, and a temperature. Its workflow is rebuilt when its settings change | Models are shared between organisations that pick the same one |
| `app/admin.py` | `GET` and `PATCH /admin/settings`. `null` puts a setting back. Each change is audited with its old and new value | |
| `app/accounts.py`, `app/knowledge.py` | Sign-in returns the organisation's own branding and categories, but never its model or thresholds. Uploads check its own categories | |
| `ui/pages/admin.py` | A Settings tab: the values answers use now, what was changed here, Save, and put back | |
| `app/config.py`, `.env.example` | `ALLOWED_CHAT_MODELS` and `SETTINGS_CACHE_SECONDS` | The owner decides which models an organisation may choose |
| `docs/DECISIONS.md`, `docs/DPDP_NOTES.md` | D52, and the new table | |

### What an organisation can and can't change

- **Can:** company name, legal name, product name, logo initials and HR email; its list of categories and the default; the
  answer model, from `ALLOWED_CHAT_MODELS`, and its temperature; the five evidence thresholds and the number of excerpts; the
  refusal wording, with `{hr_email}` and `{company_name}` filled in; plain, friendly or formal tone; short, normal or detailed
  answers; and the switches for self-service, the answer cache and skipping the model on clear first questions.
- **Can't:** API keys, the model provider, the embedding model, rate limits, retention and sign-in settings. They cost money,
  protect the shared quota, need legal sign-off, or need a re-index (part 2). D52 explains each.

## Tests

| Run | Result |
|---|---|
| New tests | 35 unit tests for the settings, checks, prompts, search and model choice. 19 tests on real PostgreSQL through the API, with Acme and Globex. 4 screen tests |
| What they prove | One organisation's settings never reach another, in the API and in the database itself. Invalid values are refused with a readable message and nothing is saved. A change is used by the next answer, and one made by another process is noticed within the cache time. The audit log records who changed what, from what, to what. Two organisations get different branding, models and thresholds at once |
| Isolation tests | The new table was added to the tests that check every tenant table has Row-Level Security and shows nothing without a tenant |
| Full suite with coverage | 844 passed. Coverage 87.70%, up from 87.17%. `app/org_settings.py` 99%, `app/admin.py` 97% |
| ruff, mypy | Passed. This also fixes an import order in `tests/integration/test_workflow.py` that came with Phase 9, which CI's lint step would have refused |
| CI quality gate | **Fails until a new 200-question run is recorded:** the workflow, prompt and search files changed. The default prompt and thresholds are unchanged, so the run should match the baseline, but it hasn't been run |

### Bugs the tests caught

- The organisation's refusal wording was read before checking it was set, so a profile without a company name failed.
- Two of my own tests asked the same question twice and got the cached answer, which is the cache working as designed.

## Not done, or not checked

- **Not run against the live local API.** I tried to create a local test HR admin account to drive the real server, and the
  session's permission check refused it. The same routes run against real PostgreSQL in the integration tests.
- **In demo mode, the screens read branding from `config/tenant.toml`,** as there is only one organisation and no sign-in.
  Answers and the API use the organisation's settings in every mode.
- **The chat screen shows new branding at the next sign-in,** because it reads the organisation once, when someone signs in.
- **Changing the categories doesn't re-file policies already uploaded.** New uploads use the new list.
- **No measurement of a changed setting's effect on quality.** The Settings tab says so beside the thresholds: run the evaluation
  after changing them.

## Phase 10 status

| Item from the plan | Status | Evidence or gap |
|---|---|---|
| `tenant_settings` table with Row-Level Security, holding a validated model | Implemented and tested | Migration 0011, isolation tests |
| One function with fallback to the environment and defaults, and a short cache cleared on change | Implemented and tested | `setting()`, `SettingsStore`, a test of a change from another process |
| Settings tab with validation and an audit entry per change | Implemented and tested | API tests and 4 screen tests. Not opened in a browser |
| `tenant.toml` kept as the seed for a new organisation, imported once | Already true | `Organisations.ensure` adds it only if missing |
| The model suggests a category for each upload, HR confirms | Planned | Part 2 |
| A setting that needs re-indexing starts a re-index job | Planned | Part 2. The embedding model stays a deployment setting until then |
| Two organisations with different branding, models and thresholds, and isolation tests passing | Implemented and tested | On real PostgreSQL with scripted models. Not shown on a deployment |

## What the owner must do

1. Approve a 200-question run so the quality gate passes. It needs more than one day of the free answer-model quota.
2. Set `ALLOWED_CHAT_MODELS` if organisations should be able to choose another model, and decide which.
3. If you want the live check on your PC, create an HR admin account with a password yourself, sign in at `/admin` and
   open the Settings tab. I couldn't create one.
