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
    assert chat.chat_message[-1].markdown[0].value.startswith("The helpdesk is waking up")


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
