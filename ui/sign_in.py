"""Sign-in for the screens. With AUTH_MODE=oidc, a person signs in and the API learns who they are.

Streamlit handles the sign-in with the provider and keeps the ID token, as set up under [auth] in
its secrets. The API checks that token on every request, so it's the API that decides who someone is.
"""
import html

import requests
import streamlit as st
from ui_settings import setting

TIMEOUT_SECONDS = 150  # long enough for a free hosted API and database to wake up


def sign_in_required() -> bool:
    return setting("AUTH_MODE", "demo") == "oidc"


def bearer_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def safe_organisation(organisation: dict) -> dict:
    """Escape what an organisation's details say, because the screens put them into HTML."""
    return {key: html.escape(value) if isinstance(value, str) else value for key, value in organisation.items()}


def fetch_session(api_url: str, token: str) -> tuple[dict | None, str]:
    """Return (the signed-in person's details, ""), or (None, a message saying what went wrong)."""
    try:
        response = requests.post(f"{api_url}/auth/session", headers=bearer_headers(token), timeout=TIMEOUT_SECONDS)
    except requests.RequestException:
        return None, "The helpdesk is waking up or unavailable. Please try again in a minute."
    try:
        details = response.json()
    except ValueError:
        return None, f"Something went wrong (error {response.status_code}). Please try again."
    if not response.ok:
        return None, str(details.get("detail", "You couldn't be signed in."))
    return {**details, "organisation": safe_organisation(details["organisation"])}, ""


def require_sign_in(api_url: str) -> dict | None:
    """Stop the page until someone is signed in. Returns their details, or None in demo mode."""
    if not sign_in_required():
        return None
    if not st.user.is_logged_in:
        st.title("HR Helpdesk")
        st.write("Please sign in with your work account.")
        st.button("Sign in", type="primary", on_click=st.login)
        st.stop()
    token = st.user.tokens["id"]
    if st.session_state.get("signed_in_token") != token:  # a new sign-in, not just a page refresh
        session, problem = fetch_session(api_url, token)
        if problem:
            st.error(problem)
            st.button("Sign out", on_click=st.logout)
            st.stop()
        st.session_state.signed_in_token = token
        st.session_state.signed_in = session
    return st.session_state.signed_in
