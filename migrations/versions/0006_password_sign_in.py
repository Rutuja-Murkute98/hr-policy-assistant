"""Add password sign-in: hashed passwords, one-time set-up codes and sessions that last between visits.

These tables get Row-Level Security like every tenant table, but the app's helpdesk_app role gets no
access at all. Only the few sign-in lookups, which run as the database owner, can read them, so no
tenant query or tool can ever reach a password hash.

Revision ID: 0006
Revises: 0005
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

TABLES = ("user_credentials", "user_sessions")


def upgrade() -> None:
    op.create_table(
        "user_credentials",
        sa.Column("user_id", sa.Text, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("tenant_id", sa.Text, sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("password_hash", sa.Text),  # empty until the person sets a password
        sa.Column("setup_code_hash", sa.String(64)),
        sa.Column("setup_code_expires_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("user_id", sa.Text, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.Text, sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_user_sessions_user", "user_sessions", ["user_id"])
    op.create_index("ix_user_sessions_expires", "user_sessions", ["expires_at"])
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} "
                   "USING (tenant_id = current_setting('app.tenant_id', true)) "
                   "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))")


def downgrade() -> None:
    op.drop_table("user_sessions")
    op.drop_table("user_credentials")
