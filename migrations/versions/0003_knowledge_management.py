"""Add document versions, indexing jobs, feedback, unanswered questions and the embedding model record.

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def created_at() -> sa.Column:
    return sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "document_versions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("collection_name", sa.Text, nullable=False),
        sa.Column("source_file", sa.Text, nullable=False),
        sa.Column("version_number", sa.Integer, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("category", sa.Text, nullable=False),
        sa.Column("version_label", sa.Text),
        sa.Column("effective_date", sa.Date),
        sa.Column("original_name", sa.Text, nullable=False),
        sa.Column("text_hash", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("uploaded_by", sa.Text, nullable=False),
        created_at(),
        sa.Column("superseded_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("tenant_id", "collection_name", "source_file", "version_number", name="uq_document_version"),
    )
    op.create_index("ix_document_versions_hash", "document_versions", ["tenant_id", "collection_name", "text_hash"])

    op.create_table(
        "indexing_jobs",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("collection_name", sa.Text, nullable=False),
        sa.Column("source_file", sa.Text, nullable=False),
        sa.Column("version_id", sa.BigInteger, sa.ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("to_embed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("embedded", sa.Integer, nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error", sa.Text),
        sa.Column("created_by", sa.Text, nullable=False),
        created_at(),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_indexing_jobs_tenant", "indexing_jobs", ["tenant_id", "status", "created_at"])

    op.create_table(
        "feedback",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("message_id", sa.BigInteger, sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rating", sa.SmallInteger, nullable=False),
        sa.Column("comment", sa.Text, nullable=False, server_default=""),
        created_at(),
        sa.UniqueConstraint("tenant_id", "user_id", "message_id", name="uq_feedback_once"),
    )

    op.create_table(
        "unanswered_questions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        created_at(),
    )
    op.create_index("ix_unanswered_tenant", "unanswered_questions", ["tenant_id", "created_at"])

    op.create_table(
        "embedding_models",
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("collection_name", sa.Text, nullable=False),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("dimension", sa.Integer, nullable=False),
        created_at(),
        sa.PrimaryKeyConstraint("tenant_id", "collection_name"),
    )


def downgrade() -> None:
    for table in ("embedding_models", "unanswered_questions", "feedback", "indexing_jobs", "document_versions"):
        op.drop_table(table)
