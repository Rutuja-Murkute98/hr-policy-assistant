"""The Slack bot's logic, using fakes. No Slack or API call is made."""
from integrations import slack_bot

BOT = "UBOT"


class FakeClient:
    def __init__(self, messages=()):
        self.messages = list(messages)
        self.calls = []

    def conversations_replies(self, channel, ts, limit):
        self.calls.append((channel, ts))
        return {"messages": self.messages}


class Recorder:
    def __init__(self):
        self.said = []
        self.asked = []

    def say(self, text, thread_ts):
        self.said.append((text, thread_ts))

    def ask(self, question, history, client_id):
        self.asked.append((question, history, client_id))
        return "18 days."


def test_mention_is_removed_and_the_reply_starts_a_thread():
    recorder = Recorder()
    event = {"text": "<@UBOT> How much leave?", "ts": "1.0", "user": "U1", "channel": "C1"}
    slack_bot.handle_message(event, recorder.say, FakeClient(), BOT, ask=recorder.ask)
    assert recorder.asked == [("How much leave?", [], "slack-U1")]
    assert recorder.said == [("18 days.", "1.0")]


def test_thread_becomes_history_for_a_follow_up():
    recorder = Recorder()
    client = FakeClient([
        {"ts": "1.0", "user": "U1", "text": "<@UBOT> How much leave?"},
        {"ts": "1.1", "user": BOT, "text": "18 days."},
        {"ts": "1.2", "user": "U1", "text": "<@UBOT> On probation?"},
    ])
    event = {"text": "<@UBOT> On probation?", "ts": "1.2", "thread_ts": "1.0", "user": "U1", "channel": "C1"}
    slack_bot.handle_message(event, recorder.say, client, BOT, ask=recorder.ask)
    assert client.calls == [("C1", "1.0")]
    assert recorder.asked[0][1] == [{"role": "user", "content": "How much leave?"},
                                    {"role": "assistant", "content": "18 days."}]
    assert recorder.said[0][1] == "1.0"


def test_bots_edits_and_empty_messages_are_ignored():
    recorder = Recorder()
    for event in [{"bot_id": "B1", "text": "hello", "ts": "1"},
                  {"subtype": "message_changed", "text": "hello", "ts": "1"},
                  {"text": "<@UBOT>", "ts": "1"}]:
        slack_bot.handle_message(event, recorder.say, FakeClient(), BOT, ask=recorder.ask)
    assert recorder.said == []


def test_history_is_limited_and_long_messages_are_cut():
    messages = [{"ts": str(number), "user": "U1", "text": "x" * 3000} for number in range(10)]
    history = slack_bot.history_from_thread(messages, BOT, "current")
    assert len(history) == slack_bot.MAX_HISTORY_MESSAGES
    assert len(history[0]["content"]) == slack_bot.MAX_MESSAGE_CHARS


def test_reply_lists_sources_only_when_there_are_some():
    assert slack_bot.format_reply("No.", []) == "No."
    reply = slack_bot.format_reply("18 days.", [{"policy": "Leave Policy", "section": "4.1", "file": "x"}])
    assert "*Policies checked*" in reply
    assert "Leave Policy: 4.1" in reply


class FakeResponse:
    def __init__(self, status, data=None):
        self.status_code, self.data = status, data
        self.ok = status < 400

    def json(self):
        return self.data


def test_api_errors_become_friendly_messages(monkeypatch):
    for status, expected in [(429, "wait a minute"), (422, "2 to 500"), (503, "can't reach")]:
        monkeypatch.setattr(slack_bot.requests, "post", lambda *a, status=status, **k: FakeResponse(status))
        assert expected in slack_bot.ask_api("Leave?", [], "slack-U1")


def test_unreachable_api_gives_a_friendly_message(monkeypatch):
    def fail(*args, **kwargs):
        raise slack_bot.requests.ConnectionError

    monkeypatch.setattr(slack_bot.requests, "post", fail)
    assert slack_bot.ask_api("Leave?", [], "slack-U1") == slack_bot.UNAVAILABLE


def test_api_gets_the_key_client_id_and_history(monkeypatch):
    sent = {}

    def fake_post(url, json, headers, timeout):
        sent.update(url=url, json=json, headers=headers)
        return FakeResponse(200, {"answer": "18 days.", "sources": []})

    monkeypatch.setattr(slack_bot.requests, "post", fake_post)
    monkeypatch.setenv("HELPDESK_API_KEY", "key")
    monkeypatch.setenv("HELPDESK_API_URL", "http://api/")
    assert slack_bot.ask_api("Leave?", [{"role": "user", "content": "Hi"}], "slack-U1") == "18 days."
    assert sent["url"] == "http://api/ask"
    assert sent["headers"] == {"X-API-Key": "key", "X-Client-Id": "slack-U1"}
    assert sent["json"]["history"] == [{"role": "user", "content": "Hi"}]
