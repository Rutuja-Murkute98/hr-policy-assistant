"""The password sign-in and sign-up page, with the API replaced by a fake. No API quota is used."""
import base64
import io
import sys
from pathlib import Path

import pytest
import requests
import streamlit as st
from streamlit.testing.v1 import AppTest

from app.passwords import email_allowed, new_setup_code, password_problem

UI_DIR = Path(__file__).resolve().parents[2] / "ui"
APP_FILE = str(UI_DIR / "streamlit_app.py")
ADMIN_FILE = str(UI_DIR / "pages" / "admin.py")
sys.path.insert(0, str(UI_DIR))

GOOD_PASSWORD = "a-long-test-phrase"
SESSION = {"user_id": "u1", "email": "asha@example.test", "display_name": "Asha Rao", "role": "employee",
           "tenant_id": "default",
           "organisation": {"company_name": "Globex <b>Ltd</b>", "product_name": "Globex HR", "logo_initials": "G",
                            "hr_email": "hr@globex.example", "legal_name": "Globex", "default_category": "general",
                            "document_categories": {}, "id": "default"}}
SIGNED_IN = {"token": "hrs_test", "max_age_seconds": 100, "session": SESSION}


class Reply:
    def __init__(self, status_code=200, body=None):
        self.status_code, self.ok, self.body = status_code, status_code < 400, {} if body is None else body

    def json(self):
        return self.body


@pytest.fixture
def api(monkeypatch):
    """A fake API. A test sets replies by path, and can look at what was sent."""
    fake = type("Api", (), {"sent": [], "replies": {}, "cookies": {}})()
    fake.sent, fake.replies, fake.cookies = [], {}, {}

    def reply(method, url, **kwargs):
        path = url.split("/", 3)[-1]
        fake.sent.append((method, "/" + path, kwargs))
        return fake.replies.get("/" + path) or Reply(404, {"detail": "not here"})

    monkeypatch.setattr(requests, "request", lambda method, url, **kwargs: reply(method, url, **kwargs))

    monkeypatch.setenv("AUTH_MODE", "password")
    monkeypatch.setattr(requests, "get", lambda url, **kwargs: reply("GET", url, **kwargs))
    monkeypatch.setattr(requests, "post", lambda url, **kwargs: reply("POST", url, **kwargs))
    st.cache_data.clear()
    import sign_in
    monkeypatch.setattr(sign_in, "saved_token", lambda: fake.cookies.get("token", ""))
    return fake


def open_page(file=APP_FILE):
    return AppTest.from_file(file, default_timeout=30).run()


def text_box(app, label):
    return next(box for box in app.text_input if box.label == label)


def sent_to(api, path):
    return [call for call in api.sent if call[1] == path]


def test_nothing_shows_until_someone_signs_in(api):
    for file in (APP_FILE, ADMIN_FILE):
        app = open_page(file)
        assert not app.exception
        assert not app.chat_input
        assert [box.label for box in app.text_input] == ["Work email", "Password"]
        assert not app.tabs


def test_signing_in_opens_the_chat_and_greets_by_name(api):
    api.replies["/auth/login"] = Reply(body=SIGNED_IN)
    app = open_page()
    text_box(app, "Work email").set_value(" asha@example.test ")
    text_box(app, "Password").set_value(GOOD_PASSWORD)
    app = submit(app, "sign_in_form")
    assert not app.exception
    assert app.chat_input
    assert "Hello, Asha!" in " ".join(block.value for block in app.markdown)
    assert "Globex &lt;b&gt;Ltd&lt;/b&gt;" in " ".join(block.value for block in app.markdown)
    fields = sent_to(api, "/auth/login")[0][2]["json"]
    assert fields == {"email": "asha@example.test", "password": GOOD_PASSWORD, "remember": True}
    assert app.session_state.auth_token == "hrs_test"


def submit(app, form):
    return next(button for button in app.button if button.form_id == form).click().run()


