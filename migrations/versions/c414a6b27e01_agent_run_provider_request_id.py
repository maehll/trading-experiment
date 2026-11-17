"""Persist provider request IDs for auditable agent runs.

Revision ID: c414a6b27e01
Revises: 932d124e9880
"""

from alembic import op
import sqlalchemy as sa

revision = "c414a6b27e01"
down_revision = "932d124e9880"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("provider_request_id", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_runs", "provider_request_id")
