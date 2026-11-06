"""The chat screen, with the API deliberately unreachable. No API quota is used."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

UI_DIR = Path(__file__).resolve().parents[2] / "ui"
APP_FILE = str(UI_DIR / "streamlit_app.py")
ADMIN_FILE = str(UI_DIR / "pages" / "admin.py")


@pytest.fixture
def chat():
    app = AppTest.from_file(APP_FILE, default_timeout=30)
    # Setting this as a secret also checks that secrets work, as on Streamlit Cloud
    app.secrets["HELPDESK_API_URL"] = "http://127.0.0.1:9"
    app.secrets["HELPDESK_API_KEY"] = "test-key"
    return app.run()


def page_text(app):
    return " ".join(block.value for block in app.markdown)


def test_opens_with_company_name_greeting_and_suggestions(chat):
    assert not chat.exception
    assert "Rutuja Talent Nexus" in page_text(chat)
    assert "Welcome to the" in chat.chat_message[0].markdown[0].value
    assert len(chat.button) == 4


def test_shows_connecting_when_the_api_is_unreachable(chat):
    assert "Connecting" in page_text(chat)


def test_unreachable_api_gives_a_friendly_message(chat):
    chat.chat_input[0].set_value("How much leave do I get?").run()
    assert not chat.exception
    reply = chat.chat_message[-1].markdown[0].value
    assert "error-note" in reply and "The helpdesk is waking up" in reply


def test_failed_reply_offers_try_again_and_new_chat(chat):
    assert [button.label for button in chat.button if button.label == "Try again"] == []
    chat.chat_input[0].set_value("How much leave do I get?").run()
    assert [button.label for button in chat.button] == ["Try again", "New chat"]


def test_try_again_asks_the_same_question_without_a_duplicate(chat):
    chat.chat_input[0].set_value("How much leave do I get?").run()
    next(button for button in chat.button if button.label == "Try again").click().run()
    assert not chat.exception
    assert len(chat.chat_message) == 3  # welcome, the question once, and its reply
    assert chat.chat_message[1].markdown[0].value == "How much leave do I get?"


def test_suggestion_button_asks_its_question(chat):
    chat.button(key="suggestion_1").click().run()
    assert not chat.exception
    assert chat.chat_message[1].markdown[0].value == "What is the notice period?"


def test_admin_screen_asks_for_the_key_first():
    app = AppTest.from_file(ADMIN_FILE, default_timeout=30)
    app.secrets["HELPDESK_API_URL"] = "http://127.0.0.1:9"
    app.run()
    assert not app.exception
    assert app.info[0].value.startswith("Enter the admin key")


def test_admin_screen_explains_an_unreachable_api():
    app = AppTest.from_file(ADMIN_FILE, default_timeout=30)
    app.secrets["HELPDESK_API_URL"] = "http://127.0.0.1:9"
    app.run()
    app.text_input[0].set_value("some-key").run()
    assert not app.exception
    assert "waking up" in app.error[0].value


def test_demo_employee_is_chosen_from_fake_data(chat):
    assert chat.selectbox[0].options == ["Asha Kulkarni", "Rohan Joshi", "Meera Patil"]
    assert chat.session_state.employee_id == "E1001"


def test_changing_employee_starts_a_new_chat(chat):
    chat.session_state.conversation_id = "someone-elses-chat"
    chat.selectbox[0].set_value("E1002").run()
    assert not chat.exception
    assert chat.session_state.conversation_id is None


def test_a_pending_action_shows_approve_and_decline(chat):
    chat.session_state.pending_action = {"id": "a1", "summary": "apply for leave"}
    chat.run()
    assert {"approve_action", "decline_action"} <= {button.key for button in chat.button}


def test_approving_with_the_api_unreachable_keeps_the_action_and_explains(chat):
    chat.session_state.pending_action = {"id": "a1", "summary": "apply for leave"}
    chat.run()
    chat.button(key="approve_action").click().run()
    assert not chat.exception
    assert chat.session_state.pending_action == {"id": "a1", "summary": "apply for leave"}
    assert chat.session_state.messages[-1]["error"] is True


def test_a_stored_answer_can_be_rated(chat):
    chat.session_state.messages = [
        {"role": "assistant", "content": "Hello", "sources": []},
        {"role": "assistant", "content": "18 days.", "sources": [], "message_id": 7},
    ]
    chat.run()
    assert not chat.exception
    assert "rating_7" in chat.session_state


def test_admin_page_explains_each_problem_without_crashing():
    app = AppTest.from_file(ADMIN_FILE, default_timeout=30)
    app.secrets["HELPDESK_API_URL"] = "http://127.0.0.1:9"
    app.run()
    app.text_input[0].set_value("some-key").run()
    assert not app.exception
    assert len(app.tabs) == 0  # stops at the error, before showing tabs with no data

