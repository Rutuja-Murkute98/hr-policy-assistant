"""The database security check, on a real PostgreSQL with pgvector. No model API is called."""
import pytest
from sqlalchemy import create_engine, text

from app import config
from app.config import Tenant
from app.db import get_engine
from app.identity import Organisations
from app.rag.store import run_migrations, sqlalchemy_url
from app.tenancy import APP_ROLE
from ops.check_database_security import run

pytestmark = pytest.mark.integration

OTHER = Tenant(id="sec_check_other", company_name="Other", legal_name="Other Ltd", hr_email="hr@other.example",
               product_name="Other HR", logo_initials="O")


@pytest.fixture(scope="module")
def migrated(database_uri):
    run_migrations(database_uri)
    engine = get_engine(database_uri)
    organisations = Organisations(engine)
    organisations.ensure(config.tenant)
    organisations.ensure(OTHER)
    engine.dispose()
    owner = create_engine(sqlalchemy_url(database_uri))
    yield database_uri, owner
    with owner.begin() as connection:
        connection.execute(text("DELETE FROM organisations WHERE id = :id"), {"id": OTHER.id})
    owner.dispose()


def results(uri) -> dict[str, bool]:
    return {check.name: check.passed for check in run(uri)}


def test_a_migrated_database_passes_every_check(migrated):
    uri, _ = migrated
    checks = run(uri)
    failed = [f"{check.name}: {check.detail}" for check in checks if not check.passed]
    assert not failed
    names = [check.name for check in checks]
    assert f"As organisation {OTHER.id}, only its own rows are visible" in names
    assert "As no organisation, no rows are visible" in names


def test_another_organisations_rows_are_never_counted_as_a_pass(migrated):
    """Rows for both organisations exist, and each one still sees only its own."""
    uri, owner = migrated
    with owner.begin() as connection:
        for tenant in (config.TENANT_ID, OTHER.id):
            connection.execute(text(
                "INSERT INTO conversations (id, tenant_id, user_id) VALUES (:id, :tenant, 'u1')"
            ), {"id": f"sec-check-{tenant}", "tenant": tenant})
    try:
        assert all(results(uri).values())
    finally:
        with owner.begin() as connection:
            connection.execute(text("DELETE FROM conversations WHERE id LIKE 'sec-check-%'"))


def test_a_tenant_table_without_a_policy_fails(migrated):
    uri, owner = migrated
    with owner.begin() as connection:
        connection.execute(text("CREATE TABLE sec_check_unprotected (id int, tenant_id text)"))
    try:
        assert not results(uri)["Every table with a tenant column has a policy"]
    finally:
        with owner.begin() as connection:
            connection.execute(text("DROP TABLE sec_check_unprotected"))


def test_an_app_role_that_can_skip_row_level_security_fails(migrated):
    uri, owner = migrated
    with owner.begin() as connection:
        connection.execute(text(f"ALTER ROLE {APP_ROLE} BYPASSRLS"))
    try:
        checks = results(uri)
        assert not checks[f"{APP_ROLE} can't skip Row-Level Security"]
        assert not checks["As no organisation, no rows are visible"]
    finally:
        with owner.begin() as connection:
            connection.execute(text(f"ALTER ROLE {APP_ROLE} NOBYPASSRLS"))


def test_the_app_role_reading_password_hashes_fails(migrated):
    uri, owner = migrated
    with owner.begin() as connection:
        connection.execute(text(f"GRANT SELECT ON user_credentials TO {APP_ROLE}"))
    try:
        detail = next(check.detail for check in run(uri) if check.name.startswith("As no organisation"))
        assert "readable but should be refused: user_credentials" in detail
    finally:
        with owner.begin() as connection:
            connection.execute(text(f"REVOKE SELECT ON user_credentials FROM {APP_ROLE}"))
