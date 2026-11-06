"""Add stored conversations, approvals, the tool audit log and the mock HR system's records.

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def created_at() -> sa.Column:
    return sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column("summarised_messages", sa.Integer, nullable=False, server_default="0"),
        created_at(),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_conversations_owner", "conversations", ["tenant_id", "user_id", "updated_at"])

    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("conversation_id", sa.Text, sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        created_at(),
    )
    op.create_index("ix_messages_conversation", "messages", ["tenant_id", "conversation_id", "id"])

    op.create_table(
        "pending_actions",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("conversation_id", sa.Text),
        sa.Column("run_id", sa.Text, nullable=False),
        sa.Column("tool", sa.Text, nullable=False),
        sa.Column("arguments", JSONB, nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        created_at(),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_pending_actions_owner", "pending_actions", ["tenant_id", "user_id", "status"])

    op.create_table(
        "tool_audit_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("conversation_id", sa.Text),
        sa.Column("run_id", sa.Text, nullable=False),
        sa.Column("tool", sa.Text, nullable=False),
        sa.Column("arguments", JSONB, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("approval_id", sa.Text),
        sa.Column("duration_ms", sa.Integer, nullable=False),
        created_at(),
    )
    op.create_index("ix_tool_audit_log_owner", "tool_audit_log", ["tenant_id", "user_id", "id"])

    op.create_table(
        "hr_leave_requests",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("employee_id", sa.Text, nullable=False),
        sa.Column("leave_type", sa.Text, nullable=False),
        sa.Column("start_date", sa.Date, nullable=False),
        sa.Column("end_date", sa.Date, nullable=False),
        sa.Column("days", sa.Integer, nullable=False),
        sa.Column("reason", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.Text, nullable=False, server_default="submitted"),
        sa.Column("idempotency_key", sa.Text, nullable=False),
        created_at(),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_leave_request_once"),
    )

    op.create_table(
        "hr_tickets",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("employee_id", sa.Text, nullable=False),
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="open"),
        sa.Column("idempotency_key", sa.Text, nullable=False),
        created_at(),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_ticket_once"),
    )


def downgrade() -> None:
    for table in ("hr_tickets", "hr_leave_requests", "tool_audit_log", "pending_actions", "messages", "conversations"):
        op.drop_table(table)
