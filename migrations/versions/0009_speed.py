"""Time each step of an answer, and cache question embeddings.

step_ms holds milliseconds per workflow step, such as {"understand": 1200, "generate": 2400}, so the slow step can
be found. question_embeddings keeps the vector for a question already asked, keyed by a hash of its masked text,
so asking it again needs no embedding call. Neither holds question or answer text.

Revision ID: 0009
Revises: 0008
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

APP_ROLE = "helpdesk_app"


def upgrade() -> None:
    op.add_column("answer_metrics", sa.Column("step_ms", postgresql.JSONB, nullable=False,
                                              server_default=sa.text("'{}'::jsonb")))
    op.create_table(
        "question_embeddings",
        sa.Column("tenant_id", sa.Text, primary_key=True),
        sa.Column("model", sa.Text, primary_key=True),
        sa.Column("text_hash", sa.Text, primary_key=True),
        sa.Column("embedding", postgresql.ARRAY(sa.Float), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_question_embeddings_age", "question_embeddings", ["tenant_id", "created_at"])
    op.execute(f"GRANT SELECT, INSERT, DELETE ON question_embeddings TO {APP_ROLE}")
    op.execute("ALTER TABLE question_embeddings ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON question_embeddings "
               "USING (tenant_id = current_setting('app.tenant_id', true)) "
               "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))")


def downgrade() -> None:
    op.drop_table("question_embeddings")
    op.drop_column("answer_metrics", "step_ms")
