"""Stage F: the chat screen. It only talks to the API, and has no RAG code of its own.

Run from the project folder (with the API running):
    venv\\Scripts\\streamlit run ui\\streamlit_app.py
"""
import base64
import html
import json
import re
import uuid
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path

import requests
import streamlit as st
from chat_format import error_html, link_citations, sources_html, usable_history
from photos import photo_bytes, square_photo
from sign_in import auth_token, bearer_headers, require_sign_in, sign_out
from ui_settings import api_url, demo_employees, setting, tenant

UI_DIR = Path(__file__).resolve().parent
API_URL = api_url()
API_KEY = setting("HELPDESK_API_KEY")

SESSION = require_sign_in(API_URL)  # None in demo mode, where the tenant file says which company this is
TENANT = SESSION["organisation"] if SESSION else tenant()
COMPANY = TENANT["company_name"]
# Only letters and the like, because a name someone typed goes into Markdown here
FIRST_NAME = re.sub(r"[^\w'-]", "", ((SESSION or {}).get("display_name") or "").split(" ")[0])
WELCOME = (
    f"Hello{f', {FIRST_NAME}' if FIRST_NAME else ''}! 👋 Welcome to the **{COMPANY} HR Helpdesk**.\n\n"
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
MESSAGES_HEIGHT = 480  # turns on scrolling for the messages. The stylesheet then fits them to the window
MAX_BACK_STEPS = 20
STORED_CITATION = re.compile(r"\s*\[\d+(?:,\s*\d+)*\]")


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
        return {**bearer_headers(auth_token()), "X-Client-Id": st.session_state.client_id}
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
    st.session_state.back_stack = []
    st.session_state.chat_view = "chat"
    for key in ("save_history", "retention_days", "save_history_toggle", "profile"):
        st.session_state.pop(key, None)


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
    with st.chat_message(message["role"], avatar=user_avatar() if message["role"] == "user" else AVATARS["assistant"]):
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


# Moving between the chat, History and Settings, with Back returning to wherever you were

def remember_place() -> None:
    st.session_state.back_stack = [*st.session_state.get("back_stack", []), {
        "view": st.session_state.get("chat_view", "chat"), "messages": st.session_state.messages,
        "conversation_id": st.session_state.conversation_id, "pending_action": st.session_state.pending_action,
    }][-MAX_BACK_STEPS:]


def go_back() -> None:
    if not st.session_state.get("back_stack"):
        st.session_state.chat_view = "chat"
        return
    place = st.session_state.back_stack.pop()
    st.session_state.chat_view = place["view"]
    st.session_state.messages = place["messages"]
    st.session_state.conversation_id = place["conversation_id"]
    st.session_state.pending_action = place["pending_action"]


def open_view(view: str) -> None:
    if st.session_state.get("chat_view", "chat") != view:
        remember_place()
        st.session_state.chat_view = view


def open_new_chat() -> None:
    is_fresh = st.session_state.get("chat_view", "chat") == "chat" and len(st.session_state.messages) == 1
    if not is_fresh:
        remember_place()
    start_new_chat()
    st.session_state.chat_view = "chat"


def forget_chat(conversation_id: str | None = None) -> None:
    """Drop a deleted chat from Back and from the screen. With no id, that's every saved chat."""
    def is_gone(place_id: str | None) -> bool:
        return bool(place_id) and conversation_id in (None, place_id)

    st.session_state.back_stack = [place for place in st.session_state.get("back_stack", [])
                                   if not is_gone(place["conversation_id"])]
    if is_gone(st.session_state.conversation_id):
        start_new_chat()


def call_api(method: str, path: str, **kwargs) -> tuple[object | None, str]:
    """Return (data, ""), or (None, a message for the person)."""
    try:
        response = requests.request(method, f"{API_URL}{path}", headers=api_headers(), timeout=30, **kwargs)
    except requests.RequestException:
        return None, WAKING_UP
    if not response.ok:
        return None, error_message(response)
    try:
        return response.json(), ""
    except ValueError:
        return None, f"Something went wrong (error {response.status_code}). Please try again."


def short_date(moment: str) -> str:
    return datetime.fromisoformat(moment).astimezone().strftime("%d %b %Y")


def open_saved_chat(conversation_id: str, updated_at: str) -> None:
    stored, problem = call_api("GET", f"/conversations/{conversation_id}/messages")
    if problem:
        st.toast(problem)
        return
    remember_place()
    # Sources aren't stored with a message, so the numbers that pointed to them would lead nowhere
    earlier = [{"role": message["role"], "content": STORED_CITATION.sub("", message["content"]), "sources": []}
               for message in stored]
    opening = f"Here's your chat from {short_date(updated_at)}. You can carry on from here."
    st.session_state.messages = [{"role": "assistant", "content": opening, "sources": []}, *earlier]
    st.session_state.conversation_id = conversation_id
    st.session_state.pending_action = None
    st.session_state.chat_view = "chat"


def delete_saved_chat(conversation_id: str) -> None:
    _, problem = call_api("DELETE", f"/conversations/{conversation_id}")
    if problem:
        st.toast(problem)
    else:
        forget_chat(conversation_id)
        st.toast("Chat deleted.")


def delete_all_chats() -> None:
    _, problem = call_api("DELETE", "/conversations")
    if problem:
        st.toast(problem)
    else:
        forget_chat()
        st.session_state.confirm_delete_all = False
        st.toast("All your saved chats were deleted.")


# The person's profile: their name, job details and photo

def load_profile() -> dict:
    """Asked for once per visit. Without sign-in there's no profile, and a failure just means no photo."""
    if "profile" not in st.session_state:
        found, problem = call_api("GET", "/me/profile") if SESSION else ({}, "")
        st.session_state.profile = found if isinstance(found, dict) and not problem else {}
    return st.session_state.profile


def initials(profile: dict) -> str:
    name = profile.get("display_name") or (SESSION or {}).get("display_name") or (SESSION or {}).get("email", "")
    letters = [part[0] for part in re.findall(r"[^\W\d_]+", name)[:2]]
    return "".join(letters).upper() or "?"


def user_avatar():
    return photo_bytes(load_profile()) or AVATARS["user"]


def save_profile(fields: dict) -> None:
    saved, problem = call_api("PUT", "/me/profile", json=fields)
    if problem:
        st.session_state.profile_problem = problem
        return
    st.session_state.profile = saved
    if SESSION:
        SESSION["display_name"] = saved["display_name"]  # the greeting uses the new name from now on
    st.toast("Your profile was saved.")


def save_photo(data: bytes) -> None:
    saved, problem = call_api("PUT", "/me/profile/photo", files={"photo": ("photo.jpg", data, "image/jpeg")})
    if problem:
        st.session_state.profile_problem = problem
    else:
        st.session_state.profile = saved
        st.toast("Your photo was saved.")


def remove_photo() -> None:
    saved, problem = call_api("DELETE", "/me/profile/photo")
    if problem:
        st.session_state.profile_problem = problem
    else:
        st.session_state.profile = saved


def show_profile() -> None:
    st.markdown('<div class="panel-title">Your profile</div>', unsafe_allow_html=True)
    if not SESSION:
        st.caption("Profiles need you to be signed in.")
        return
    if "profile_problem" in st.session_state:
        st.error(st.session_state.pop("profile_problem"))
    profile = load_profile()
    if not profile:
        st.error(WAKING_UP)
        return
    current = photo_bytes(profile)
    with st.container(horizontal=True, key="profile_head", vertical_alignment="center"):
        if current:
            st.image(current, width=88)
        else:
            st.markdown(f'<div class="profile-initials">{html.escape(initials(profile))}</div>',
                        unsafe_allow_html=True)
        st.markdown(f'<div class="profile-name">{html.escape(profile["display_name"] or "Your name")}</div>'
                    f'<div class="panel-note">{html.escape(profile["email"])}</div>', unsafe_allow_html=True)

    st.markdown('<div class="panel-subtitle">Profile photo</div>', unsafe_allow_html=True)
    upload = st.file_uploader("Choose a photo", type=["png", "jpg", "jpeg", "webp"], max_upload_size=10,
                              key="photo_upload", help="It's shown at the top of the chat and next to your messages")
    if upload is not None:
        shrunk = square_photo(upload.getvalue())
        if shrunk is None:
            st.error("That file isn't a photo we can use. Please choose a PNG, JPEG or WebP picture.")
        else:
            with st.container(horizontal=True, vertical_alignment="center"):
                st.image(shrunk, width=64)
                st.button("Use this photo", icon=":material/check:", type="primary", key="use_photo",
                          on_click=save_photo, args=(shrunk,))
    if current:
        st.button("Remove photo", icon=":material/delete:", key="remove_photo", on_click=remove_photo)

    st.markdown('<div class="panel-subtitle">About you</div>', unsafe_allow_html=True)
    with st.form("profile_form", border=False):
        name = st.text_input("Full name", value=profile["display_name"], max_chars=100, autocomplete="name")
        job_title = st.text_input("Job title", value=profile["job_title"], max_chars=100,
                                  placeholder="Such as Software Engineer")
        department = st.text_input("Department", value=profile["department"], max_chars=100,
                                   placeholder="Such as Engineering")
        location = st.text_input("Work location", value=profile["location"], max_chars=100,
                                 placeholder="Such as Pune office")
        st.text_input("Work email", value=profile["email"], disabled=True,
                      help="Your email signs you in, so it can't be changed here")
        if st.form_submit_button("Save profile", type="primary"):
            if not name.strip():
                st.error("Please enter your name.")
            else:
                save_profile({"display_name": name.strip(), "job_title": job_title.strip(),
                              "department": department.strip(), "location": location.strip()})
                st.rerun()


def history_is_saved() -> bool:
    """This person's choice, asked for once. If the API can't say, assume the usual setting."""
    if "save_history" not in st.session_state:
        settings, problem = call_api("GET", "/me/settings")
        if problem:
            return True
        st.session_state.save_history = settings["save_chat_history"]
        st.session_state.retention_days = settings["retention_days"]
    return st.session_state.save_history


def change_saving() -> None:
    wanted = st.session_state.save_history_toggle
    settings, problem = call_api("PATCH", "/me/settings", json={"save_chat_history": wanted})
    if problem:
        st.session_state.save_history_toggle = not wanted
        st.toast(problem)
    else:
        st.session_state.save_history = settings["save_chat_history"]


def show_history() -> None:
    st.markdown('<div class="panel-title">Your chats</div>', unsafe_allow_html=True)
    st.button("New chat", icon=":material/add:", on_click=open_new_chat, key="history_new_chat")
    chats, problem = call_api("GET", "/conversations")
    if problem:
        st.error(problem)
        return
    if not chats:
        st.caption("No saved chats yet." if history_is_saved() else
                   "No saved chats. Saving is turned off in Settings, so new chats won't appear here.")
        return
    for chat in chats:
        title = chat["title"] or "Chat"
        with st.container(horizontal=True, key=f"chat_row_{chat['id']}"):
            st.button(f"{title} · {short_date(chat['updated_at'])}", key=f"open_{chat['id']}", on_click=open_saved_chat,
                      args=(chat["id"], chat["updated_at"]))
            st.button("Delete", icon=":material/delete:", key=f"delete_{chat['id']}", on_click=delete_saved_chat,
                      args=(chat["id"],), help="Delete this chat")


def show_settings() -> None:
    st.markdown('<div class="panel-title">Settings</div>', unsafe_allow_html=True)
    if SESSION:
        st.markdown(f'<div class="panel-note">Signed in as {html.escape(SESSION["email"])}</div>',
                    unsafe_allow_html=True)
    saving = history_is_saved()
    if "save_history" not in st.session_state:  # the API couldn't say, so don't show a switch that might be wrong
        st.error(WAKING_UP)
        return
    if "save_history_toggle" not in st.session_state:
        st.session_state.save_history_toggle = saving
    st.toggle("Save my chat history", key="save_history_toggle", on_change=change_saving)
    days = st.session_state.get("retention_days", 90)
    if saving:
        st.caption(f"Your chats are saved to your account, so you can reopen them from History. "
                   f"A chat is deleted automatically after {days} days with no activity.")
    else:
        st.caption("New chats aren't saved or shown in History, and they're gone once you leave this screen. "
                   "Chats saved earlier stay until you delete them. Anything you ask the helpdesk to do, "
                   "such as a leave request or a ticket, is still recorded.")
    st.markdown('<div class="panel-subtitle">Delete my saved chats</div>', unsafe_allow_html=True)
    confirmed = st.checkbox("Yes, delete all my saved chats. This can't be undone.", key="confirm_delete_all")
    st.button("Delete all chats", icon=":material/delete:", disabled=not confirmed, on_click=delete_all_chats,
              key="delete_all_chats")


def show_chat(question: str | None) -> None:
    # A sleeping API wakes up from this, so an unreachable one isn't asked on every redraw
    if page_status != "connecting" and not history_is_saved():
        st.markdown('<div class="chat-temp-note">Temporary chat: this chat isn\'t being saved. '
                    "You can change that in Settings.</div>", unsafe_allow_html=True)

    for message_index, past_message in enumerate(st.session_state.messages):
        show_message(message_index, past_message)

    if question:
        st.session_state.messages.append({"role": "user", "content": question, "sources": []})
        with st.chat_message("user", avatar=user_avatar()):
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
            st.button("New chat", icon=":material/refresh:", on_click=open_new_chat)
    elif len(st.session_state.messages) == 1:
        st.markdown('<div class="suggest-label">Popular questions</div>', unsafe_allow_html=True)
        columns = st.columns(2)
        for index, suggestion in enumerate(SUGGESTIONS):
            columns[index % 2].button(suggestion, key=f"suggestion_{index}", on_click=queue_question,
                                      args=(suggestion,), use_container_width=True)
    else:
        st.button("New chat", icon=":material/refresh:", on_click=open_new_chat)


st.set_page_config(page_title=f"HR Helpdesk | {COMPANY}", page_icon=":material/support_agent:")
# st.html, not st.markdown: Markdown can draw after the elements below it, showing them unstyled for a moment
st.html(f"<style>{(UI_DIR / 'style.css').read_text(encoding='utf-8')}</style>")

page_status = api_status()

if "client_id" not in st.session_state:
    st.session_state.client_id = uuid.uuid4().hex  # gives each chat its own rate limit
if "messages" not in st.session_state:
    start_new_chat()  # every visit opens on a fresh chat. Earlier ones are under History

# The chat sits in one frame. Only the messages scroll: the header above and the question box below stay put.
with st.container(key="chat_frame", gap=None):
    frame_top = st.container(key="chat_top", gap=None)
    following = st.session_state.get("chat_view", "chat") == "chat"
    messages_area = st.container(key="chat_messages", height=MESSAGES_HEIGHT, border=False, autoscroll=following)
    frame_bottom = st.container(key="chat_bottom", gap=None)

with frame_top:
    # Only while the API can't be reached, the header checks again by itself. Otherwise an
    # open tab would keep waking the hosted API and database.
    st.fragment(show_header, run_every=STATUS_CHECK_SECONDS if page_status == "connecting" else None)(page_status)
    employees = {} if SESSION else demo_employees()
    with st.container(key="chat_tools"):
        if st.session_state.get("chat_view", "chat") != "chat":
            # Away from the chat, an arrow takes you back. In the chat, your photo opens your profile.
            st.button("Back", icon=":material/arrow_back:", key="chat_back", on_click=go_back, help="Back")
        elif SESSION:
            profile = load_profile()
            photo = photo_bytes(profile)
            if photo:
                # The photo is drawn behind the button. It was checked as an image, and base64 can't break the CSS
                encoded = base64.b64encode(photo).decode()
                st.html(f"<style>.st-key-chat_profile button {{ background-image: url('data:image/jpeg;base64,"
                        f"{encoded}'); }} .st-key-chat_profile button p {{ opacity: 0; }}</style>")
            st.button(initials(profile), key="chat_profile", on_click=open_view, args=("profile",),
                      help="Your profile")
        st.button("History", icon=":material/history:", key="chat_history", on_click=open_view, args=("history",))
        st.button("Settings", icon=":material/settings:", key="chat_settings", on_click=open_view, args=("settings",))
        if SESSION:
            st.button("Sign out", icon=":material/logout:", key="chat_sign_out", on_click=sign_out,
                      help=f"Signed in as {SESSION['email']}")
        if employees:
            st.selectbox("Demo employee (fake data, until real sign-in)", list(employees),
                         format_func=employees.get, key="employee_id", on_change=change_employee)

with frame_bottom:
    # A question comes from the box or from a suggested-question button
    question = st.chat_input("Ask about leave, payroll, work from home...", max_chars=500)
    st.markdown(
        '<div class="footer-note">Answers come from company HR policies. For important decisions, '
        f"please confirm with HR at {TENANT['hr_email']}.</div>",
        unsafe_allow_html=True,
    )
question = question or st.session_state.pop("queued_question", None)
if question:
    st.session_state.chat_view = "chat"  # asking from History or Settings carries on with the open chat

with messages_area:
    view = st.session_state.get("chat_view", "chat")
    if view == "history":
        show_history()
    elif view == "profile":
        show_profile()
    elif view == "settings":
        show_settings()
    else:
        show_chat(question)
