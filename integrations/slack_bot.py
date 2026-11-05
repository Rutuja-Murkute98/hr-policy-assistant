"""Answer HR questions in Slack, using the same API as the chat screen.

It uses Slack's Socket Mode, so it needs no public address. Setup is in DEPLOY.md. Run it with:
    venv\\Scripts\\python -m integrations.slack_bot
"""
import os
import re
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

MAX_HISTORY_MESSAGES = 6
MAX_MESSAGE_CHARS = 2000
UNAVAILABLE = "Sorry, I can't reach the HR helpdesk right now. Please try again in a minute."
MENTION = re.compile(r"<@[A-Z0-9]+>")


def clean_text(text: str) -> str:
    """Remove the @bot mention from a message."""
    return MENTION.sub("", text or "").strip()


def history_from_thread(messages: list[dict], bot_user_id: str, current_ts: str) -> list[dict]:
    """Turn a Slack thread into chat history, leaving out the message being answered."""
    history = []
    for message in messages:
        if message.get("ts") == current_ts:
            continue
        text = clean_text(message.get("text", ""))
        if text:
            role = "assistant" if message.get("user") == bot_user_id or message.get("bot_id") else "user"
            history.append({"role": role, "content": text[:MAX_MESSAGE_CHARS]})
    return history[-MAX_HISTORY_MESSAGES:]


def ask_api(question: str, history: list[dict], client_id: str) -> str:
    """Return Slack-formatted text for the answer, or a friendly message if the call fails."""
    api_url = os.getenv("HELPDESK_API_URL", "http://127.0.0.1:8000").rstrip("/")
    try:
        response = requests.post(
            f"{api_url}/ask", json={"question": question, "history": history},
            headers={"X-API-Key": os.getenv("HELPDESK_API_KEY", ""), "X-Client-Id": client_id},
            timeout=150,  # long enough for a free hosted API and database to wake up
        )
    except requests.RequestException:
        return UNAVAILABLE
    if response.status_code == 422:
        return "Please ask a question of 2 to 500 characters."
    if response.status_code == 429:
        return "That's a lot of questions at once. Please wait a minute and try again."
    if not response.ok:
        return UNAVAILABLE
    data = response.json()
    return format_reply(data["answer"], data["sources"])


def format_reply(answer: str, sources: list[dict]) -> str:
    if not sources:
        return answer
    checked = "\n".join(f"• {source['policy']}: {source['section']}" for source in sources)
    return f"{answer}\n\n*Policies checked*\n{checked}"


def handle_message(event: dict, say, client, bot_user_id: str, ask=ask_api) -> None:
    """Answer one message, in a thread so that follow-up questions keep their context."""
    if event.get("bot_id") or event.get("subtype"):
        return  # never reply to bots, including this one, or to edits and deletions
    question = clean_text(event.get("text", ""))
    if not question:
        return

    history = []
    if event.get("thread_ts"):
        replies = client.conversations_replies(channel=event["channel"], ts=event["thread_ts"], limit=20)
        history = history_from_thread(replies["messages"], bot_user_id, event["ts"])
    reply = ask(question, history, f"slack-{event.get('user', 'unknown')}")
    say(text=reply, thread_ts=event.get("thread_ts") or event["ts"])


def main() -> None:
    # Imported here so that the tests and the API don't need this package
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    app = App(token=os.environ["SLACK_BOT_TOKEN"])
    bot_user_id = app.client.auth_test()["user_id"]

    @app.event("app_mention")
    def on_mention(event, say, client):
        handle_message(event, say, client, bot_user_id)

    @app.event("message")
    def on_direct_message(event, say, client):
        if event.get("channel_type") == "im":
            handle_message(event, say, client, bot_user_id)

    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()


if __name__ == "__main__":
    main()
