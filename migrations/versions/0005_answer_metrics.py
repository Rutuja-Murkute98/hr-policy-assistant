"""Add per-answer metrics. They hold counts and times, never question or answer text.

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "answer_metrics",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("total_ms", sa.Float, nullable=False),
        sa.Column("failed", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("intent", sa.Text, nullable=False, server_default=""),
        sa.Column("answered", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("abstain_reason", sa.Text, nullable=False, server_default=""),
        sa.Column("tool", sa.Text, nullable=False, server_default=""),
        sa.Column("tool_status", sa.Text, nullable=False, server_default=""),
        sa.Column("embedding_ms", sa.Float, nullable=False, server_default="0"),
        sa.Column("retrieval_ms", sa.Float, nullable=False, server_default="0"),
        sa.Column("llm_ms", sa.Float, nullable=False, server_default="0"),
        sa.Column("llm_calls", sa.Integer, nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("estimated_cost_usd", sa.Float, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_answer_metrics_tenant", "answer_metrics", ["tenant_id", "created_at"])
    op.execute("GRANT SELECT, INSERT ON answer_metrics TO helpdesk_app")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE answer_metrics_id_seq TO helpdesk_app")
    op.execute("ALTER TABLE answer_metrics ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON answer_metrics "
               "USING (tenant_id = current_setting('app.tenant_id', true)) "
               "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))")


def downgrade() -> None:
    op.drop_table("answer_metrics")
