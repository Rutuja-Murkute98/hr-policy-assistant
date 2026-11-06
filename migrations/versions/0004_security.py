"""Add organisations, users, API keys and the audit log, and switch on Row-Level Security.

Row-Level Security doesn't apply to a table's owner or a superuser, so the app does its tenant work
as a separate role, helpdesk_app. See app/tenancy.py.

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

APP_ROLE = "helpdesk_app"
TENANT_TABLES = ("policy_documents", "conversations", "messages", "pending_actions", "tool_audit_log",
                 "hr_leave_requests", "hr_tickets", "document_versions", "indexing_jobs", "feedback",
                 "unanswered_questions", "embedding_models", "users", "api_keys", "audit_events")


def created_at() -> sa.Column:
    return sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "organisations",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("company_name", sa.Text, nullable=False),
        sa.Column("legal_name", sa.Text, nullable=False),
        sa.Column("hr_email", sa.Text, nullable=False),
        sa.Column("product_name", sa.Text, nullable=False),
        sa.Column("logo_initials", sa.Text, nullable=False),
        sa.Column("default_category", sa.Text, nullable=False, server_default="general"),
        sa.Column("document_categories", JSONB, nullable=False, server_default="{}"),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        created_at(),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("employee_id", sa.Text),
        sa.Column("display_name", sa.Text, nullable=False, server_default=""),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("oidc_subject", sa.Text),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        created_at(),
        sa.CheckConstraint("role IN ('employee', 'hr_admin', 'platform_admin')", name="ck_users_role"),
    )
    # One email belongs to one organisation, which is how a login finds its tenant
    op.execute("CREATE UNIQUE INDEX uq_users_email ON users (lower(email))")
    op.create_index("ix_users_tenant", "users", ["tenant_id"])

    op.create_table(
        "api_keys",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("key_prefix", sa.Text, nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
        created_at(),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_api_keys_tenant", "api_keys", ["tenant_id"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("target", sa.Text, nullable=False, server_default=""),
        sa.Column("details", JSONB, nullable=False, server_default="{}"),
        created_at(),
    )
    op.create_index("ix_audit_events_tenant", "audit_events", ["tenant_id", "id"])

    op.create_table(
        "rate_limit_hits",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("key", sa.Text, nullable=False),
        sa.Column("hit_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_rate_limit_hits_key", "rate_limit_hits", ["key", "hit_at"])

    op.execute(f"""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            CREATE ROLE {APP_ROLE} NOLOGIN;
        END IF;
    END $$""")
    op.execute(f"GRANT {APP_ROLE} TO CURRENT_USER")  # lets the app switch to this role
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT SELECT ON organisations TO {APP_ROLE}")
    for table in TENANT_TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} "
                   "USING (tenant_id = current_setting('app.tenant_id', true)) "
                   "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
    op.execute("ALTER TABLE organisations ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON organisations "
               "USING (id = current_setting('app.tenant_id', true))")


def downgrade() -> None:
    for table in TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
    for table in ("rate_limit_hits", "audit_events", "api_keys", "users", "organisations"):
        op.drop_table(table)
