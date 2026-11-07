"""How backups treat passwords and restore errors, with no database."""
import subprocess

import pytest

from ops import backup
from ops.backup import BackupError, _run, _tool_arguments, restore_failed

TIMEOUT_ERROR = 'pg_restore: error: could not execute query: ERROR:  unrecognized configuration parameter "transaction_timeout"'
REAL_ERROR = 'pg_restore: error: could not execute query: ERROR:  relation "users" already exists'


def test_a_password_only_goes_in_the_private_environment():
    address, environment = _tool_arguments("postgresql://helpdesk:s3cret-value@db.example:5432/app?sslmode=require")
    assert "s3cret-value" not in address
    assert address == "postgresql://helpdesk@db.example:5432/app?sslmode=require"
    assert environment["PGPASSWORD"] == "s3cret-value"


def test_an_address_with_no_password_still_works():
    address, _ = _tool_arguments("postgresql://helpdesk@localhost:5432/app")
    assert address == "postgresql://helpdesk@localhost:5432/app"


def test_a_failure_message_never_shows_the_password(monkeypatch):
    failed = subprocess.CompletedProcess([], 1, stderr="could not connect using s3cret-value")
    monkeypatch.setattr(backup.subprocess, "run", lambda *args, **kwargs: failed)
    with pytest.raises(BackupError) as error:
        _run(["pg_dump"], {}, "The backup", "s3cret-value")
    assert "s3cret-value" not in str(error.value)
    assert "***" in str(error.value)


@pytest.mark.parametrize("returncode, errors, expected", [
    (0, "", False),
    (0, "pg_restore: warning: something minor", False),
    (1, TIMEOUT_ERROR, False),  # newer tools on an older server: seen on PostgreSQL 16 with newer client tools
    (1, f"{TIMEOUT_ERROR}
pg_restore: warning: errors ignored on restore: 1", False),
    (1, REAL_ERROR, True),
    (1, f"{TIMEOUT_ERROR}
{REAL_ERROR}", True),
    (1, "pg_restore: error: connection to server failed", True),  # no query ran at all
    (1, "", True),
])
def test_only_harmless_errors_are_allowed_in_a_restore(returncode, errors, expected):
    assert restore_failed(returncode, errors) is expected
