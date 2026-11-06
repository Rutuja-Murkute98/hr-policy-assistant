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
from chat_format import error_html, link_citations, sources_html, usable_history
from sign_in import bearer_headers, require_sign_in
from ui_settings import api_url, demo_employees, setting, tenant

UI_DIR = Path(__file__).resolve().parent
API_URL = api_url()
API_KEY = setting("HELPDESK_API_KEY")

SESSION = require_sign_in(API_URL)  # None in demo mode, where the tenant file says which company this is
TENANT = SESSION["organisation"] if SESSION else tenant()
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
# The dots show at once. The notes fade in after 5 and 20 seconds, using only CSS, because
# this script can't update the screen while it waits for the API's first word.
TYPING_DOTS = (
    '<div class="typing-wrap" role="status" aria-label="Checking the policies">'
    '<div class="typing" aria-hidden="true"><span></span><span></span><span></span></div>'
    '<div class="wait-notes" aria-hidden="true">'
    '<span class="wait-note wait-note-first">Still checking the policies...</span>'
    '<span class="wait-note wait-note-second">This is taking longer than usual. Thanks for waiting.</span>'
    "</div></div>"
)
STATUS_LABELS = {"online": "Online", "limited": "Service issue", "connecting": "Connecting"}
STATUS_CHECK_SECONDS = 5  # how often to check again while the API can't be reached


@st.cache_data(ttl=30, show_spinner=False)
def _fetch_status() -> str:
    try:
        response = requests.get(f"{API_URL}/health", timeout=2)
        if not response.ok:
            return "connecting"
        return "limited" if response.json().get("ai") == "unavailable" else "online"
    except (requests.RequestException, ValueError):
        return "connecting"


def api_status() -> str:
    """Return "online", "limited" (answers are failing) or "connecting".

    A good result is reused for 30 seconds. A sleeping API wakes up from this check,
    and "connecting" isn't reused, so the badge updates as soon as the API is back.
    """
    status = _fetch_status()
    if status == "connecting":
        _fetch_status.clear()
    return status


def show_header(shown_status: str) -> None:
    """Draw the company header, and redraw the page if the status changes."""
    status = api_status()
    if status != shown_status:
        st.rerun()
    st.markdown(
        f"""<div class="brand-bar">
          <div class="brand-logo" aria-hidden="true">{TENANT['logo_initials']}</div>
          <div class="brand-text">
            <div class="brand-company">{COMPANY}</div>
            <div class="brand-product">{TENANT['product_name']}</div>
          </div>
          <div class="brand-status {status}" role="status">
            <span class="status-dot"></span>{STATUS_LABELS[status]}
          </div>
        </div>""",
        unsafe_allow_html=True,
    )


def chat_history() -> list[dict]:
    """Return the recent messages, without the welcome, failed replies or the question being asked now."""
    messages = usable_history(st.session_state.messages[1:-1])
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


def api_headers() -> dict[str, str]:
    if SESSION:  # the API works out who this is from the token, so nothing else is sent
        return {**bearer_headers(st.user.tokens["id"]), "X-Client-Id": st.session_state.client_id}
    headers = {"X-API-Key": API_KEY, "X-Client-Id": st.session_state.client_id}
    if st.session_state.get("employee_id"):
        headers["X-User-Id"] = st.session_state.employee_id
    return headers


def decide_action(action_id: str, approve: bool) -> None:
    """Send the employee's decision, and show what happened."""
    try:
        response = requests.post(f"{API_URL}/actions/{action_id}", json={"approve": approve},
                                 headers=api_headers(), timeout=150)
    except requests.RequestException:
        st.session_state.messages.append({"role": "assistant", "content": WAKING_UP, "sources": [], "error": True})
        return
    st.session_state.pending_action = None
    content = response.json()["answer"] if response.ok else error_message(response)
    st.session_state.messages.append({"role": "assistant", "content": content, "sources": []})


def send_feedback(message_id: int) -> None:
    """Save a thumbs up or down. A rating shouldn't get in the way, so a failure is only a small note."""
    choice = st.session_state.get(f"rating_{message_id}")
    if choice is None:
        return
    try:
        response = requests.post(f"{API_URL}/feedback", json={"message_id": message_id, "rating": 1 if choice else -1},
                                 headers=api_headers(), timeout=30)
        if not response.ok:
            st.toast("Your rating couldn't be saved.")
    except requests.RequestException:
        st.toast("Your rating couldn't be saved.")


def change_employee() -> None:
    start_new_chat()  # one employee must never continue another's chat


