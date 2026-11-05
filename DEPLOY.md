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

4. Wait for the deploy to finish, then copy the service's URL, such as `https://hr-policy-assistant-api.onrender.com`.
5. Open that URL with `/health` on the end. You should see:
   ```json
   {"status":"ok","chunks_indexed":539}
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

## If something goes wrong

| What you see | Likely cause | What to do |
|---|---|---|
| `That isn't a connection string` | The paste didn't work, or copied something else | Repeat step 1.3 and paste again |
| `/health` says "No policies indexed" | The chunks weren't copied, or `DATABASE_URL` points to another database | Repeat step 1.3 and compare the connection strings |
| `/health` says "Database unavailable" | Wrong `DATABASE_URL` | Paste it again in Render's **Environment** settings |
| Chat says "Invalid or missing API key" | The keys on Render and Streamlit differ | Make `HELPDESK_API_KEY` identical in both |
| Chat says "The AI service is unavailable" | Gemini quota or key problem | Check Render's logs for the request ID, and try later |
| Stays on **Connecting** | Wrong URL, or the API is asleep | Check `HELPDESK_API_URL`, including `https://`, then wait a minute and ask a question |
| Render stops with an out-of-memory error | The service went over its 512 MB limit | Check Render's logs. The API used about 150 MB when tested on Windows, so this isn't expected |
