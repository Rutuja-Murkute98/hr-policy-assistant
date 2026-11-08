"""Sign-in for the screens. The API always decides who someone is: a screen only passes on what proves it.

- AUTH_MODE=password: people sign up and sign in here with an email and password. The API returns a session
  token, which a browser cookie keeps for the next visit. The password itself is never kept by this screen.
- AUTH_MODE=oidc: Streamlit handles the sign-in with the provider and keeps the ID token, as set up under
  [auth] in its secrets.
"""
import html
import json
import re
from pathlib import Path

import requests
import streamlit as st
from ui_settings import api_url, setting, tenant

TIMEOUT_SECONDS = 150  # long enough for a free hosted API and database to wake up
COOKIE = "hr_helpdesk_session"
UI_DIR = Path(__file__).resolve().parent
WAKING_UP = "The helpdesk is waking up or unavailable. Please try again in a minute."
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SIGN_IN, SIGN_UP = "Sign in", "Create account"


def auth_mode() -> str:
    return setting("AUTH_MODE", "demo")


def sign_in_required() -> bool:
    return auth_mode() != "demo"


def password_mode() -> bool:
    return auth_mode() == "password"


def bearer_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def auth_token() -> str:
    """What proves who the signed-in person is, on each request to the API."""
    return st.session_state.get("auth_token", "") if password_mode() else st.user.tokens["id"]


def safe_organisation(organisation: dict) -> dict:
    """Escape what an organisation's details say, because the screens put them into HTML."""
    return {key: html.escape(value) if isinstance(value, str) else value for key, value in organisation.items()}


def safe_session(details: dict) -> dict:
    return {**details, "organisation": safe_organisation(details["organisation"])}


def fetch_session(url: str, token: str) -> tuple[dict | None, str]:
    """Return (the signed-in person's details, ""), or (None, a message saying what went wrong)."""
    try:
        response = requests.post(f"{url}/auth/session", headers=bearer_headers(token), timeout=TIMEOUT_SECONDS)
    except requests.RequestException:
        return None, WAKING_UP
    try:
        details = response.json()
    except ValueError:
        return None, f"Something went wrong (error {response.status_code}). Please try again."
    if not response.ok:
        return None, str(details.get("detail", "You couldn't be signed in."))
    return safe_session(details), ""


# Keeping the session between visits

def saved_token() -> str:
    """Read the session this browser kept from an earlier visit."""
    try:
        return st.context.cookies.get(COOKIE) or ""
    except Exception:  # no browser behind this run, as in a test
        return ""


def _run_in_browser(script: str) -> None:
    st.html(f"<script>{script}</script>", unsafe_allow_javascript=True)


def _apply_cookie_changes() -> None:
    """Streamlit can't set cookies from the server, so the page does it, once, after a sign-in or sign-out."""
    change = st.session_state.pop("cookie_change", None)
    if change is None:
        return
    # Secure on https, so it's never sent in the clear. No Max-Age means the browser forgets it on closing.
    secure = "(location.protocol === 'https:' ? '; Secure' : '')"
    if change["token"]:
        max_age = f"; Max-Age={int(change['max_age'])}" if change["max_age"] else ""
        value = json.dumps(f"{COOKIE}={change['token']}; Path=/; SameSite=Lax{max_age}")
    else:
        value = json.dumps(f"{COOKIE}=; Path=/; SameSite=Lax; Max-Age=0")
    _run_in_browser(f"document.cookie = {value} + {secure};")


def _accept_session(token: str, session: dict, max_age: int | None = None, keep_cookie: bool = True) -> None:
    st.session_state.auth_token = token
    st.session_state.signed_in = safe_session(session)
    if keep_cookie:
        st.session_state.cookie_change = {"token": token, "max_age": max_age}


def _restore_saved_session(url: str) -> bool:
    """Sign the person back in from their browser's cookie, if that session is still good."""
    token = saved_token()
    if not token or token == st.session_state.get("ended_token"):
        return False
    try:
        response = requests.get(f"{url}/me", headers=bearer_headers(token), timeout=TIMEOUT_SECONDS)
    except requests.RequestException:
        return False
    if response.ok:
        _accept_session(token, response.json(), keep_cookie=False)
        return True
    if response.status_code == 401:  # expired or ended, so the browser can stop sending it
        st.session_state.ended_token = token
        st.session_state.cookie_change = {"token": "", "max_age": None}
    return False