def test_the_password_is_not_kept_after_signing_in(api):
    api.replies["/auth/login"] = Reply(body=SIGNED_IN)
    app = open_page()
    text_box(app, "Work email").set_value("asha@example.test")
    text_box(app, "Password").set_value(GOOD_PASSWORD)
    app = submit(app, "sign_in_form")
    assert GOOD_PASSWORD not in str(app.session_state)


def test_a_refused_sign_in_shows_why_and_stays_on_the_page(api):
    api.replies["/auth/login"] = Reply(401, {"detail": "That email and password don't match."})
    app = open_page()
    text_box(app, "Work email").set_value("asha@example.test")
    text_box(app, "Password").set_value("wrong-phrase-here")
    app = submit(app, "sign_in_form")
    assert "don't match" in app.error[0].value
    assert not app.chat_input


def test_an_unreachable_api_is_explained(api, monkeypatch):
    def fail(*args, **kwargs):
        raise requests.ConnectionError

    monkeypatch.setattr(requests, "post", fail)
    app = open_page()
    text_box(app, "Work email").set_value("asha@example.test")
    text_box(app, "Password").set_value(GOOD_PASSWORD)
    app = submit(app, "sign_in_form")
    assert not app.exception
    assert "waking up" in app.error[0].value


def create_account_view(app):
    app.button_group[0].set_value("Create account")
    return app.run()


def test_creating_an_account_signs_the_person_in(api):
    api.replies["/auth/signup"] = Reply(201, SIGNED_IN)
    app = create_account_view(open_page())
    assert [box.label for box in app.text_input] == ["Full name", "Work email", "Password", "Confirm password"]
    text_box(app, "Full name").set_value("Asha Rao")
    text_box(app, "Work email").set_value("asha@example.test")
    text_box(app, "Password").set_value(GOOD_PASSWORD)
    text_box(app, "Confirm password").set_value(GOOD_PASSWORD)
    app = submit(app, "sign_up_form")
    assert not app.exception
    assert app.chat_input
    assert sent_to(api, "/auth/signup")[0][2]["json"]["display_name"] == "Asha Rao"


@pytest.mark.parametrize(("name", "email", "password", "again", "message"), [
    ("", "a@example.test", GOOD_PASSWORD, GOOD_PASSWORD, "your name"),
    ("Asha", "not-an-email", GOOD_PASSWORD, GOOD_PASSWORD, "valid email"),
    ("Asha", "a@example.test", "short", "short", "at least 10"),
    ("Asha", "a@example.test", GOOD_PASSWORD, "something-different", "don't match"),
])
def test_the_form_catches_mistakes_before_sending(api, name, email, password, again, message):
    app = create_account_view(open_page())
    text_box(app, "Full name").set_value(name)
    text_box(app, "Work email").set_value(email)
    text_box(app, "Password").set_value(password)
    text_box(app, "Confirm password").set_value(again)
    app = submit(app, "sign_up_form")
    assert message in app.error[0].value
    assert not sent_to(api, "/auth/signup")


def test_sign_up_is_hidden_when_it_is_switched_off(api):
    api.replies["/auth/options"] = Reply(body={"signup_enabled": False})
    app = open_page()
    assert not app.button_group
    assert [box.label for box in app.text_input] == ["Work email", "Password"]


def test_a_saved_session_signs_the_person_back_in_without_asking(api):
    api.cookies["token"] = "hrs_saved"
    api.replies["/me"] = Reply(body=SESSION)
    app = open_page()
    assert not app.exception
    assert app.chat_input
    assert not app.text_input
    assert sent_to(api, "/me")[0][2]["headers"] == {"Authorization": "Bearer hrs_saved"}
    assert not sent_to(api, "/auth/login")


