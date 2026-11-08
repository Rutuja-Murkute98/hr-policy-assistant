"""Check that a database keeps organisations apart, by trying to read across them as the app does.

    python -m ops.check_database_security
    python -m ops.check_database_security --env CLOUD_DATABASE_URL --report evaluation/reports/neon_security.json

Hosted databases differ in what their roles may do, and the role that owns the tables skips Row-Level Security, so
the app switches to its own helpdesk_app role for every tenant transaction. This checks that this role can't skip
it, then reads every protected table as that role, for each organisation, for none and for one that doesn't exist,
and counts any row it shouldn't see. Everything runs inside transactions that are rolled back, so
nothing is changed. The address is read from the environment, so the password never appears in shell history.
"""
import argparse
import json
import os
import secrets
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import DBAPIError

from app import config
from app.rag.store import get_database_uri, sqlalchemy_url
from app.tenancy import APP_ROLE, CHECKPOINT_TABLES, OWNER_ONLY_TABLES, enter_tenant, verify_tenant_policies

ONLY_APP_ROLE = f"SELECT set_config('role', '{APP_ROLE}', true)"
PROTECTED_TABLES = (
    "SELECT tablename FROM pg_policies WHERE schemaname = 'public' AND policyname = 'tenant_isolation' "
    "ORDER BY tablename"
)


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


def other_tenant_condition(table: str, columns: set[str]) -> str | None:
    """SQL that's true for a row of another tenant than the one set, or None if the table can't say."""
    if table == "langchain_pg_embedding":
        return f"COALESCE(cmetadata->>'tenant_id', '{config.TENANT_ID}') <> current_setting('app.tenant_id', true)"
    if table in CHECKPOINT_TABLES:
        return "split_part(thread_id, ':', 1) <> current_setting('app.tenant_id', true)"
    if table == "organisations":
        return "id <> current_setting('app.tenant_id', true)"
    if "tenant_id" in columns:
        return "tenant_id <> current_setting('app.tenant_id', true)"
    return None


def columns_of(connection: Connection, table: str) -> set[str]:
    return set(connection.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = :table"
    ), {"table": table}).scalars())


def check_role(connection: Connection) -> list[Check]:
    role = connection.execute(text(
        "SELECT rolsuper, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname = :role"), {"role": APP_ROLE}).first()
    if role is None:
        return [Check(f"{APP_ROLE} role exists", False, "missing: run the migrations")]
    connected = connection.execute(text(
        "SELECT current_user, pg_has_role(current_user, :role, 'MEMBER') AS member, "
        "(SELECT rolbypassrls OR rolsuper FROM pg_roles WHERE rolname = current_user) AS skips"
    ), {"role": APP_ROLE}).one()
    owned = connection.execute(text(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND pg_get_userbyid(c.relowner) = :role"
    ), {"role": APP_ROLE}).scalars().all()
    return [
        Check(f"{APP_ROLE} role exists", True),
        Check(f"{APP_ROLE} can't skip Row-Level Security", not role.rolsuper and not role.rolbypassrls,
              f"superuser={role.rolsuper}, bypassrls={role.rolbypassrls}"),
        Check(f"{APP_ROLE} can't sign in by itself", not role.rolcanlogin, f"login={role.rolcanlogin}"),
        Check(f"{APP_ROLE} owns no tables", not owned,
              "a table's owner skips its policies" + (f": {', '.join(owned)}" if owned else "")),
        Check(f"The connected role can switch to {APP_ROLE}", bool(connected.member),
              f"connected as {connected.current_user}; it can skip Row-Level Security itself: {bool(connected.skips)}, "
              "which is why the app switches role"),
    ]


def check_policies(connection: Connection) -> Check:
    try:
        verify_tenant_policies(connection)
    except RuntimeError as error:
        return Check("Every table with a tenant column has a policy", False, str(error))
    return Check("Every table with a tenant column has a policy", True)


def count_visible(connection: Connection, table: str, condition: str | None) -> int:
    where = f" WHERE {condition}" if condition else ""
    return connection.execute(text(f"SELECT count(*) FROM {table}{where}")).scalar_one()


def check_reads(engine, tables: list[str], tenants: list[str]) -> list[Check]:
    """Read every protected table as the app, and count rows that belong to someone else."""
    checks = []
    with engine.connect() as connection:
        conditions = {table: other_tenant_condition(table, columns_of(connection, table)) for table in tables}
    stranger = f"rls_check_{secrets.token_hex(4)}"  # an organisation that doesn't exist sees nothing at all
    for label, tenant in [*((f"organisation {tenant}", tenant) for tenant in tenants), ("no organisation", None),
                          ("an unknown organisation", stranger)]:
        leaks, denied = [], []
        for table in tables:
            with engine.connect() as connection, connection.begin() as transaction:
                if tenant is None:
                    connection.execute(text(ONLY_APP_ROLE))
                else:
                    enter_tenant(connection, tenant)
                # Another tenant's rows, or any row at all when no real organisation is set
                condition = conditions[table] if tenant in tenants else None
                try:
                    count = count_visible(connection, table, condition)
                except DBAPIError as error:
                    if "permission denied" not in str(error.orig).lower():
                        raise
                    count = None
                transaction.rollback()
            if count is None:
                denied.append(table)
            elif count:
                leaks.append(f"{table} ({count})")
        unexpected_access = [table for table in OWNER_ONLY_TABLES if table in tables and table not in denied]
        detail = f"{len(tables) - len(denied)} tables read, {len(denied)} refused"
        if leaks:
            detail += f"; rows from elsewhere: {', '.join(leaks)}"
        if unexpected_access:
            detail += f"; readable but should be refused: {', '.join(unexpected_access)}"
        unexpected_denials = [table for table in denied if table not in OWNER_ONLY_TABLES]
        if unexpected_denials:
            detail += f"; refused, though the app needs them: {', '.join(unexpected_denials)}"
        expected = "only its own rows are visible" if tenant in tenants else "no rows are visible"
        checks.append(Check(f"As {label}, {expected}",
                            not leaks and not unexpected_access and not unexpected_denials, detail))
    return checks


def run(database_uri: str) -> list[Check]:
    engine = create_engine(sqlalchemy_url(database_uri))
    try:
        with engine.connect() as connection:
            checks = check_role(connection)
            checks.append(check_policies(connection))
            tables = list(connection.execute(text(PROTECTED_TABLES)).scalars())
            tenants = list(connection.execute(text("SELECT id FROM organisations ORDER BY id")).scalars())
        if not checks[0].passed:
            return checks
        checks.append(Check("Protected tables found", bool(tables),
                            f"{len(tables)} tables, {len(tenants)} organisations"))
        checks += check_reads(engine, tables, tenants)
    finally:
        engine.dispose()
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default="", help="the environment variable holding the database address "
                                                  "(default: DATABASE_URL, or the local database)")
    parser.add_argument("--report", type=Path, help="save the results as JSON here")
    args = parser.parse_args(argv)
    uri = os.getenv(args.env, "") if args.env else get_database_uri()
    if not uri:
        parser.error(f"{args.env} isn't set")

    checks = run(uri)
    for check in checks:
        print(f"[{'PASS' if check.passed else 'FAIL'}] {check.name}" + (f": {check.detail}" if check.detail else ""))
    passed = all(check.passed for check in checks)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({"passed": passed, "checks": [asdict(check) for check in checks]},
                                          indent=2) + "\n", encoding="utf-8")
    print("All checks passed." if passed else "Some checks failed.")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
