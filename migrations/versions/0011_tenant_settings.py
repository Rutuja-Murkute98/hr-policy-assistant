"""Store each organisation's own settings.

tenant_settings holds one row per organisation that changed a setting: only the settings it changed, as a JSON
document checked by app/org_settings.py. Anything it didn't change comes from the deployment. See docs/DECISIONS.md, D52.

Revision ID: 0011
Revises: 0010
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

APP_ROLE = "helpdesk_app"


def upgrade() -> None:
    op.create_table(
        "tenant_settings",
        sa.Column("tenant_id", sa.Text, sa.ForeignKey("organisations.id"), primary_key=True),
        sa.Column("settings", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_by", sa.Text, nullable=False, server_default=""),
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_settings TO {APP_ROLE}")
    op.execute("ALTER TABLE tenant_settings ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON tenant_settings "
               "USING (tenant_id = current_setting('app.tenant_id', true)) "
               "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))")


def downgrade() -> None:
    op.drop_table("tenant_settings")