def test_an_ended_saved_session_goes_to_the_sign_in_page(api):
    api.cookies["token"] = "hrs_old"
    api.replies["/me"] = Reply(401, {"detail": "Please sign in again."})
    app = open_page()
    assert not app.exception
    assert not app.chat_input
    assert [box.label for box in app.text_input] == ["Work email", "Password"]


def signed_in_page(api, settings=None):
    api.cookies["token"] = "hrs_saved"
    api.replies["/health"] = Reply(body={"ai": "ok"})
    api.replies["/me"] = Reply(body=SESSION)
    api.replies["/me/settings"] = Reply(body=settings or {"save_chat_history": True, "retention_days": 90})
    api.replies["/conversations"] = Reply(body=[{"id": "chat1", "title": "How much leave?",
                                                 "updated_at": "2026-11-07T10:00:00+00:00"}])
    api.replies["/conversations/chat1/messages"] = Reply(body=[
        {"role": "user", "content": "How much leave?"}, {"role": "assistant", "content": "You get 12 days [1, 2]."}])
    return open_page()


def shown(app):
    return " ".join(block.value for block in app.markdown)


def press(app, key):
    return next(button for button in app.button if button.key == key).click().run()


def test_every_visit_opens_on_a_fresh_chat(api):
    app = signed_in_page(api)
    assert "Hello, Asha!" in shown(app)
    assert "How much leave?" not in shown(app)
    assert app.session_state.conversation_id is None
    assert not sent_to(api, "/conversations")


def test_a_saved_chat_opens_from_history_and_back_returns(api):
    app = press(signed_in_page(api), "chat_history")
    assert "Your chats" in shown(app)
    assert next(button for button in app.button if button.key == "open_chat1").label.startswith("How much leave?")
    app = press(app, "chat_back")  # the arrow returns to the fresh chat History was opened from
    assert "Hello, Asha!" in shown(app)
    app = press(press(app, "chat_history"), "open_chat1")
    assert "How much leave?" in shown(app) and "You get 12 days." in shown(app)
    assert app.session_state.conversation_id == "chat1"


def test_the_chat_has_no_back_arrow(api):
    app = signed_in_page(api)
    assert not [button for button in app.button if button.key == "chat_back"]


def test_history_explains_a_failure_without_crashing(api):
    app = signed_in_page(api)
    api.replies["/conversations"] = Reply(500, {"detail": "Please try again."})
    app = press(app, "chat_history")
    assert not app.exception
    assert "Please try again." in app.error[0].value


def test_a_chat_can_be_deleted_from_history(api):
    api.replies["/conversations/chat1"] = Reply(body={"deleted": "chat1"})
    app = press(signed_in_page(api), "chat_history")
    app = press(app, "delete_chat1")
    assert not app.exception
    assert [call for call in api.sent if call[:2] == ("DELETE", "/conversations/chat1")]


def test_deleting_the_open_chat_starts_a_fresh_one_and_drops_it_from_back(api):
    api.replies["/conversations"] = Reply(body=[{"id": "chat1", "title": "How much leave?",
                                                 "updated_at": "2026-11-07T10:00:00+00:00"}])
    api.replies["/conversations/chat1"] = Reply(body={"deleted": "chat1"})
    app = press(signed_in_page(api), "chat_history")
    app = press(app, "open_chat1")
    app = press(app, "chat_history")
    app = press(app, "delete_chat1")
    assert app.session_state.conversation_id is None
    assert all(place["conversation_id"] != "chat1" for place in app.session_state.back_stack)


def test_settings_shows_the_choice_and_saves_a_change(api):
    app = press(signed_in_page(api), "chat_settings")
    assert "Settings" in shown(app)
    toggle = next(item for item in app.toggle if item.label == "Save my chat history")
    assert toggle.value is True
    api.replies["/me/settings"] = Reply(body={"save_chat_history": False, "retention_days": 90})
    app = toggle.set_value(False).run()
    assert not app.exception
    change = [call for call in api.sent if call[:2] == ("PATCH", "/me/settings")][0]
    assert change[2]["json"] == {"save_chat_history": False}
    assert "aren't saved" in " ".join(item.value for item in app.caption)