def sign_out() -> None:
    """End the session on the server and in this browser, and forget everything about this person's chat."""
    if not password_mode():
        st.logout()
        return
    token = st.session_state.get("auth_token", "")
    if token:
        try:
            requests.post(f"{api_url()}/auth/logout", headers=bearer_headers(token), timeout=30)
        except requests.RequestException:  # the cookie still goes, and the session ends when it expires
            pass
    st.session_state.clear()  # one person must never see another's chat in this tab
    st.session_state.ended_token = saved_token()  # this run's copy of the cookies still has the old one
    st.session_state.cookie_change = {"token": "", "max_age": None}


# Talking to the API before anyone is signed in

@st.cache_data(ttl=60, show_spinner=False)
def _fetch_options(url: str) -> dict:
    try:
        response = requests.get(f"{url}/auth/options", timeout=5)
        return response.json() if response.ok else {}
    except (requests.RequestException, ValueError):
        return {}


def _send(url: str, path: str, fields: dict) -> tuple[dict | None, str]:
    """Return (the reply, ""), or (None, a message for the person)."""
    try:
        response = requests.post(f"{url}{path}", json=fields, timeout=TIMEOUT_SECONDS)
    except requests.RequestException:
        return None, WAKING_UP
    try:
        reply = response.json()
    except ValueError:
        return None, f"Something went wrong (error {response.status_code}). Please try again."
    if response.ok:
        return reply, ""
    detail = reply.get("detail")
    return None, detail if isinstance(detail, str) else "Please check your details and try again."


def _finish_sign_in(reply: dict) -> None:
    _accept_session(reply["token"], reply["session"], reply.get("max_age_seconds"))
    st.session_state.pop("auth_reset", None)
    st.rerun()


# The page

def _hero() -> str:
    company = tenant()
    name, product = html.escape(company["company_name"]), html.escape(company["product_name"])
    return f"""<div class="auth-hero">
      <div class="auth-brand">
        <div class="brand-logo" aria-hidden="true">{html.escape(company['logo_initials'])}</div>
        <div><div class="auth-brand-name">{name}</div><div class="auth-brand-product">{product}</div></div>
      </div>
      <h1 class="auth-title">Your HR questions, answered from your own policies.</h1>
    </div>"""


def _heading(title: str) -> None:
    st.markdown(f'<h2 class="auth-heading">{title}</h2>', unsafe_allow_html=True)


def _show_sign_in_form(url: str, remember_days: int) -> None:
    with st.form("sign_in_form", border=False):
        email = st.text_input("Work email", placeholder="you@company.com", autocomplete="email", icon=":material/mail:")
        password = st.text_input("Password", type="password", autocomplete="current-password",
                                 placeholder="Your password", icon=":material/lock:")
        remember = st.checkbox(f"Keep me signed in for {remember_days} days", value=True)
        submitted = st.form_submit_button(SIGN_IN, type="primary", width="stretch")
    if not submitted:
        return
    if not EMAIL.match(email.strip()) or not password:
        st.error("Please enter your email and password.")
        return
    reply, problem = _send(url, "/auth/login", {"email": email.strip(), "password": password, "remember": remember})
    if problem:
        st.error(problem)
    else:
        _finish_sign_in(reply)


def _show_sign_up_form(url: str, options: dict, remember_days: int) -> None:
    shortest = options.get("min_password_chars", 10)
    domains = options.get("signup_email_domains") or []
    with st.form("sign_up_form", border=False):
        name = st.text_input("Full name", placeholder="Your name", autocomplete="name", max_chars=100,
                             icon=":material/person:")
        email = st.text_input("Work email", placeholder=f"you@{domains[0] if domains else 'company.com'}",
                              autocomplete="email", icon=":material/mail:")
        password = st.text_input("Password", type="password", autocomplete="new-password", icon=":material/lock:",
                                 placeholder=f"At least {shortest} characters",
                                 help="A short phrase of a few words is both strong and easy to remember.")
        again = st.text_input("Confirm password", type="password", autocomplete="new-password",
                              placeholder="The same password again", icon=":material/lock:")
        remember = st.checkbox(f"Keep me signed in for {remember_days} days", value=True)
        submitted = st.form_submit_button("Create my account", type="primary", width="stretch")
    if not submitted:
        return
    if not name.strip():
        st.error("Please enter your name.")
    elif not EMAIL.match(email.strip()):
        st.error("Please enter a valid email address.")
    elif len(password) < shortest:
        st.error(f"Please use a password of at least {shortest} characters.")
    elif password != again:
        st.error("Those passwords don't match.")
    else:
        reply, problem = _send(url, "/auth/signup", {"display_name": name.strip(), "email": email.strip(),
                                                     "password": password, "remember": remember})
        if problem:
            st.error(problem)
        else:
            _finish_sign_in(reply)