def stream_answer(question: str, history: list[dict], result: dict) -> Iterator[str]:
    """Yield the answer as the API writes it.

    The sources arrive after the answer, so they're put in result. So is the message
    to show if the call fails, so that it can be shown differently from an answer.
    """
    try:
        response = requests.post(
            f"{API_URL}/ask/stream",
            json={"question": question, "history": history, "conversation_id": st.session_state.conversation_id},
            headers=api_headers(),
            timeout=150, stream=True,  # long enough for a free hosted API and database to wake up
        )
        if not response.ok:
            result["error"] = error_message(response)
            return
        response.encoding = "utf-8"
        for event, data in read_events(response.iter_lines(decode_unicode=True)):
            if event == "token":
                yield data["text"]
            elif event == "status":
                result["on_status"](data["text"])
            elif event == "done":
                result["sources"] = data["sources"]
                result["answer"] = data.get("answer")
                result["pending_action"] = data.get("pending_action")
                result["message_id"] = data.get("message_id")
                st.session_state.conversation_id = data.get("conversation_id")
            elif event == "error":
                result["error"] = data["detail"]
    except requests.RequestException:
        result["error"] = WAKING_UP


def show_message(index: int, message: dict) -> None:
    with st.chat_message(message["role"], avatar=AVATARS[message["role"]]):
        if message.get("error"):
            st.markdown(error_html(message["content"]), unsafe_allow_html=True)
            return
        st.markdown(link_citations(message["content"], message["sources"], index))
        if message["sources"]:
            st.markdown(sources_html(message["sources"], index), unsafe_allow_html=True)
        if message.get("message_id"):
            st.feedback("thumbs", key=f"rating_{message['message_id']}", on_change=send_feedback,
                        args=(message["message_id"],))


def queue_question(question: str) -> None:
    st.session_state.queued_question = question


def try_again() -> None:
    """Ask the last question again, removing the failed attempt."""
    st.session_state.messages.pop()
    queue_question(st.session_state.messages.pop()["content"])


def start_new_chat() -> None:
    st.session_state.messages = [{"role": "assistant", "content": WELCOME, "sources": []}]
    st.session_state.conversation_id = None
    st.session_state.pending_action = None


st.set_page_config(page_title=f"HR Helpdesk | {COMPANY}", page_icon=":material/support_agent:")
st.markdown(f"<style>{(UI_DIR / 'style.css').read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)

page_status = api_status()
# Only while the API can't be reached, the header checks again by itself. Otherwise an
# open tab would keep waking the hosted API and database.
st.fragment(show_header, run_every=STATUS_CHECK_SECONDS if page_status == "connecting" else None)(page_status)

if "messages" not in st.session_state:
    start_new_chat()
if "client_id" not in st.session_state:
    st.session_state.client_id = uuid.uuid4().hex  # gives each chat its own rate limit

employees = {} if SESSION else demo_employees()
if SESSION:
    st.button(f"Sign out ({SESSION['email']})", icon=":material/logout:", on_click=st.logout)
if employees:
    st.selectbox("Demo employee (fake data, until real sign-in)", list(employees), format_func=employees.get,
                 key="employee_id", on_change=change_employee)

# A question comes from the box or from a suggested-question button
question = st.chat_input("Ask about leave, payroll, work from home...", max_chars=500)
question = question or st.session_state.pop("queued_question", None)

for message_index, past_message in enumerate(st.session_state.messages):
    show_message(message_index, past_message)

if question:
    st.session_state.messages.append({"role": "user", "content": question, "sources": []})
    with st.chat_message("user", avatar=AVATARS["user"]):
        st.markdown(question)

    with st.chat_message("assistant", avatar=AVATARS["assistant"]):
        history = chat_history()
        typing = st.empty()
        with typing.container():
            st.markdown(TYPING_DOTS, unsafe_allow_html=True)
            if page_status == "connecting":
                st.caption("The helpdesk is waking up. The first question can take a minute or two.")


        def clear_typing_then_stream(tokens: Iterator[str]) -> Iterator[str]:
            for number, token in enumerate(tokens):
                if number == 0:
                    typing.empty()
                yield token


        def show_status(text: str) -> None:
            with typing.container():
                st.markdown(TYPING_DOTS, unsafe_allow_html=True)
                st.caption(text)


        result = {"sources": [], "on_status": show_status}
        streamed = st.write_stream(clear_typing_then_stream(stream_answer(question, history, result)))
    if "error" in result:
        failed = {"role": "assistant", "content": result["error"], "sources": [], "error": True}
        st.session_state.messages.append(failed)
    else:
        answer = result.get("answer") or streamed  # the API's final, checked text wins
        st.session_state.messages.append({"role": "assistant", "content": answer, "sources": result["sources"],
                                          "message_id": result.get("message_id")})
        st.session_state.pending_action = result.get("pending_action")
    _fetch_status.clear()  # the badge should reflect how this question went
    st.rerun()  # shows the sources, links and buttons straight away

pending = st.session_state.get("pending_action")
if pending:
    with st.container(horizontal=True):
        st.button("Approve", icon=":material/check:", type="primary", key="approve_action",
                  on_click=decide_action, args=(pending["id"], True))
        st.button("Decline", icon=":material/close:", key="decline_action",
                  on_click=decide_action, args=(pending["id"], False))
elif st.session_state.messages[-1].get("error"):
    with st.container(horizontal=True):
        st.button("Try again", icon=":material/replay:", type="primary", on_click=try_again)
        st.button("New chat", icon=":material/refresh:", on_click=start_new_chat)
elif len(st.session_state.messages) == 1:
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