def test_a_failed_change_puts_the_switch_back(api):
    app = press(signed_in_page(api), "chat_settings")
    api.replies["/me/settings"] = Reply(500, {"detail": "Please try again."})
    app = next(item for item in app.toggle if item.label == "Save my chat history").set_value(False).run()
    assert not app.exception
    assert next(item for item in app.toggle if item.label == "Save my chat history").value is True


def test_a_chat_that_is_not_saved_says_so(api):
    app = signed_in_page(api, {"save_chat_history": False, "retention_days": 90})
    assert "Temporary chat" in shown(app)


def test_deleting_everything_needs_a_tick_first(api):
    api.replies["/conversations"] = Reply(body={"deleted": 3})
    app = press(signed_in_page(api), "chat_settings")
    delete = next(button for button in app.button if button.key == "delete_all_chats")
    assert delete.disabled
    app = next(box for box in app.checkbox if box.key == "confirm_delete_all").check().run()
    app = press(app, "delete_all_chats")
    assert not app.exception
    assert [call for call in api.sent if call[:2] == ("DELETE", "/conversations")]


def test_signing_out_ends_the_session_and_forgets_the_chat(api):
    api.cookies["token"] = "hrs_saved"
    api.replies["/me"] = Reply(body=SESSION)
    api.replies["/auth/logout"] = Reply(body={"signed_out": True})
    app = open_page()
    sign_out = next(button for button in app.button if button.label.startswith("Sign out"))
    app = sign_out.click().run()
    assert not app.exception
    assert not app.chat_input
    assert sent_to(api, "/auth/logout")[0][2]["headers"] == {"Authorization": "Bearer hrs_saved"}
    assert "messages" not in app.session_state
    assert "auth_token" not in app.session_state
    assert len(sent_to(api, "/me")) == 1  # the old cookie isn't tried again in this tab


def test_a_set_up_code_sets_a_password_and_signs_in(api):
    api.replies["/auth/password"] = Reply(body=SIGNED_IN)
    app = open_page()
    app = next(button for button in app.button if button.key == "auth_forgot").click().run()
    text_box(app, "Work email").set_value("asha@example.test")
    text_box(app, "Set-up code").set_value("ABCD-EFGH-JKMN")
    text_box(app, "New password").set_value(GOOD_PASSWORD)
    text_box(app, "Confirm new password").set_value(GOOD_PASSWORD)
    app = submit(app, "reset_form")
    assert not app.exception
    assert app.chat_input
    assert sent_to(api, "/auth/password")[0][2]["json"]["code"] == "ABCD-EFGH-JKMN"


def test_an_employee_cannot_open_the_admin_screen(api):
    api.cookies["token"] = "hrs_saved"
    api.replies["/me"] = Reply(body=SESSION)
    app = open_page(ADMIN_FILE)
    assert not app.exception
    assert "This page is for HR admins" in app.error[0].value


def test_organisation_details_cannot_add_html_to_the_page(api):
    api.cookies["token"] = "hrs_saved"
    api.replies["/me"] = Reply(body=SESSION)
    app = open_page()
    assert "<b>Ltd</b>" not in " ".join(block.value for block in app.markdown)


# The rules themselves

@pytest.mark.parametrize("password", ["short", "1234567890", "password123", "aaaaaaaaaaaaaa", "ABABABABABAB"])
def test_weak_passwords_are_refused(password):
    assert password_problem(password)


def test_a_password_cannot_contain_your_name_or_email():
    assert "name or email" in password_problem("asha-rao-is-great", "asha.rao@example.test", "Asha Rao".split()[0])
    assert password_problem("correct-horse-battery", "asha@example.test", "Asha Rao") is None


