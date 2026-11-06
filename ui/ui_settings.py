"""Settings shared by the chat screen and the admin screen."""
import json
import os
import tomllib
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def setting(name: str, default: str = "") -> str:
    """Read a setting from Streamlit Cloud's secrets, or else from .env."""
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:  # no secrets file, as when running on your own PC
        pass
    return os.getenv(name, default)


def tenant() -> dict:
    """Read the company details. The chat screen doesn't install the API's packages, so it reads the file itself."""
    path = setting("TENANT_CONFIG_FILE") or PROJECT_ROOT / "config" / "tenant.toml"
    with Path(path).open("rb") as file:
        return tomllib.load(file)


def demo_employees() -> dict[str, str]:
    """List the mock HR system's fake employees, by id, for demo mode. With sign-in, this isn't used."""
    path = PROJECT_ROOT / "config" / "mock_hrms.json"
    if not path.exists():
        return {}
    return {key: value["name"] for key, value in json.loads(path.read_text(encoding="utf-8"))["employees"].items()}


def api_url() -> str:
    return setting("HELPDESK_API_URL", "http://127.0.0.1:8000").rstrip("/")
