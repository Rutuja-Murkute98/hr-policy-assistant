"""Sources that policies sync from: a watched folder, Google Drive or SharePoint. See docs/DECISIONS.md, D59.

knowledge_sources holds each organisation's sources, with how often they sync and how the last sync went. source_files
remembers each file seen at a source, with a version tag from the source, so a sync only fetches what changed, and the
policy it became, so a file removed at the source can stop being answered from.

Revision ID: 0014
Revises: 0013
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

APP_ROLE = "helpdesk_app"
POLICY = ("CREATE POLICY tenant_isolation ON {table} USING (tenant_id = current_setting('app.tenant_id', true)) "
          "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))")


def upgrade() -> None:
    op.create_table(
        "knowledge_sources",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("kind", sa.Text, nullable=False),  # folder, google_drive or sharepoint
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("settings", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("every_minutes", sa.Integer, nullable=False, server_default="60"),
        sa.Column("keep_removed", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("status", sa.Text, nullable=False, server_default="never"),  # never, running, ok or failed
        sa.Column("last_error", sa.Text),
        sa.Column("last_summary", postgresql.JSONB),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.Text, nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_knowledge_sources_tenant", "knowledge_sources", ["tenant_id"])
    op.create_table(
        "source_files",
        sa.Column("tenant_id", sa.Text, primary_key=True),
        sa.Column("source_id", sa.Text, sa.ForeignKey("knowledge_sources.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("file_key", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("version_tag", sa.Text, nullable=False),
        sa.Column("policy_file", sa.Text, nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    for table in ("knowledge_sources", "source_files"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(POLICY.format(table=table))


def downgrade() -> None:
    op.drop_table("source_files")
    op.drop_table("knowledge_sources")
