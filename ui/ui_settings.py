"""Settings shared by the chat screen and the admin screen."""
import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def setting(name: str, default: str = "") -> str:
    """Read a setting from Streamlit Cloud's secrets, or else from .env."""
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:  # no secrets file, as when running on your own PC
        pass
    return os.getenv(name, default)


def api_url() -> str:
    return setting("HELPDESK_API_URL", "http://127.0.0.1:8000").rstrip("/")