def _show_reset_form(url: str, options: dict) -> None:
    shortest = options.get("min_password_chars", 10)
    with st.form("reset_form", border=False):
        email = st.text_input("Work email", placeholder="you@company.com", autocomplete="email", icon=":material/mail:")
        code = st.text_input("Set-up code", placeholder="ABCD-EFGH-JKMN", autocomplete="one-time-code",
                             icon=":material/key:", max_chars=40)
        password = st.text_input("New password", type="password", autocomplete="new-password", icon=":material/lock:",
                                 placeholder=f"At least {shortest} characters")
        again = st.text_input("Confirm new password", type="password", autocomplete="new-password",
                              placeholder="The same password again", icon=":material/lock:")
        submitted = st.form_submit_button("Save password and sign in", type="primary", width="stretch")
    if not submitted:
        return
    if not EMAIL.match(email.strip()) or not code.strip():
        st.error("Please enter your email and the code from your HR team.")
    elif len(password) < shortest:
        st.error(f"Please use a password of at least {shortest} characters.")
    elif password != again:
        st.error("Those passwords don't match.")
    else:
        reply, problem = _send(url, "/auth/password", {"email": email.strip(), "code": code.strip(),
                                                       "password": password, "remember": True})
        if problem:
            st.error(problem)
        else:
            _finish_sign_in(reply)


def _set_reset(showing: bool) -> None:
    st.session_state.auth_reset = showing


def _show_sign_in_page(url: str) -> None:
    company = tenant()
    st.set_page_config(page_title=f"Sign in | {company['company_name']}", page_icon=":material/support_agent:")
    style = "".join((UI_DIR / name).read_text(encoding="utf-8") for name in ("style.css", "sign_in.css"))
    # st.html, not st.markdown: Markdown can draw after the elements below it, showing them unstyled for a moment
    st.html(f"<style>{style}</style>")
    options = _fetch_options(url)
    signup_enabled = options.get("signup_enabled", True)
    remember_days = options.get("session_remember_days", 30)

    with st.container(key="auth_page"):
        hero, panel = st.columns([1, 1.04], gap=None)
        hero.markdown(_hero(), unsafe_allow_html=True)
        with panel:
            if st.session_state.get("auth_reset"):
                _heading("Set your password")
                _show_reset_form(url, options)
                st.button("Back to sign in", type="tertiary", on_click=_set_reset, args=(False,), key="auth_back")
            else:
                view = SIGN_IN
                if signup_enabled:
                    view = st.segmented_control("Choose", [SIGN_IN, SIGN_UP], default=SIGN_IN, required=True,
                                                key="auth_view", label_visibility="collapsed", width="stretch")
                if view == SIGN_UP:
                    _heading("Create your account")
                    _show_sign_up_form(url, options, remember_days)
                else:
                    _heading("Welcome back")
                    _show_sign_in_form(url, remember_days)
                    st.button("Forgot your password, or have a set-up code?", type="tertiary", on_click=_set_reset,
                              args=(True,), key="auth_forgot")
            email = html.escape(company["hr_email"])
            st.markdown(f'<div class="auth-help">Need help? Contact HR at <a href="mailto:{email}">{email}</a></div>',
                        unsafe_allow_html=True)


def _require_password_sign_in(url: str) -> dict:
    _apply_cookie_changes()
    if "signed_in" not in st.session_state and not _restore_saved_session(url):
        _apply_cookie_changes()  # a saved session that's no longer good
        _show_sign_in_page(url)
        st.stop()
    return st.session_state.signed_in


def require_sign_in(url: str) -> dict | None:
    """Stop the page until someone is signed in. Returns their details, or None in demo mode."""
    if not sign_in_required():
        return None
    if password_mode():
        return _require_password_sign_in(url)
    if not st.user.is_logged_in:
        st.title("HR Helpdesk")
        st.write("Please sign in with your work account.")
        st.button("Sign in", type="primary", on_click=st.login)
        st.stop()
    token = st.user.tokens["id"]
    if st.session_state.get("signed_in_token") != token:  # a new sign-in, not just a page refresh
        session, problem = fetch_session(url, token)
        if problem:
            st.error(problem)
            st.button("Sign out", on_click=st.logout)
            st.stop()
        st.session_state.signed_in_token = token
        st.session_state.signed_in = session
    return st.session_state.signed_in
