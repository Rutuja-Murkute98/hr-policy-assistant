"""Keep each organisation's data apart at the database level.

Application queries already filter by tenant. As a second, independent wall, tenant work runs as the
helpdesk_app role with app.tenant_id set for that transaction. PostgreSQL's Row-Level Security then
hides other tenants' rows even if a query forgets its filter. With no tenant set, no rows are visible.

The role and setting last for one transaction, so a pooled connection can't carry them to another
request. That also makes this safe behind a transaction-pooling proxy, such as Neon's.
"""
import re
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, Engine, text

APP_ROLE = "helpdesk_app"
SAFE_TENANT_ID = re.compile(r"^[a-z0-9_-]+$")
SET_ROLE = text(f"SET LOCAL ROLE {APP_ROLE}")
SET_TENANT = text("SELECT set_config('app.tenant_id', :tenant_id, true)")
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_writes", "checkpoint_blobs")
SECURITY_LOCK = 72_004  # any fixed number: it stops two server processes changing security at once
THREAD_TENANT = "split_part(thread_id, ':', 1) = current_setting('app.tenant_id', true)"


def check_tenant_id(tenant_id: str) -> str:
    """Tenant ids go into run ids and SQL policies, so only safe characters are allowed."""
    if not SAFE_TENANT_ID.fullmatch(tenant_id or ""):
        raise ValueError("A tenant id can only use lowercase letters, numbers, _ and -")
    return tenant_id


def enter_tenant(connection, tenant_id: str) -> None:
    """Switch this transaction to the app role and one tenant. Works on a connection or a session."""
    connection.execute(SET_ROLE)
    connection.execute(SET_TENANT, {"tenant_id": tenant_id})


class TenantEngine:
    """Looks like a SQLAlchemy engine, but everything it runs is limited to one tenant."""

    def __init__(self, engine: Engine, tenant_id: str):
        self.engine = engine
        self.tenant_id = check_tenant_id(tenant_id)

    @contextmanager
    def begin(self) -> Iterator[Connection]:
        with self.engine.begin() as connection:
            enter_tenant(connection, self.tenant_id)
            yield connection

    # A read needs a transaction too, because the role and tenant only last for one
    connect = begin


def tenant_scoped(engine, tenant_id: str) -> TenantEngine:
    """Limit an engine to one tenant. An engine already limited to another tenant is refused."""
    if isinstance(engine, TenantEngine):
        if engine.tenant_id != tenant_id:
            raise ValueError("This database connection belongs to another tenant")
        return engine
    return TenantEngine(engine, tenant_id)


def secure_library_tables(connection, default_tenant: str) -> None:
    """Protect the tables LangChain and LangGraph create themselves, which migrations can't reach first.

    Each table is only changed if it has no policy yet, so a normal start takes no strong locks.
    """
    connection.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": SECURITY_LOCK})
    tenant = check_tenant_id(default_tenant)
    # A chunk with no tenant was stored before tenants existed, so it belongs to the first organisation
    chunk_tenant = f"COALESCE(cmetadata->>'tenant_id', '{tenant}') = current_setting('app.tenant_id', true)"
    for table, condition in [("langchain_pg_embedding", chunk_tenant),
                             *((table, THREAD_TENANT) for table in CHECKPOINT_TABLES)]:
        _protect(connection, table, condition)
    for table in ("langchain_pg_collection", "checkpoint_migrations"):
        if connection.execute(text("SELECT to_regclass(:table)"), {"table": table}).scalar():
            connection.execute(text(f"GRANT SELECT ON {table} TO {APP_ROLE}"))


def _protect(connection, table: str, condition: str) -> None:
    if not connection.execute(text("SELECT to_regclass(:table)"), {"table": table}).scalar():
        return
    exists = connection.execute(text(
        "SELECT 1 FROM pg_policies WHERE tablename = :table AND policyname = 'tenant_isolation'"
    ), {"table": table}).first()
    if exists:
        return
    connection.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}"))
    connection.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    connection.execute(text(f"CREATE POLICY tenant_isolation ON {table} USING ({condition}) WITH CHECK ({condition})"))
