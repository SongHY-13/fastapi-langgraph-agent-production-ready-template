"""Track finalized consultation outcomes for per-turn advisory rules."""

import sqlalchemy as sa

from alembic import op

revision = "consultation_outcome_v04"
down_revision = "consultation_delivery_v04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the internal outcome column."""
    op.add_column("consultation_generations", sa.Column("turn_outcome", sa.String()))


def downgrade() -> None:
    """Remove the internal outcome column."""
    op.drop_column("consultation_generations", "turn_outcome")
