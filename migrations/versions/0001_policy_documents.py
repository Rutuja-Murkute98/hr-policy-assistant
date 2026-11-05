"""Record each policy document, so a change can be detected from its content hash.

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "policy_documents",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("collection_name", sa.Text, nullable=False),
        sa.Column("source_file", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("category", sa.Text, nullable=False),
        sa.Column("version", sa.Text),
        sa.Column("effective_date", sa.Date),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("chunk_count", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "collection_name", "source_file", name="uq_policy_documents_file"),
    )
    op.create_index("ix_policy_documents_tenant", "policy_documents", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("policy_documents")