def test_email_domains_limit_who_can_sign_up(monkeypatch):
    from app import config

    monkeypatch.setattr(config, "SIGNUP_EMAIL_DOMAINS", [])
    assert email_allowed("a@anything.test")
    monkeypatch.setattr(config, "SIGNUP_EMAIL_DOMAINS", ["company.test"])
    assert email_allowed("a@COMPANY.test")
    assert not email_allowed("a@company.test.evil.example")


def test_set_up_codes_are_long_and_easy_to_read():
    code = new_setup_code()
    assert len(code) == 14 and code.count("-") == 2
    assert not set(code) & set("01OIL")
    assert code != new_setup_code()


# Profile

PROFILE = {"display_name": "Asha Rao", "email": "asha@example.test", "job_title": "", "department": "",
           "location": "", "photo": ""}


def profile_page(api, profile=None):
    api.replies["/me/profile"] = Reply(body=profile or PROFILE)
    return signed_in_page(api)


def test_the_profile_button_shows_initials_where_back_used_to_be(api):
    app = profile_page(api)
    assert next(button for button in app.button if button.key == "chat_profile").label == "AR"
    assert not [button for button in app.button if button.key == "chat_back"]


def test_the_profile_opens_and_a_back_arrow_returns_to_the_chat(api):
    app = press(profile_page(api), "chat_profile")
    assert "Your profile" in shown(app)
    assert [box.label for box in app.text_input][:4] == ["Full name", "Job title", "Department", "Work location"]
    assert not [button for button in app.button if button.key == "chat_profile"]
    app = press(app, "chat_back")
    assert "Hello, Asha!" in shown(app)
    assert next(button for button in app.button if button.key == "chat_profile")


def test_settings_and_history_have_a_back_arrow_too(api):
    for key in ("chat_settings", "chat_history"):
        app = press(profile_page(api), key)
        assert next(button for button in app.button if button.key == "chat_back")


def test_saving_the_profile_sends_the_details_and_uses_the_new_name(api):
    api.replies["/me/profile"] = Reply(body=PROFILE)
    app = press(profile_page(api), "chat_profile")
    app.text_input[0].set_value("Asha Kulkarni")
    app.text_input[1].set_value("Engineer")
    api.replies["/me/profile"] = Reply(body={**PROFILE, "display_name": "Asha Kulkarni", "job_title": "Engineer"})
    app = submit(app, "profile_form")
    assert not app.exception
    sent = [call for call in api.sent if call[:2] == ("PUT", "/me/profile")][0][2]["json"]
    assert sent == {"display_name": "Asha Kulkarni", "job_title": "Engineer", "department": "", "location": ""}
    app = press(app, "chat_back")
    app = press(app, "history_new_chat") if [b for b in app.button if b.key == "history_new_chat"] else app
    assert app.session_state.signed_in["display_name"] == "Asha Kulkarni"


def test_a_profile_needs_a_name(api):
    app = press(profile_page(api), "chat_profile")
    app.text_input[0].set_value("  ")
    app = submit(app, "profile_form")
    assert "Please enter your name." in app.error[0].value
    assert not [call for call in api.sent if call[:2] == ("PUT", "/me/profile")]


def test_a_photo_is_used_for_the_button_and_your_messages(api):
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 120, 110)).save(output, format="JPEG")
    with_photo = {**PROFILE, "photo": "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode()}
    app = profile_page(api, with_photo)
    assert "background-image" in " ".join(str(block.value) for block in app.get("html"))
    assert next(button for button in app.button if button.key == "chat_profile").label == "AR"  # still read aloud
    app = press(app, "chat_profile")
    assert not app.exception
    assert next(button for button in app.button if button.key == "remove_photo")


def test_a_profile_that_cannot_load_explains_without_crashing(api):
    api.replies["/me/profile"] = Reply(500, {"detail": "Please try again."})
    app = press(signed_in_page(api), "chat_profile")
    assert not app.exception
    assert app.error
