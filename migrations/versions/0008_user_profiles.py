"""Add profiles: a person's job details and photo. Their name stays in users.display_name.

Revision ID: 0008
Revises: 0007
"""
import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

APP_ROLE = "helpdesk_app"


def upgrade() -> None:
    op.create_table(
        "user_profiles",
        sa.Column("user_id", sa.Text, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("tenant_id", sa.Text, sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("job_title", sa.Text, nullable=False, server_default=""),
        sa.Column("department", sa.Text, nullable=False, server_default=""),
        sa.Column("location", sa.Text, nullable=False, server_default=""),
        sa.Column("photo", sa.LargeBinary),
        sa.Column("photo_type", sa.Text),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON user_profiles TO {APP_ROLE}")
    op.execute("ALTER TABLE user_profiles ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON user_profiles "
               "USING (tenant_id = current_setting('app.tenant_id', true)) "
               "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))")


def downgrade() -> None:
    op.drop_table("user_profiles")
