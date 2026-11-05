"""Getting the database address for the copy script. No database is used."""
import pytest

from pipeline import copy_to_cloud

GOOD = "postgresql://user:secret@example.neon.tech/db?sslmode=require"


class Terminal:
    def isatty(self):
        return True


def test_uses_the_setting_when_present(monkeypatch):
    monkeypatch.setenv("CLOUD_DATABASE_URL", GOOD)
    assert copy_to_cloud.get_target_uri() == GOOD


def test_asks_when_the_setting_is_missing(monkeypatch):
    monkeypatch.delenv("CLOUD_DATABASE_URL", raising=False)
    monkeypatch.setattr(copy_to_cloud.sys, "stdin", Terminal())
    monkeypatch.setattr(copy_to_cloud.getpass, "getpass", lambda prompt: f"  {GOOD}\n")
    assert copy_to_cloud.get_target_uri() == GOOD


def test_rejects_text_that_is_not_a_connection_string(monkeypatch):
    monkeypatch.delenv("CLOUD_DATABASE_URL", raising=False)
    monkeypatch.setattr(copy_to_cloud.sys, "stdin", Terminal())
    monkeypatch.setattr(copy_to_cloud.getpass, "getpass", lambda prompt: "not a url")
    with pytest.raises(SystemExit, match="start with postgresql://"):
        copy_to_cloud.get_target_uri()
