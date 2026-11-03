"""Async PostgreSQL repository for trusted consultation submissions and lookup."""

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4

from pydantic import JsonValue, TypeAdapter

from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from app.models.consultation import ACTIVE_NORMAL
from app.schemas.consultation import (
    Advisory,
    Answer,
    CancelResult,
    FailurePayload,
    GenerationAccepted,
    GenerationCreate,
    GenerationEvent,
    GenerationResult,
    ImageAttachment,
    RiskNotice,
)
from app.services.consultation.identity import PlatformPrincipal


class SubmissionPriority(StrEnum):
    """Server-side result of risk classification before admission."""

    NORMAL = "normal"
    HIGH_RISK = "high_risk"


class RepositoryError(Exception):
    """A consultation request cannot be fulfilled."""

    def __init__(self, code: str) -> None:
        """Attach the stable public error code."""
        super().__init__(code)
        self.code = code


def _stable_attachments(attachments: list[ImageAttachment]) -> list[dict[str, str]]:
    return [{"file_id": attachment.file_id, "media_type": attachment.media_type} for attachment in attachments]


def fingerprint(request: GenerationCreate) -> str:
    """Hash business content using the already authorized stable file IDs."""
    canonical = json.dumps(
        [request.input_type, request.text, [attachment.file_id for attachment in request.attachments]],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


async def _lock_session(conn: AsyncConnection[DictRow], principal: PlatformPrincipal, platform_session_id: str) -> str:
    scope = (principal.caller_id, principal.tenant_id, principal.subject_id, platform_session_id)
    await conn.execute(
        """
        INSERT INTO consultation_sessions (id, caller_id, tenant_id, subject_id, platform_session_id)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (caller_id, tenant_id, subject_id, platform_session_id) DO NOTHING
        """,
        (uuid4().hex, *scope),
    )
    cursor = await conn.execute(
        """
        SELECT id FROM consultation_sessions
        WHERE caller_id = %s AND tenant_id = %s AND subject_id = %s AND platform_session_id = %s
        FOR UPDATE
        """,
        scope,
    )
    row = await cursor.fetchone()
    if row is None:
        raise RuntimeError("consultation session missing after registration")
    return row["id"]


async def _preempt_normal(conn: AsyncConnection[DictRow], session_id: str) -> None:
    # Wait for an in-flight event writer before starting the statement that allocates a sequence.
    await conn.execute(
        "SELECT id FROM consultation_generations WHERE session_id = %s AND " + ACTIVE_NORMAL + " FOR UPDATE",
        (session_id,),
    )
    await conn.execute(
        """
        WITH stopped AS (
            UPDATE consultation_generations SET status = 'cancelled', finished_at = now(), state_version = state_version + 1, lease_token = NULL, lease_until = NULL
            WHERE session_id = %s AND
        """
        + ACTIVE_NORMAL
        + """
            RETURNING id
        )
        INSERT INTO consultation_events (generation_id, seq, type, payload)
        SELECT stopped.id,
            COALESCE((SELECT MAX(seq) FROM consultation_events WHERE generation_id = stopped.id), 0) + 1,
            'generation.cancelled', '{}'::jsonb
        FROM stopped
        """,
        (session_id,),
    )


@dataclass(frozen=True, slots=True, repr=False)
class GenerationLease:
    """Durable job inputs and compare-and-set credentials for a worker."""

    generation_id: str
    session_id: str
    turn_id: str
    principal: PlatformPrincipal
    text: str
    input_type: str
    attachments: list[dict[str, str]]
    token: str
    version: int
    history: tuple[tuple[str, str], ...] = ()
    regeneration_target: str | None = None


_EVENT = TypeAdapter(GenerationEvent)
_TERMINAL = {"completed", "cancelled", "failed"}
_SCOPE = "g.id = %s AND s.caller_id = %s AND s.tenant_id = %s AND s.subject_id = %s"


async def _scoped(
    conn: AsyncConnection[DictRow], principal: PlatformPrincipal, generation_id: str, *, write: bool = False
) -> DictRow:
    cursor = await conn.execute(
        """
        SELECT g.status, g.request_id, g.replay_floor,
            COALESCE(g.finished_at <= now() - interval '24 hours', false) AS expired
        FROM consultation_generations g JOIN consultation_sessions s ON s.id = g.session_id
        WHERE """
        + _SCOPE
        + (" FOR UPDATE OF g" if write else " FOR SHARE OF g"),
        (generation_id, principal.caller_id, principal.tenant_id, principal.subject_id),
    )
    row = await cursor.fetchone()
    if row is None:
        raise RepositoryError("GENERATION_NOT_FOUND")
    return row


async def append_event(
    conn: AsyncConnection[DictRow], generation_id: str, event_type: str, payload: dict[str, JsonValue]
) -> int:
    """Append within a transaction already holding the generation write lock."""
    cursor = await conn.execute(
        """
        INSERT INTO consultation_events (generation_id, seq, type, payload)
        SELECT g.id, GREATEST(g.replay_floor, COALESCE(MAX(e.seq), 0)) + 1, %s, %s
        FROM consultation_generations g LEFT JOIN consultation_events e ON e.generation_id = g.id
        WHERE g.id = %s GROUP BY g.id
        RETURNING seq
        """,
        (event_type, Jsonb(payload), generation_id),
    )
    row = await cursor.fetchone()
    if row is None:
        raise RepositoryError("GENERATION_NOT_FOUND")
    return row["seq"]


class ConsultationRepository:
    """Store admissions and recover results under the trusted platform scope."""

    def __init__(self, dsn: str) -> None:
        """Use a PostgreSQL connection string for repository transactions."""
        self._dsn = dsn

    async def create_or_get(
        self,
        principal: PlatformPrincipal,
        session_id: str,
        request: GenerationCreate,
        *,
        priority: SubmissionPriority = SubmissionPriority.NORMAL,
        risk: RiskNotice | None = None,
        request_id: str | None = None,
    ) -> GenerationAccepted:
        """Deduplicate admission and reserve the normal slot in one transaction."""
        if (priority == SubmissionPriority.HIGH_RISK) != (risk is not None):
            raise ValueError("a risk notice is required only for high-risk submissions")
        request_id = request_id or uuid4().hex
        content_hash = fingerprint(request)
        async with await AsyncConnection[DictRow].connect(self._dsn, row_factory=dict_row) as conn:
            internal_session_id = await _lock_session(conn, principal, session_id)
            cursor = await conn.execute(
                """
                SELECT g.id AS generation_id, t.id AS user_message_id, g.status, t.fingerprint
                FROM consultation_turns t
                JOIN consultation_generations g ON g.turn_id = t.id AND g.submission_key = 'initial'
                WHERE t.session_id = %s AND t.client_message_id = %s
                """,
                (internal_session_id, request.client_message_id),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                if existing.pop("fingerprint") != content_hash:
                    raise RepositoryError("IDEMPOTENCY_CONFLICT")
                return GenerationAccepted.model_validate({**existing, "request_id": request_id})

            if risk is None:
                cursor = await conn.execute(
                    "SELECT id FROM consultation_generations WHERE session_id = %s AND " + ACTIVE_NORMAL,
                    (internal_session_id,),
                )
                if await cursor.fetchone() is not None:
                    raise RepositoryError("GENERATION_IN_PROGRESS")
            else:
                await _preempt_normal(conn, internal_session_id)

            turn_id = uuid4().hex
            generation_id = uuid4().hex
            status = "queued" if risk is None else "completed"
            await conn.execute(
                """
                INSERT INTO consultation_turns (id, session_id, client_message_id, fingerprint,
                    input_type, text, attachments)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    turn_id,
                    internal_session_id,
                    request.client_message_id,
                    content_hash,
                    request.input_type,
                    request.text,
                    Jsonb(_stable_attachments(request.attachments)),
                ),
            )
            await conn.execute(
                """
                INSERT INTO consultation_generations (id, session_id, turn_id, submission_key,
                    priority, status, request_id, risk, finished_at)
                VALUES (%s, %s, %s, 'initial', %s, %s, %s, %s, CASE WHEN %s THEN now() END)
                """,
                (
                    generation_id,
                    internal_session_id,
                    turn_id,
                    priority.value,
                    status,
                    request_id,
                    Jsonb(risk.model_dump(mode="json")) if risk is not None else None,
                    risk is not None,
                ),
            )
            if risk is not None:
                await conn.execute(
                    """
                    INSERT INTO consultation_events (generation_id, seq, type, payload)
                    VALUES (%s, 1, 'risk.notice', %s),
                           (%s, 2, 'generation.completed', '{"answer_id": null, "advisory": null}'::jsonb)
                    """,
                    (generation_id, Jsonb(risk.model_dump(mode="json")), generation_id),
                )
            return GenerationAccepted(
                generation_id=generation_id, user_message_id=turn_id, status=status, request_id=request_id
            )

    async def regenerate(
        self,
        principal: PlatformPrincipal,
        session_id: str,
        answer_id: str,
        idempotency_key: str,
        attachments: list[ImageAttachment],
        *,
        request_id: str,
        probe: bool = False,
    ) -> GenerationAccepted | None:
        """Scope the target and recheck eligibility under the normal slot lock."""
        key = f"regen:{idempotency_key}"
        async with await AsyncConnection[DictRow].connect(self._dsn, row_factory=dict_row) as conn:
            scope = await conn.execute(
                """
                SELECT id FROM consultation_sessions WHERE caller_id = %s AND tenant_id = %s
                    AND subject_id = %s AND platform_session_id = %s FOR UPDATE
                """,
                (principal.caller_id, principal.tenant_id, principal.subject_id, session_id),
            )
            session = await scope.fetchone()
            if session is None:
                raise RepositoryError("REGENERATION_NOT_ALLOWED")
            internal_id = session["id"]
            target = await conn.execute(
                """
                SELECT t.*, g.is_current,
                    EXISTS (SELECT 1 FROM consultation_turns n WHERE n.session_id = t.session_id
                        AND (n.created_at, n.id) > (t.created_at, t.id)) AS superseded
                FROM consultation_turns t JOIN consultation_generations g ON g.turn_id = t.id
                WHERE t.session_id = %s AND g.answer ->> 'answer_id' = %s
                """,
                (internal_id, answer_id),
            )
            turn = await target.fetchone()
            if turn is None:
                raise RepositoryError("REGENERATION_NOT_ALLOWED")
            if _stable_attachments(attachments) != turn["attachments"]:
                raise RepositoryError("INVALID_ATTACHMENT")
            existing = await conn.execute(
                """
                SELECT id AS generation_id, status, regeneration_target FROM consultation_generations
                WHERE turn_id = %s AND submission_key = %s
                """,
                (turn["id"], key),
            )
            replay = await existing.fetchone()
            if replay is not None:
                if replay.pop("regeneration_target") != answer_id:
                    raise RepositoryError("IDEMPOTENCY_CONFLICT")
                return GenerationAccepted.model_validate(
                    {**replay, "user_message_id": turn["id"], "request_id": request_id}
                )
            if not turn["is_current"] or turn["superseded"]:
                raise RepositoryError("REGENERATION_NOT_ALLOWED")
            if probe:
                return None
            active = await conn.execute(
                "SELECT id FROM consultation_generations WHERE session_id = %s AND " + ACTIVE_NORMAL,
                (internal_id,),
            )
            if await active.fetchone() is not None:
                raise RepositoryError("GENERATION_IN_PROGRESS")
            generation_id = uuid4().hex
            await conn.execute(
                """
                INSERT INTO consultation_generations
                    (id, session_id, turn_id, submission_key, priority, status, request_id, regeneration_target)
                VALUES (%s, %s, %s, %s, 'normal', 'queued', %s, %s)
                """,
                (generation_id, internal_id, turn["id"], key, request_id, answer_id),
            )
            return GenerationAccepted(
                generation_id=generation_id, user_message_id=turn["id"], status="queued", request_id=request_id
            )

    async def get(
        self, principal: PlatformPrincipal, generation_id: str, *, request_id: str | None = None
    ) -> GenerationResult:
        """Read the durable result only when every trusted scope component matches."""
        async with await AsyncConnection[DictRow].connect(self._dsn, row_factory=dict_row) as conn:
            cursor = await conn.execute(
                """
                SELECT g.status, g.answer, g.risk, g.advisory
                FROM consultation_generations g
                JOIN consultation_sessions s ON s.id = g.session_id
                WHERE g.id = %s AND s.caller_id = %s AND s.tenant_id = %s AND s.subject_id = %s
                """,
                (generation_id, principal.caller_id, principal.tenant_id, principal.subject_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RepositoryError("GENERATION_NOT_FOUND")
            return GenerationResult.model_validate({**row, "request_id": request_id or uuid4().hex})

    async def claim(self, *, lease_seconds: int = 60) -> GenerationLease | None:
        """Claim queued or abandoned work, using the database clock."""
        if lease_seconds <= 0:
            raise ValueError("lease duration must be positive")
        async with await AsyncConnection[DictRow].connect(self._dsn, row_factory=dict_row) as conn:
            token = uuid4().hex
            cursor = await conn.execute(
                """
                WITH candidate AS (
                    SELECT id FROM consultation_generations
                    WHERE priority = 'normal' AND (
                        status = 'queued' OR (status = 'running' AND (lease_until IS NULL OR lease_until <= now()))
                    )
                    ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
                ), claimed AS (
                    UPDATE consultation_generations g SET status = 'running', lease_token = %s,
                        lease_until = now() + %s * interval '1 second', state_version = state_version + 1
                    FROM candidate WHERE g.id = candidate.id
                    RETURNING g.id, g.session_id, g.turn_id, g.state_version, g.regeneration_target
                )
                SELECT c.*, t.text, t.input_type, t.attachments, s.caller_id, s.tenant_id, s.subject_id
                FROM claimed c JOIN consultation_turns t ON t.id = c.turn_id
                JOIN consultation_sessions s ON s.id = c.session_id
                """,
                (token, lease_seconds),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            history = await conn.execute(
                """
                SELECT prior.text, g.answer ->> 'text' AS answer FROM consultation_turns current_turn
                JOIN consultation_turns prior ON prior.session_id = current_turn.session_id
                    AND (prior.created_at, prior.id) < (current_turn.created_at, current_turn.id)
                JOIN consultation_generations g ON g.turn_id = prior.id AND g.is_current
                WHERE current_turn.id = %s ORDER BY prior.created_at DESC, prior.id DESC LIMIT 8
                """,
                (row["turn_id"],),
            )
            return GenerationLease(
                row["id"],
                row["session_id"],
                row["turn_id"],
                PlatformPrincipal(row["caller_id"], row["tenant_id"], row["subject_id"]),
                row["text"],
                row["input_type"],
                row["attachments"],
                token,
                row["state_version"],
                tuple((item["text"], item["answer"]) for item in reversed(await history.fetchall())),
                row["regeneration_target"],
            )

    async def finish(
        self,
        lease: GenerationLease,
        *,
        answer: Answer | None = None,
        advisory: Advisory | None = None,
        failure: FailurePayload | None = None,
        unanswered: bool = False,
        effective: bool = True,
        risk: RiskNotice | None = None,
    ) -> bool:
        """Publish reviewed text, metadata and terminal state in one transaction."""
        if (failure is None) == (answer is None and advisory is None and risk is None):
            raise ValueError("provide either a reviewed answer or failure")
        if risk is not None and (answer is not None or advisory is not None or unanswered):
            raise ValueError("risk completion must be risk-only")
        risk_data = risk.model_dump(mode="json") if risk is not None else None
        answer_data = answer.model_dump(mode="json") if answer is not None else None
        advisory_data = advisory.model_dump(mode="json") if advisory is not None else None
        outcome = "unanswered" if unanswered else "effective" if answer is not None and effective else None
        async with await AsyncConnection[DictRow].connect(self._dsn, row_factory=dict_row) as conn:
            if outcome is not None or risk is not None:
                await conn.execute(
                    "SELECT id FROM consultation_sessions WHERE id = %s FOR UPDATE", (lease.session_id,)
                )
            if outcome is not None:
                prior = await conn.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM consultation_generations WHERE turn_id = %s AND turn_outcome IS NOT NULL
                    ) AS duplicate, (
                        SELECT turn_outcome FROM consultation_generations
                        WHERE session_id = %s AND turn_outcome IS NOT NULL
                        ORDER BY finished_at DESC, id DESC LIMIT 1
                    ) AS latest
                    """,
                    (lease.turn_id, lease.session_id),
                )
                previous = await prior.fetchone()
                if previous and previous["duplicate"]:
                    outcome = None
                elif unanswered and previous and previous["latest"] == "unanswered":
                    advisory_data = Advisory(
                        code="human_handoff_suggested", reason="consecutive_unanswered"
                    ).model_dump(mode="json")
            cursor = await conn.execute(
                """
                UPDATE consultation_generations
                SET status = %s, answer = %s, advisory = %s, turn_outcome = %s, risk = %s,
                    priority = CASE WHEN %s THEN 'high_risk' ELSE priority END, finished_at = clock_timestamp(),
                    lease_token = NULL, lease_until = NULL, state_version = state_version + 1
                WHERE id = %s AND status = 'running' AND lease_token = %s AND state_version = %s
                    AND lease_until > clock_timestamp()
                RETURNING id
                """,
                (
                    "failed" if failure is not None else "completed",
                    Jsonb(answer_data) if answer_data is not None else None,
                    Jsonb(advisory_data) if advisory_data is not None else None,
                    outcome,
                    Jsonb(risk_data) if risk_data is not None else None,
                    risk is not None,
                    lease.generation_id,
                    lease.token,
                    lease.version,
                ),
            )
            if await cursor.fetchone() is None:
                return False
            if answer_data is not None and effective and not unanswered:
                await conn.execute(
                    "UPDATE consultation_generations SET is_current = false WHERE turn_id = %s AND is_current",
                    (lease.turn_id,),
                )
                await conn.execute(
                    "UPDATE consultation_generations SET is_current = true WHERE id = %s",
                    (lease.generation_id,),
                )
            if risk_data is not None:
                await append_event(conn, lease.generation_id, "risk.notice", risk_data)
            if failure is not None:
                await append_event(conn, lease.generation_id, "generation.failed", failure.model_dump(mode="json"))
            elif answer is not None:
                for offset in range(0, len(answer.text), 256):
                    await append_event(
                        conn,
                        lease.generation_id,
                        "answer.delta",
                        {"answer_id": answer.answer_id, "text": answer.text[offset : offset + 256]},
                    )
                await append_event(
                    conn, lease.generation_id, "answer.metadata", answer.model_dump(mode="json", exclude={"text"})
                )
            if failure is None:
                await append_event(
                    conn,
                    lease.generation_id,
                    "generation.completed",
                    {"answer_id": answer.answer_id if answer else None, "advisory": advisory_data},
                )
            return True

    async def cancel(
        self, principal: PlatformPrincipal, generation_id: str, subscription_id: str | None, request_id: str
    ) -> CancelResult:
        """Settle active work or close only the selected completed delivery."""
        async with await AsyncConnection[DictRow].connect(self._dsn, row_factory=dict_row) as conn:
            row = await _scoped(conn, principal, generation_id, write=True)
            status = row["status"]
            aborted = False
            if subscription_id:
                cursor = await conn.execute(
                    "DELETE FROM consultation_subscriptions WHERE id = %s AND generation_id = %s RETURNING id",
                    (subscription_id, generation_id),
                )
                aborted = await cursor.fetchone() is not None
            if status not in _TERMINAL:
                await conn.execute(
                    """
                    UPDATE consultation_generations SET status = 'cancelled', finished_at = now(),
                        lease_token = NULL, lease_until = NULL, state_version = state_version + 1
                    WHERE id = %s
                    """,
                    (generation_id,),
                )
                await append_event(conn, generation_id, "generation.cancelled", {})
                status = "cancelled"
            return CancelResult(status=status, delivery_status="aborted" if aborted else None, request_id=request_id)

    async def open_subscription(self, principal: PlatformPrincipal, generation_id: str, after_seq: int) -> str:
        """Verify scoped replay availability before sending response headers."""
        async with await AsyncConnection[DictRow].connect(self._dsn, row_factory=dict_row) as conn:
            row = await _scoped(conn, principal, generation_id)
            if row["expired"] or after_seq < row["replay_floor"]:
                raise RepositoryError("EVENT_HISTORY_EXPIRED")
            subscription_id = uuid4().hex
            await conn.execute(
                "INSERT INTO consultation_subscriptions (id, generation_id) VALUES (%s, %s)",
                (subscription_id, generation_id),
            )
            return subscription_id

    async def subscription_is_open(self, generation_id: str, subscription_id: str) -> bool:
        """Check a delivery marker in shared storage."""
        async with await AsyncConnection[DictRow].connect(self._dsn, row_factory=dict_row) as conn:
            return await self._subscription_exists(conn, generation_id, subscription_id)

    @staticmethod
    async def _subscription_exists(conn: AsyncConnection[DictRow], generation_id: str, subscription_id: str) -> bool:
        cursor = await conn.execute(
            "SELECT 1 FROM consultation_subscriptions WHERE id = %s AND generation_id = %s",
            (subscription_id, generation_id),
        )
        return await cursor.fetchone() is not None

    async def close_subscription(self, generation_id: str, subscription_id: str) -> None:
        """Remove a delivery marker after completion or disconnect."""
        async with await AsyncConnection[DictRow].connect(self._dsn, row_factory=dict_row) as conn:
            await conn.execute(
                "DELETE FROM consultation_subscriptions WHERE id = %s AND generation_id = %s",
                (subscription_id, generation_id),
            )

    async def read_events(
        self, principal: PlatformPrincipal, generation_id: str, after_seq: int, subscription_id: str | None = None
    ) -> tuple[list[GenerationEvent], bool]:
        """Read one consistent ordered batch and indicate when replay has ended."""
        async with await AsyncConnection[DictRow].connect(self._dsn, row_factory=dict_row) as conn:
            row = await _scoped(conn, principal, generation_id)
            if subscription_id and not await self._subscription_exists(conn, generation_id, subscription_id):
                return [], True
            if row["expired"] or after_seq < row["replay_floor"]:
                raise RepositoryError("EVENT_HISTORY_EXPIRED")
            cursor = await conn.execute(
                """
                SELECT seq, type, payload FROM consultation_events
                WHERE generation_id = %s AND seq > %s ORDER BY seq LIMIT 64
                """,
                (generation_id, after_seq),
            )
            events = [
                _EVENT.validate_python(
                    {
                        **event,
                        "generation_id": generation_id,
                        "event_id": f"{generation_id}:{event['seq']}",
                        "request_id": row["request_id"],
                    }
                )
                for event in await cursor.fetchall()
            ]
            return events, row["status"] in _TERMINAL and len(events) < 64
