"""Cache checked answers to repeated first questions.

answer_cache keeps one answer per organisation, answer model and question, keyed by a hash of the question's
masked, normalised text. Each row records the documents it was written from, as a hash of policy_documents, so an
upload, change or removal means the row is never served again. Only answers that passed the citation check are
stored, and only for policy questions, so no personal data is kept. Rows are removed after the conversation
retention period.

Revision ID: 0010
Revises: 0009
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

APP_ROLE = "helpdesk_app"


def upgrade() -> None:
    op.create_table(
        "answer_cache",
        sa.Column("tenant_id", sa.Text, primary_key=True),
        sa.Column("model", sa.Text, primary_key=True),
        sa.Column("question_hash", sa.Text, primary_key=True),
        sa.Column("documents_hash", sa.Text, nullable=False),
        sa.Column("answer", postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_answer_cache_age", "answer_cache", ["tenant_id", "created_at"])
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON answer_cache TO {APP_ROLE}")
    op.execute("ALTER TABLE answer_cache ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON answer_cache "
               "USING (tenant_id = current_setting('app.tenant_id', true)) "
               "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))")


def downgrade() -> None:
    op.drop_table("answer_cache")
