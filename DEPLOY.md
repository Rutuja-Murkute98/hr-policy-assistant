# Deploy guide

This puts the chatbot online for free, using three services:

```mermaid
flowchart LR
    U["Employee's<br>browser"] --> S["Chat screen<br>Streamlit Community Cloud"]
    S -->|"X-API-Key"| R["API<br>Render"]
    R --> N[("Database<br>Neon + pgvector")]
    R --> G["Gemini"]
```

**Allow about 20 minutes.** You'll need your GitHub account, your Gemini key, and your `HELPDESK_API_KEY` from `.env`.

> **Keep these secret:** your Gemini key, `HELPDESK_API_KEY` and database connection string. Only enter them in the places below, and never commit them to GitHub.

Commands below are shown for Windows. On Linux or macOS, use `venv/bin/` in place of `venv\Scripts\`.

## Step 1: Create the database on Neon

1. Sign up at [neon.com](https://neon.com) and create a project. Choose a region close to your users.
2. Open **Connect** and copy the **direct** connection string, with connection pooling turned off. It looks like this, with no `-pooler` in the host name:
   ```
   postgresql://user:password@ep-example-123456.region.aws.neon.tech/neondb?sslmode=require
   ```
3. Copy your indexed chunks to Neon. This uses none of your Gemini quota, and it's safe to run again:
   ```bash
   venv\Scripts\python -m pipeline.copy_to_cloud
   ```
   When asked, paste the connection string and press Enter. It stays hidden and isn't saved.
   You should see `Done: the hosted database has all 539 chunks.`

## Step 2: Deploy the API on Render

1. Sign up at [render.com](https://render.com) and connect your GitHub account.
2. Choose **New > Blueprint** and select the `hr-policy-assistant` repo. Render reads the settings from `render.yaml`.
3. Enter these three values when asked:

   | Name | Value |
   |---|---|
   | `GOOGLE_API_KEY` | Your Gemini key |
   | `HELPDESK_API_KEY` | The same value as in your `.env` |
   | `DATABASE_URL` | The same Neon connection string as in step 1 |

4. Wait for the deploy to finish, then copy the URL shown at the top of your service's page. Render may add extra characters to the name, so always copy it rather than typing it.
5. Open that URL with `/health` on the end. If it doesn't show your chunk count, it isn't your service, so don't use it. You should see:
   ```json
   {"status":"ok","chunks_indexed":539,"ai":"ok"}
   ```

## Step 3: Deploy the chat screen on Streamlit Community Cloud

1. Sign in at [share.streamlit.io](https://share.streamlit.io) with GitHub.
2. Choose **Create app**, then deploy from your GitHub repo:

   | Setting | Value |
   |---|---|
   | Repository | `Rutuja-Murkute98/hr-policy-assistant` |
   | Branch | `main` |
   | Main file path | `ui/streamlit_app.py` |

3. Open **Advanced settings**, choose Python **3.13**, and paste this into **Secrets** with your own values:
   ```toml
   HELPDESK_API_URL = "https://your-service-name.onrender.com"
   HELPDESK_API_KEY = "the_same_value_as_in_your_env"
   ```
   Your Gemini key isn't needed here, because only the API talks to Gemini.
4. Choose **Deploy**, then open your app's link and ask a question.

## What to expect

- **Slow first question:** Render's free API sleeps after 15 minutes without traffic, and Neon after 5 minutes. The first question afterwards can take a minute or two, and the screen shows **Connecting** until the API responds.
- **Automatic updates:** pushing to `main` redeploys the API and the chat screen.
- **Shared quota:** every question uses your Gemini quota, so heavy use can stop answers until it resets. The API allows 10 questions a minute per chat and 30 in total, but those counts reset whenever it restarts.
- **Sample data only:** Gemini's free tier may use submitted data to improve Google's products, so don't use real HR documents.
- **Limits change:** check each service's current free limits when you sign up.

## Updating a database from before Phase 1

Run this once against the hosted database, with `DATABASE_URL` set to its connection string. It adds category, version and effective date to the stored chunks, and records each document's hash. It uses no embedding quota unless a policy has changed:

```bash
venv\Scripts\python -m pipeline.embed_and_store
```

The API creates its new table by itself when it starts.

## About the self-service tools

The chat screen lets anyone choose a demo employee, and the tools run on the mock HR system's **fake** employees. Don't connect real employee data until real sign-in arrives in Phase 4. To turn the tools off, set `SELF_SERVICE_ENABLED=false` on Render.

Each policy question now uses two model calls, or three if the search is weak, so the free daily limit covers fewer questions.

## Optional: the policy admin screen

The admin screen lets HR add, replace or remove policies. It's off until you set a key.

1. In Render's **Environment** settings, add `HELPDESK_ADMIN_KEY` with a long random value that's different from your other keys. If you're creating the service for the first time, Render asks for it in step 2.
2. Open your chat screen's address with `/admin` on the end, and enter that key. It isn't linked from the chat screen.

Uploads stay in the database, so they survive restarts. The next `python -m pipeline.embed_and_store` run against that database keeps any policy with no local file and tells you about it. Use `--prune` only if you want those removed.

## Optional: the Slack bot

The bot runs as its own small program and calls your API. It uses Socket Mode, so it doesn't need a public address, but it does have to be running somewhere for as long as you want it to answer.

1. Create an app at [api.slack.com/apps](https://api.slack.com/apps), then turn on **Socket Mode** and create an app-level token with the `connections:write` scope. This is your `SLACK_APP_TOKEN`.
2. Under **OAuth & Permissions**, add these bot scopes: `app_mentions:read`, `chat:write`, `channels:history`, `groups:history` and `im:history`.
3. Under **Event Subscriptions**, subscribe to the bot events `app_mention` and `message.im`.
4. Under **App Home**, allow people to send messages to the app.
5. Install the app to your workspace and copy the bot token. This is your `SLACK_BOT_TOKEN`.
6. Add both tokens to `.env`, plus `HELPDESK_API_KEY` and `HELPDESK_API_URL` for your API, then run:
   ```bash
   venv\Scripts\pip install -r integrations\requirements.txt
   ```
   ```bash
   venv\Scripts\python -m integrations.slack_bot
   ```
7. Invite the bot to a channel and mention it with a question, or send it a direct message. It replies in a thread, and replies in that thread count as follow-up questions.

## If something goes wrong

| What you see | Likely cause | What to do |
|---|---|---|
| `That isn't a connection string` | The paste didn't work, or copied something else | Repeat step 1.3 and paste again |
| `/health` says "No policies indexed" | The chunks weren't copied, or `DATABASE_URL` points to another database | Repeat step 1.3 and compare the connection strings |
| `/health` says "Database unavailable" | Wrong `DATABASE_URL` | Paste it again in Render's **Environment** settings |
| Admin screen says "Admin is switched off" | `HELPDESK_ADMIN_KEY` isn't set on Render | Add it in Render's **Environment** settings |
| Chat says "Invalid or missing API key" | The keys on Render and Streamlit differ | Make `HELPDESK_API_KEY` identical in both |
| Chat says "The AI service is unavailable" | Gemini quota or key problem | Check Render's logs for the request ID, and try later |
| Stays on **Connecting** | Wrong URL, or the API is asleep | Check `HELPDESK_API_URL`, including `https://`, then wait a minute and ask a question |
| Render stops with an out-of-memory error | The service went over its 512 MB limit | Check Render's logs. The API used about 150 MB when tested on Windows, so this isn't expected |
