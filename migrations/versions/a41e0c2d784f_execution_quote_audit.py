"""Preserve the quote and cost assumptions used by new simulator fills."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a41e0c2d784f"
down_revision = "c414a6b27e01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("executions", sa.Column("market_data_id", sa.UUID(), nullable=True))
    op.add_column("executions", sa.Column("simulation_details", postgresql.JSONB(), nullable=True))
    op.create_foreign_key(
        "fk_executions_market_data_id", "executions", "market_data",
        ["market_data_id"], ["id"],
    )


def downgrade() -> None:
    raise RuntimeError("Downgrade würde unveränderliche Ausführungs-Auditdaten löschen")
