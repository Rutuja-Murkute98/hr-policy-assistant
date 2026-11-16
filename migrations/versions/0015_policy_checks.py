"""Checks after a policy is indexed: its review date, what changed, and clashes with other policies. See D60.

document_versions gets the review date and a summary of what changed from the version before. policy_documents gets the
current review date, for the admin screen's reminder. policy_pairs keeps the model's verdict on each pair of sections it
compared, against hashes of both texts, so no pair is asked about twice, and HR's decision on each clash.

Revision ID: 0015
Revises: 0014
"""
import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

APP_ROLE = "helpdesk_app"


def upgrade() -> None:
    op.add_column("document_versions", sa.Column("review_date", sa.Date))
    op.add_column("document_versions", sa.Column("change_summary", sa.Text))
    op.add_column("policy_documents", sa.Column("review_date", sa.Date))
    op.create_table(
        "policy_pairs",
        sa.Column("tenant_id", sa.Text, primary_key=True),
        sa.Column("chunk_a", sa.Text, primary_key=True),  # the two ids in sorted order, so a pair has one row
        sa.Column("chunk_b", sa.Text, primary_key=True),
        sa.Column("hash_a", sa.Text, nullable=False),
        sa.Column("hash_b", sa.Text, nullable=False),
        sa.Column("file_a", sa.Text, nullable=False),
        sa.Column("file_b", sa.Text, nullable=False),
        sa.Column("verdict", sa.Text, nullable=False),  # clash or fine
        sa.Column("explanation", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.Text, nullable=False, server_default="open"),  # open, resolved or dismissed
        sa.Column("decided_by", sa.Text, nullable=False, server_default=""),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON policy_pairs TO {APP_ROLE}")
    op.execute("ALTER TABLE policy_pairs ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON policy_pairs "
               "USING (tenant_id = current_setting('app.tenant_id', true)) "
               "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))")


def downgrade() -> None:
    op.drop_table("policy_pairs")
    op.drop_column("policy_documents", "review_date")
    op.drop_column("document_versions", "change_summary")
    op.drop_column("document_versions", "review_date")
