"""Track the canonical effective answer for each consultation turn."""

import sqlalchemy as sa

from alembic import op

revision = "consultation_current_v04"
down_revision = "consultation_outcome_v04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Backfill canonical effective answers and enforce one per turn."""
    op.add_column("consultation_turns", sa.Column("created_at", sa.DateTime(timezone=True)))
    op.execute(
        """
        UPDATE consultation_turns t SET created_at = g.created_at FROM consultation_generations g
        WHERE g.turn_id = t.id AND g.submission_key = 'initial'
        """
    )
    op.alter_column("consultation_turns", "created_at", nullable=False, server_default=sa.func.clock_timestamp())
    op.add_column(
        "consultation_generations",
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("consultation_generations", sa.Column("regeneration_target", sa.String()))
    op.execute(
        """
        UPDATE consultation_generations SET is_current = true WHERE id IN (
            SELECT DISTINCT ON (turn_id) id FROM consultation_generations
            WHERE turn_outcome = 'effective' AND status = 'completed' AND answer IS NOT NULL
                AND risk IS NULL AND priority = 'normal'
            ORDER BY turn_id, finished_at DESC, id DESC
        )
        """
    )
    op.create_index(
        "uq_consultation_current_answer",
        "consultation_generations",
        ["turn_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )


def downgrade() -> None:
    """Remove canonical answer selection and regeneration linkage."""
    op.drop_index("uq_consultation_current_answer", table_name="consultation_generations")
    op.drop_column("consultation_generations", "regeneration_target")
    op.drop_column("consultation_generations", "is_current")
    op.drop_column("consultation_turns", "created_at")
