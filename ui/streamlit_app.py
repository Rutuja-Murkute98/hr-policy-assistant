"""Stage F: the chat screen. It only talks to the API, and has no RAG code of its own.

Run from the project folder (with the API running):
    venv\\Scripts\\streamlit run ui\\streamlit_app.py
"""
import json
import uuid
from collections.abc import Iterable, Iterator
from pathlib import Path

import requests
import streamlit as st
from ui_settings import api_url, setting, tenant

UI_DIR = Path(__file__).resolve().parent
API_URL = api_url()
API_KEY = setting("HELPDESK_API_KEY")

TENANT = tenant()
COMPANY = TENANT["company_name"]
WELCOME = (
    f"Hello! 👋 Welcome to the **{COMPANY} HR Helpdesk**.\n\n"
    "I can answer questions about our HR policies, including leave, payroll, "
    "work from home, insurance and more. What would you like to know?"
)
SUGGESTIONS = [
    "How many leaves do I get in a year?",
    "What is the notice period?",
    "Can I work from home on probation?",
    "When is my salary credited?",
]
AVATARS = {"assistant": ":material/support_agent:", "user": ":material/person:"}
WAKING_UP = "The helpdesk is waking up or unavailable. Please try again in a minute."
MAX_HISTORY_MESSAGES = 6  # earlier messages sent with each question, for follow-up questions
MAX_MESSAGE_CHARS = 2000  # the longest earlier message the API accepts
TYPING_DOTS = '<div class="typing" role="status" aria-label="Typing"><span></span><span></span><span></span></div>'


@st.cache_data(ttl=30, show_spinner=False)
def api_is_online() -> bool:
    """Check the API at most once every 30 seconds. A sleeping API wakes up from this."""
    try:
        return requests.get(f"{API_URL}/health", timeout=2).ok
    except requests.RequestException:
        return False


def chat_history() -> list[dict]:
    """Return the recent messages, without the welcome and the question being asked now."""
    messages = st.session_state.messages[1:-1]
    return [{"role": message["role"], "content": message["content"][:MAX_MESSAGE_CHARS]}
            for message in messages[-MAX_HISTORY_MESSAGES:]]


def error_message(response: requests.Response) -> str:
    if response.status_code == 422:
        return "Please type a question of 2 to 500 characters."
    try:
        return response.json()["detail"]
    except (ValueError, KeyError):
        return f"Something went wrong (error {response.status_code}). Please try again."


def read_events(lines: Iterable[str]) -> Iterator[tuple[str, dict]]:
    """Turn the lines of a server-sent event stream into (event name, data) pairs."""
    event = ""
    for line in lines:
        if line.startswith("event: "):
            event = line.removeprefix("event: ")
        elif line.startswith("data: "):
            yield event, json.loads(line.removeprefix("data: "))


def stream_answer(question: str, history: list[dict], result: dict) -> Iterator[str]:
    """Yield the answer as the API writes it, or a friendly message if the call fails.

    The sources arrive after the answer, so they're put in result.
    """
    try:
        response = requests.post(
            f"{API_URL}/ask/stream", json={"question": question, "history": history},
            headers={"X-API-Key": API_KEY, "X-Client-Id": st.session_state.client_id},
            timeout=150, stream=True,  # long enough for a free hosted API and database to wake up
        )
        if not response.ok:
            yield error_message(response)
            return
        api_is_online.clear()
        response.encoding = "utf-8"
        for event, data in read_events(response.iter_lines(decode_unicode=True)):
            if event == "token":
                yield data["text"]
            elif event == "done":
                result["sources"] = data["sources"]
            elif event == "error":
                yield data["detail"]
    except requests.RequestException:
        yield WAKING_UP


def show_sources(sources: list[dict]) -> None:
    if sources:
        with st.expander(f"Policies checked ({len(sources)})", icon=":material/menu_book:"):
            for source in sources:
                st.markdown(f"- **{source['policy']}**: {source['section']}")


def queue_question(question: str) -> None:
    st.session_state.queued_question = question


def start_new_chat() -> None:
    st.session_state.messages = [{"role": "assistant", "content": WELCOME, "sources": []}]


st.set_page_config(page_title=f"HR Helpdesk | {COMPANY}", page_icon=":material/support_agent:")
st.markdown(f"<style>{(UI_DIR / 'style.css').read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)

online = api_is_online()
st.markdown(
    f"""<div class="brand-bar">
      <div class="brand-logo" aria-hidden="true">{TENANT['logo_initials']}</div>
      <div class="brand-text">
        <div class="brand-company">{COMPANY}</div>
        <div class="brand-product">{TENANT['product_name']}</div>
      </div>
      <div class="brand-status {'online' if online else ''}">
        <span class="status-dot"></span>{'Online' if online else 'Connecting'}
      </div>
    </div>""",
    unsafe_allow_html=True,
)

if "messages" not in st.session_state:
    start_new_chat()
if "client_id" not in st.session_state:
    st.session_state.client_id = uuid.uuid4().hex  # gives each chat its own rate limit

# A question comes from the box or from a suggested-question button
question = st.chat_input("Ask about leave, payroll, work from home...", max_chars=500)
question = question or st.session_state.pop("queued_question", None)

for message in st.session_state.messages:
    with st.chat_message(message["role"], avatar=AVATARS[message["role"]]):
        st.markdown(message["content"])
        show_sources(message["sources"])

if question:
    st.session_state.messages.append({"role": "user", "content": question, "sources": []})
    with st.chat_message("user", avatar=AVATARS["user"]):
        st.markdown(question)

    with st.chat_message("assistant", avatar=AVATARS["assistant"]):
        history = chat_history()
        typing = st.empty()
        with typing.container():
            st.markdown(TYPING_DOTS, unsafe_allow_html=True)
            if not online:
                st.caption("The helpdesk is waking up. The first question can take a minute or two.")


        def clear_typing_then_stream(tokens: Iterator[str]) -> Iterator[str]:
            for number, token in enumerate(tokens):
                if number == 0:
                    typing.empty()
                yield token


        result = {"sources": []}
        answer = st.write_stream(clear_typing_then_stream(stream_answer(question, history, result)))
        sources = result["sources"]
        show_sources(sources)
    st.session_state.messages.append({"role": "assistant", "content": answer, "sources": sources})
    st.rerun()  # shows the "New chat" button straight away

if len(st.session_state.messages) == 1:
    st.markdown('<div class="suggest-label">Popular questions</div>', unsafe_allow_html=True)
    columns = st.columns(2)
    for index, suggestion in enumerate(SUGGESTIONS):
        columns[index % 2].button(suggestion, key=f"suggestion_{index}", on_click=queue_question,
                                  args=(suggestion,), use_container_width=True)
else:
    st.button("New chat", icon=":material/refresh:", on_click=start_new_chat)

st.markdown(
    '<div class="footer-note">Answers come from company HR policies. For important decisions, '
    f"please confirm with HR at {TENANT['hr_email']}.</div>",
    unsafe_allow_html=True,
)
