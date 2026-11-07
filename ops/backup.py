"""Back up the database to a file, and restore it into an empty database.

    python -m ops.backup create backups/2026-11-07.dump
    python -m ops.backup restore backups/2026-11-07.dump --target-env RESTORE_DATABASE_URL

A backup is a full copy of the data, unencrypted, so keep it somewhere as private as the database itself.
A list of row counts is saved beside it, and a restore is checked against that list. Addresses are read from
the environment and passwords are passed privately, so they don't appear in shell history or process lists.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
from sqlalchemy import create_engine, text

from app import config
from app.rag.store import get_database_uri, sqlalchemy_url
from app.tenancy import grant_app_role, verify_tenant_policies

HARMLESS_RESTORE_ERROR = "unrecognized configuration parameter"


class BackupError(Exception):
    """A backup or restore can't go ahead. The message says why."""


def _major_version(binary: Path | str) -> int:
    output = subprocess.run([str(binary), "--version"], capture_output=True, text=True, check=True).stdout
    match = re.search(r"(\d+)(?:\.\d+)?", output)
    if not match:
        raise BackupError(f"Couldn't read the version of {binary}")
    return int(match[1])


def _server_major(uri: str) -> int:
    with psycopg.connect(uri) as connection:
        return int(connection.execute("SHOW server_version_num").fetchone()[0]) // 10000  # type: ignore[index]


def find_tool(name: str, uri: str) -> str:
    """Find a tool at least as new as the server: pg_dump refuses to back up a newer server."""
    needed = _server_major(uri)
    executable = f"{name}.exe" if sys.platform == "win32" else name
    candidates = [Path(os.environ["PG_BIN_DIR"]) / executable] if os.environ.get("PG_BIN_DIR") else []
    if shutil.which(name):
        candidates.append(Path(shutil.which(name) or name))
    try:
        import pixeltable_pgserver

        root = Path(pixeltable_pgserver.__file__).parent
        candidates += sorted(root.glob(f"pginstall*/bin/{executable}"))
    except ImportError:
        pass  # only installed for running on your own PC
    for candidate in candidates:
        if candidate.exists() and _major_version(candidate) >= needed:
            return str(candidate)
    raise BackupError(f"{name} for PostgreSQL {needed} or newer wasn't found. Install the PostgreSQL client tools, "
                      "or set PG_BIN_DIR to their folder.")


def _tool_arguments(uri: str) -> tuple[str, dict]:
    """Split a database address, so its password goes in the tool's private environment."""
    parts = urlsplit(uri)
    environment = dict(os.environ)
    if parts.password:
        environment["PGPASSWORD"] = parts.password
    host = parts.hostname or ""
    host_part = f"{parts.username}@{host}" if parts.username else host
    if parts.port:
        host_part += f":{parts.port}"
    return urlunsplit((parts.scheme, host_part, parts.path, parts.query, "")), environment


def restore_failed(returncode: int, errors: str) -> bool:
    """Decide if a restore failed. Newer tools set options an older server doesn't have, which does no harm."""
    if returncode == 0:
        return False
    problems = [line for line in errors.splitlines() if "ERROR:" in line]
    return not problems or any(HARMLESS_RESTORE_ERROR not in line for line in problems)


def _run(arguments: list[str], environment: dict, what: str, password: str | None,
         failed: Callable[[int, str], bool] = lambda returncode, errors: returncode != 0) -> None:
    result = subprocess.run(arguments, capture_output=True, text=True, env=environment)
    if failed(result.returncode, result.stderr):
        detail = result.stderr[-2000:]
        raise BackupError(f"{what} failed: {detail.replace(password, '***') if password else detail}")


def table_counts(uri: str) -> dict[str, int]:
    with psycopg.connect(uri) as connection:
        tables = [row[0] for row in connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename").fetchall()]
        return {table: connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]  # type: ignore[index]
                for table in tables}


def manifest_path(backup: Path) -> Path:
    return backup.with_name(f"{backup.name}.manifest.json")


def create_backup(backup: Path, uri: str) -> dict[str, int]:
    backup.parent.mkdir(parents=True, exist_ok=True)
    counts = table_counts(uri)
    address, environment = _tool_arguments(uri)
    # Owners and privileges differ between hosts, so a restore applies its own
    _run([find_tool("pg_dump", uri), "--format=custom", "--no-owner", "--no-privileges", f"--file={backup}",
          f"--dbname={address}"], environment, "The backup", urlsplit(uri).password)
    manifest_path(backup).write_text(json.dumps(counts, indent=1), encoding="utf-8")
    return counts


def restore_backup(backup: Path, uri: str) -> dict[str, int]:
    """Restore into an empty database, then check the rows, the policies and the app's access."""
    if not backup.exists():
        raise BackupError(f"{backup} doesn't exist")
    with psycopg.connect(uri) as connection:
        if connection.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'").fetchone()[0]:  # type: ignore[index]
            raise BackupError("That database already has tables. A restore only goes into an empty database, "
                              "so it can't overwrite anything.")
    engine = create_engine(sqlalchemy_url(uri))
    with engine.begin() as connection:  # the role belongs to the server, so a backup can't hold it
        connection.execute(text(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'helpdesk_app') THEN "
            "CREATE ROLE helpdesk_app NOLOGIN; END IF; END $$"
        ))
        connection.execute(text("GRANT helpdesk_app TO CURRENT_USER"))
    address, environment = _tool_arguments(uri)
    # It carries on past an error, so a harmless one can't stop it. Anything else fails here, or in the checks below.
    _run([find_tool("pg_restore", uri), "--no-owner", "--no-privileges", f"--dbname={address}", str(backup)],
         environment, "The restore", urlsplit(uri).password, restore_failed)
    with engine.begin() as connection:
        grant_app_role(connection)
    with engine.begin() as connection:
        verify_tenant_policies(connection)
    engine.dispose()

    counts = table_counts(uri)
    expected = manifest_path(backup)
    if expected.exists():
        wanted = json.loads(expected.read_text(encoding="utf-8"))
        different = {table: (wanted.get(table), counts.get(table)) for table in {*wanted, *counts}
                     if wanted.get(table) != counts.get(table)}
        if different:
            raise BackupError(f"The restored row counts don't match the backup: {different}")
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Back up or restore the helpdesk database")
    parser.add_argument("action", choices=["create", "restore"])
    parser.add_argument("file", type=Path)
    parser.add_argument("--target-env", default="", help="for restore: the environment variable holding the address")
    args = parser.parse_args()
    try:
        if args.action == "create":
            counts = create_backup(args.file, get_database_uri())
            print(f"Backed up {len(counts)} tables, with {sum(counts.values()):,} rows, to {args.file}")
            return
        target = os.environ.get(args.target_env, "") if args.target_env else ""
        if not target:
            raise BackupError("Set --target-env to an environment variable holding the empty database's address.")
        if target == config.DATABASE_URL:
            raise BackupError("That's the database the app is using. Restore into an empty one, check it, then switch.")
        counts = restore_backup(args.file, target)
        print(f"Restored and checked {len(counts)} tables, with {sum(counts.values()):,} rows")
    except BackupError as problem:
        sys.exit(str(problem))


if __name__ == "__main__":
    main()
