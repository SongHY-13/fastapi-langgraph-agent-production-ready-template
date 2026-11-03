"""Add consultation worker leases, replay retention and subscriptions."""

import sqlalchemy as sa

from alembic import op

revision = "consultation_delivery_v04"
down_revision = "consultation_v04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Persist lease credentials and independently stoppable streams."""
    op.add_column("consultation_generations", sa.Column("lease_token", sa.String()))
    op.add_column("consultation_generations", sa.Column("lease_until", sa.DateTime(timezone=True)))
    op.add_column("consultation_generations", sa.Column("finished_at", sa.DateTime(timezone=True)))
    op.add_column(
        "consultation_generations", sa.Column("state_version", sa.BigInteger(), nullable=False, server_default="0")
    )
    op.add_column(
        "consultation_generations", sa.Column("replay_floor", sa.BigInteger(), nullable=False, server_default="0")
    )
    op.execute("""
        UPDATE consultation_generations g SET finished_at = COALESCE(
            (SELECT MAX(created_at) FROM consultation_events e WHERE e.generation_id = g.id), g.created_at)
        WHERE g.status IN ('completed', 'cancelled', 'failed')
    """)
    op.create_table(
        "consultation_subscriptions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "generation_id",
            sa.String(),
            sa.ForeignKey("consultation_generations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    """Remove lease and delivery metadata."""
    op.drop_table("consultation_subscriptions")
    for field in ("replay_floor", "state_version", "finished_at", "lease_until", "lease_token"):
        op.drop_column("consultation_generations", field)
