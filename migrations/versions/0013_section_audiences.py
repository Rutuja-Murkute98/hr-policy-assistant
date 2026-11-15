"""Record who each policy section applies to: some bands, locations, employment types, or probation.

One row per organisation and chunk, with a hash of the text it was tagged from, so an edited section counts as untagged.
An empty applies_to means everyone. HR's corrections are kept apart from the model's, and the model never overwrites
them. See docs/DECISIONS.md, D55.

Revision ID: 0013
Revises: 0012
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

APP_ROLE = "helpdesk_app"


def upgrade() -> None:
    op.create_table(
        "section_audiences",
        sa.Column("tenant_id", sa.Text, primary_key=True),
        sa.Column("chunk_id", sa.Text, primary_key=True),
        sa.Column("source_file", sa.Text, nullable=False),
        sa.Column("text_hash", sa.Text, nullable=False),
        sa.Column("applies_to", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("source", sa.Text, nullable=False),  # "model" or "hr"
        sa.Column("updated_by", sa.Text, nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_section_audiences_file", "section_audiences", ["tenant_id", "source_file"])
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON section_audiences TO {APP_ROLE}")
    op.execute("ALTER TABLE section_audiences ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON section_audiences "
               "USING (tenant_id = current_setting('app.tenant_id', true)) "
               "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))")


def downgrade() -> None:
    op.drop_table("section_audiences")
