"""Add each person's choice of whether their chats are saved.

Revision ID: 0007
Revises: 0006
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

APP_ROLE = "helpdesk_app"


def upgrade() -> None:
    op.create_table(
        "user_preferences",
        sa.Column("tenant_id", sa.Text, primary_key=True),
        sa.Column("user_id", sa.Text, primary_key=True),
        sa.Column("save_chat_history", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON user_preferences TO {APP_ROLE}")
    op.execute("ALTER TABLE user_preferences ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON user_preferences "
               "USING (tenant_id = current_setting('app.tenant_id', true)) "
               "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))")


def downgrade() -> None:
    op.drop_table("user_preferences")
