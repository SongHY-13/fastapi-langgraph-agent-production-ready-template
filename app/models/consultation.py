"""Durable consultation metadata and replay events."""

from datetime import datetime

from pydantic import JsonValue
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

ACTIVE_NORMAL = "priority = 'normal' AND status IN ('queued', 'running', 'cancelling')"


class ConsultationSession(SQLModel, table=True):
    """Map a scoped platform session to an internal context ID."""

    __tablename__ = "consultation_sessions"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "caller_id", "tenant_id", "subject_id", "platform_session_id", name="uq_consultation_session_scope"
        ),
    )

    id: str = Field(primary_key=True)
    caller_id: str
    tenant_id: str
    subject_id: str
    platform_session_id: str


class ConsultationTurn(SQLModel, table=True):
    """Retain the current turn's necessary input and stable image references."""

    __tablename__ = "consultation_turns"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint("session_id", "client_message_id", name="uq_consultation_turn_message"),
        UniqueConstraint("session_id", "id", name="uq_consultation_turn_session_id"),
    )

    id: str = Field(primary_key=True)
    session_id: str = Field(foreign_key="consultation_sessions.id")
    client_message_id: str
    fingerprint: str
    input_type: str
    text: str
    attachments: list[dict[str, str]] = Field(sa_column=Column(JSONB, nullable=False))
    created_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp())
    )


class ConsultationGeneration(SQLModel, table=True):
    """Retain task lifecycle state and final result."""

    __tablename__ = "consultation_generations"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        ForeignKeyConstraint(["session_id", "turn_id"], ["consultation_turns.session_id", "consultation_turns.id"]),
        UniqueConstraint("turn_id", "submission_key", name="uq_consultation_generation_submission"),
        CheckConstraint("priority IN ('normal', 'high_risk')", name="ck_consultation_generation_priority"),
        CheckConstraint(
            "status IN ('queued', 'running', 'cancelling', 'completed', 'cancelled', 'failed')",
            name="ck_consultation_generation_status",
        ),
        Index("uq_consultation_normal_slot", "session_id", unique=True, postgresql_where=text(ACTIVE_NORMAL)),
        Index("ix_consultation_generation_turn", "turn_id"),
        Index("uq_consultation_current_answer", "turn_id", unique=True, postgresql_where=text("is_current")),
    )

    id: str = Field(primary_key=True)
    session_id: str = Field(foreign_key="consultation_sessions.id")
    turn_id: str
    submission_key: str
    turn_outcome: str | None = None
    is_current: bool = Field(default=False, sa_column_kwargs={"server_default": "false"})
    regeneration_target: str | None = None
    priority: str
    status: str
    request_id: str
    lease_token: str | None = None
    lease_until: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    finished_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    state_version: int = Field(default=0, sa_column=Column(BigInteger, nullable=False, server_default="0"))
    replay_floor: int = Field(default=0, sa_column=Column(BigInteger, nullable=False, server_default="0"))
    answer: dict[str, JsonValue] | None = Field(default=None, sa_column=Column(JSONB))
    risk: dict[str, JsonValue] | None = Field(default=None, sa_column=Column(JSONB))
    advisory: dict[str, JsonValue] | None = Field(default=None, sa_column=Column(JSONB))
    created_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    )


class ConsultationEvent(SQLModel, table=True):
    """An ordered event belonging to a single generation."""

    __tablename__ = "consultation_events"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (CheckConstraint("seq > 0", name="ck_consultation_event_positive_seq"),)

    generation_id: str = Field(primary_key=True, foreign_key="consultation_generations.id")
    seq: int = Field(sa_column=Column(BigInteger, primary_key=True))
    type: str
    payload: dict[str, JsonValue] = Field(sa_column=Column(JSONB, nullable=False))
    created_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    )


class ConsultationSubscription(SQLModel, table=True):
    """Track active delivery independently from generation state."""

    __tablename__ = "consultation_subscriptions"  # pyright: ignore[reportAssignmentType]

    id: str = Field(primary_key=True)
    generation_id: str = Field(foreign_key="consultation_generations.id", ondelete="CASCADE")
    created_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    )
