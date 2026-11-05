"""Stage F: the chat screen. It only talks to the API, and has no RAG code of its own.

Run from the project folder (with the API running):
    venv\\Scripts\\streamlit run ui\\streamlit_app.py
"""
import os
import re
import time
from pathlib import Path

import requests
import streamlit as st
from dotenv import load_dotenv

UI_DIR = Path(__file__).resolve().parent
load_dotenv(UI_DIR.parent / ".env")
API_URL = os.getenv("HELPDESK_API_URL", "http://127.0.0.1:8000")
API_KEY = os.getenv("HELPDESK_API_KEY", "")

COMPANY = "Rutuja Talent Nexus"
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
TYPING_DOTS = '<div class="typing" role="status" aria-label="Typing"><span></span><span></span><span></span></div>'


@st.cache_data(ttl=30, show_spinner=False)
def api_is_online() -> bool:
    """Check the API's health at most once every 30 seconds."""
    try:
        return requests.get(f"{API_URL}/health", timeout=3).ok
    except requests.RequestException:
        return False


def ask_api(question: str) -> tuple[str, list[dict]]:
    """Return (answer, sources), or a friendly message if the API call fails."""
    try:
        response = requests.post(f"{API_URL}/ask", json={"question": question},
                                 headers={"X-API-Key": API_KEY}, timeout=90)
    except requests.RequestException:
        return "I can't reach the helpdesk right now. Please try again in a moment.", []
    if response.ok:
        data = response.json()
        return data["answer"], data["sources"]
    if response.status_code == 422:
        return "Please type a question of 2 to 500 characters.", []
    try:
        return response.json()["detail"], []
    except (ValueError, KeyError):
        return "Something went wrong. Please try again.", []


def type_out(text: str):
    """Reveal the answer word by word."""
    for word in re.findall(r"\S+\s*", text):
        yield word
        time.sleep(0.018)


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
      <div class="brand-logo" aria-hidden="true">RT</div>
      <div class="brand-text">
        <div class="brand-company">{COMPANY}</div>
        <div class="brand-product">HR Helpdesk Assistant</div>
      </div>
      <div class="brand-status {'online' if online else ''}">
        <span class="status-dot"></span>{'Online' if online else 'Offline'}
      </div>
    </div>""",
    unsafe_allow_html=True,
)

if "messages" not in st.session_state:
    start_new_chat()

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
        typing = st.empty()
        typing.markdown(TYPING_DOTS, unsafe_allow_html=True)
        answer, sources = ask_api(question)
        typing.empty()
        st.write_stream(type_out(answer))
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
    "please confirm with HR at hr@rutujatalentnexus.in.</div>",
    unsafe_allow_html=True,
)
