# Running the helpdesk

How to see what the helpdesk is doing, back it up, and get it back if something goes wrong.

## Logs

The API writes one JSON object per line. On Render, they're on the service's **Logs** tab.

```json
{"time":"2026-11-07T12:00:00.123+00:00","level":"INFO","logger":"hr_helpdesk","message":"POST /ask status=200 duration_ms=3412","request_id":"r3f9a1c2b7d0","event":"request","method":"POST","path":"/ask","status":200,"duration_ms":3412,"tenant_id":"default"}
```

- **Follow one request:** every response has an `X-Request-ID` header, and every log line from that request has the same `request_id`. Ask someone reporting a problem for the time, then search for it.
- **Errors** have `error_type` and `error_stack`. They deliberately don't include the error's message, because a model provider's error can quote what it was sent.
- **Questions and answers are never logged.** Personal data in any log line is also masked. Tests check both.
- `LOG_FORMAT=text` is easier to read on your own PC. `LOG_LEVEL` can be `DEBUG`, `INFO`, `WARNING` or `ERROR`.

## Metrics

HR admins see these on the admin screen's **Usage** tab, or at `GET /admin/metrics?days=7`, for their own organisation only:

| Measure | Meaning |
|---|---|
| `requests`, `failures` | Chat answers and decisions, and how many failed |
| `refusals`, `refusal_rate` | Questions it wouldn't answer. **A sudden rise can mean a broken index or a quota problem** |
| `latency_ms` | Total, search and model time, as p50 and p95. Embedding time is p50 only |
| `llm_calls`, `input_tokens`, `output_tokens` | Model use. Embeddings aren't included |
| `estimated_cost_usd` | Uses `LLM_PRICE_INPUT_PER_MILLION` and `LLM_PRICE_OUTPUT_PER_MILLION`, which are 0 by default |
| `tool_calls`, `tool_failures` | Self-service tools. Failures are `failed`, `blocked` or `unknown_tool`, not a business rule saying no |
| `thumbs_up`, `thumbs_down` | Ratings of answers |

Rows hold only counts and times, and are removed after `METRICS_RETENTION_DAYS`, 90 by default. `/health` also reports
`ai: unavailable` if the model failed in the last 5 minutes and hasn't worked since.

Nothing raises an alert yet. On the free stack, the simplest watch is an uptime service checking `/live`.

## Tracing (optional)

To see each model call in [Langfuse](https://langfuse.com):

1. Install it: `pip install -r requirements-tracing.txt`. On Render, add that to the build command.
2. Set `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`. Set `LANGFUSE_BASE_URL` for a self-hosted one or another region.

**This sends prompts, including policy text and masked questions, to Langfuse.** Masking doesn't catch names. If you use
it with real data, it needs the same agreement as any other company handling that data. If Langfuse can't start, tracing
switches off and answers carry on. **This has only been tested with a fake, not a real Langfuse account.**

## Backups

A backup is a **full, unencrypted copy of the data**. Keep it somewhere as private as the database, and never commit it.
`backups/` and `*.dump` are in `.gitignore`.

Nothing runs backups automatically, because the free stack has no background workers. Run this from your PC, with
`DATABASE_URL` set to the hosted database. On Neon, use the direct connection string, not the pooled one.

```bash
venv\Scripts\python -m ops.backup create backups/helpdesk.dump
```

It needs the PostgreSQL client tools, at least as new as the server. Tools newer than the server work: a restore ignores a setting the older server doesn't have, and checks everything else. Set `PG_BIN_DIR` if they aren't on your `PATH`. Your
host may also keep its own history, so check what your plan includes.

**How often:** after any batch of policy changes, and before any upgrade. With sample data, that's enough. With real
data, agree how much loss is acceptable and schedule it, such as with a GitHub Actions workflow or your host's backups.

## Restoring

A restore only goes into an **empty** database, so it can't overwrite anything. It recreates the app's database role,
reapplies its access, checks every tenant table is still protected, and compares row counts with the backup.

1. Create an empty database with the `vector` extension available. On Neon, create a new database or branch.
2. Put its address in an environment variable, then restore:
   ```bash
   set RESTORE_DATABASE_URL=postgresql://...
   ```
   ```bash
   venv\Scripts\python -m ops.backup restore backups/helpdesk.dump --target-env RESTORE_DATABASE_URL
   ```
3. Check it, ideally by running a copy of the API against it, and calling `/health`.
4. Change `DATABASE_URL` on Render to the new database, and redeploy.

Anything added after the backup is lost. Chats waiting for approval when the backup was taken can still be approved
within their 24 hours.

**Tested on:** the local database, with 25 tables and 5,366 rows in an 8.8 MB file. Backup and restore took about 5 seconds
each, and tenant separation still held afterwards. **It hasn't been run against Neon.** Test it there before relying on it.

## When something goes wrong

| What you see | Likely cause | What to do |
|---|---|---|
| Answers fail, `/health` says `ai: unavailable` | Model quota or an outage | Check the log for `error_type`, and the provider's status. Daily quotas reset |
| Many more refusals than usual | Index problem, or a model change | Check `chunks_indexed` on `/health`, then run the evaluation |
| `/health` returns 503 "Database unavailable" | Database asleep or unreachable | Wait a minute on a free tier, then check Neon |
| Everyone gets 429 | The shared limit, which protects the model quota | Change `RATE_LIMIT_TOTAL_PER_MINUTE`, if the quota allows it |
| Some answers fail when a few people use it at once, with `error_type` `GoogleRateLimitError` | The model's per-minute limit. In testing, 3 active people were enough on the free tier | Set `RATE_LIMIT_TOTAL_PER_MINUTE` to about half that limit, as each question uses about 2 calls, or move to a paid tier |
| An indexing job failed | Embedding quota, or a restart | Retry it on the admin screen. It carries on from where it stopped |
| The first start fails with "permission denied" | The database user can't create the app's role | See "The database role" in `DEPLOY.md` |
| Someone can't sign in | Not added, deactivated, or their email isn't verified | Check the **People** tab and **Audit log** |
| A suspected leak or breach | | Suspend the organisation with `PATCH /platform/organisations/{id}`, keep the logs, and follow your breach process. See `docs/DPDP_NOTES.md` |
