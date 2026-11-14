"""Re-embed an organisation's chunks when the embedding model changes, without stopping search.

reindex_runs tracks one re-index per organisation and target collection: where it came from, how far it got, and
whether it failed. reindex_vectors holds the new vectors until every chunk has one, so the old collection keeps
serving searches, and the switch happens in one transaction. See docs/DECISIONS.md, D54.

Revision ID: 0012
Revises: 0011
"""
import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

APP_ROLE = "helpdesk_app"
POLICY = ("CREATE POLICY tenant_isolation ON {table} USING (tenant_id = current_setting('app.tenant_id', true)) "
          "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))")


def upgrade() -> None:
    op.create_table(
        "reindex_runs",
        sa.Column("tenant_id", sa.Text, primary_key=True),
        sa.Column("to_collection", sa.Text, primary_key=True),
        sa.Column("from_collection", sa.Text, nullable=False),
        sa.Column("from_model", sa.Text, nullable=False),  # provider/model, as the old vectors were made
        sa.Column("to_model", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="running"),  # running, failed or succeeded
        sa.Column("total", sa.Integer, nullable=False, server_default="0"),
        sa.Column("embedded", sa.Integer, nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="1"),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    # The vector type has no fixed size here, as the new model's size isn't known until it's called. On a new
    # database, migrations run before LangChain switches pgvector on, so it's switched on here too.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE TABLE reindex_vectors (tenant_id TEXT NOT NULL, to_collection TEXT NOT NULL, "
               "chunk_id TEXT NOT NULL, text_hash TEXT NOT NULL, embedding vector NOT NULL, "
               "PRIMARY KEY (tenant_id, to_collection, chunk_id))")
    for table in ("reindex_runs", "reindex_vectors"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(POLICY.format(table=table))


def downgrade() -> None:
    op.drop_table("reindex_vectors")
    op.drop_table("reindex_runs")
