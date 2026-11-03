"""Consultation sessions, turns, generations and events for v0.4."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "consultation_v04"
down_revision = "b25d38b0cd7c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create consultation storage and enforce the normal generation slot."""
    op.create_table(
        "consultation_sessions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("caller_id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("subject_id", sa.String(), nullable=False),
        sa.Column("platform_session_id", sa.String(), nullable=False),
        sa.UniqueConstraint(
            "caller_id", "tenant_id", "subject_id", "platform_session_id", name="uq_consultation_session_scope"
        ),
    )
    op.create_table(
        "consultation_turns",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), sa.ForeignKey("consultation_sessions.id"), nullable=False),
        sa.Column("client_message_id", sa.String(), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("input_type", sa.String(), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("attachments", JSONB(), nullable=False),
        sa.UniqueConstraint("session_id", "client_message_id", name="uq_consultation_turn_message"),
        sa.UniqueConstraint("session_id", "id", name="uq_consultation_turn_session_id"),
    )
    op.create_table(
        "consultation_generations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), sa.ForeignKey("consultation_sessions.id"), nullable=False),
        sa.Column("turn_id", sa.String(), nullable=False),
        sa.Column("submission_key", sa.String(), nullable=False),
        sa.Column("priority", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("answer", JSONB()),
        sa.Column("risk", JSONB()),
        sa.Column("advisory", JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["session_id", "turn_id"], ["consultation_turns.session_id", "consultation_turns.id"]),
        sa.UniqueConstraint("turn_id", "submission_key", name="uq_consultation_generation_submission"),
        sa.CheckConstraint("priority IN ('normal', 'high_risk')", name="ck_consultation_generation_priority"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'cancelling', 'completed', 'cancelled', 'failed')",
            name="ck_consultation_generation_status",
        ),
    )
    op.create_index("ix_consultation_generation_turn", "consultation_generations", ["turn_id"])
    op.create_index(
        "uq_consultation_normal_slot",
        "consultation_generations",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("priority = 'normal' AND status IN ('queued', 'running', 'cancelling')"),
    )
    op.create_table(
        "consultation_events",
        sa.Column("generation_id", sa.String(), sa.ForeignKey("consultation_generations.id"), primary_key=True),
        sa.Column("seq", sa.BigInteger(), primary_key=True),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("seq > 0", name="ck_consultation_event_positive_seq"),
    )


def downgrade() -> None:
    """Remove consultation storage in reverse dependency order."""
    op.drop_table("consultation_events")
    op.drop_table("consultation_generations")
    op.drop_table("consultation_turns")
    op.drop_table("consultation_sessions")
