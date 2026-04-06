"""CallLog SQLModel — materialized call instances with full lifecycle tracking."""

from datetime import date, datetime

from sqlalchemy import CheckConstraint, Column, DateTime, Index, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.models.enums import (
    CallLogStatus,
    CallType,
    OccurrenceKind,
    OutcomeConfidence,
)
from app.models.mixins import TimestampMixin

# Build CHECK constraint value lists from enums at import time.
_STATUS_VALUES = ", ".join(f"'{e.value}'" for e in CallLogStatus)
_CALL_TYPE_VALUES = ", ".join(f"'{e.value}'" for e in CallType)
_OCCURRENCE_KIND_VALUES = ", ".join(f"'{e.value}'" for e in OccurrenceKind)
_OUTCOME_CONFIDENCE_VALUES = ", ".join(f"'{e.value}'" for e in OutcomeConfidence)


class CallLog(TimestampMixin, SQLModel, table=True):
    __tablename__ = "call_logs"
    __table_args__ = (
        # Planner idempotency: one planned occurrence per user/type/date
        Index(
            "ix_call_log_planned_unique",
            "user_id",
            "call_type",
            "call_date",
            unique=True,
            postgresql_where=text("occurrence_kind = 'planned'"),
        ),
        # One active on-demand call per user (covers all non-terminal states)
        Index(
            "ix_call_log_ondemand_unique",
            "user_id",
            unique=True,
            postgresql_where=text(
                "call_type = 'on_demand' AND status NOT IN "
                "('completed', 'missed', 'cancelled', 'skipped', 'deferred')"
            ),
        ),
        # Enum CHECK constraints
        CheckConstraint(
            f"status IN ({_STATUS_VALUES})",
            name="ck_call_log_status",
        ),
        CheckConstraint(
            f"call_type IN ({_CALL_TYPE_VALUES})",
            name="ck_call_log_call_type",
        ),
        CheckConstraint(
            f"occurrence_kind IN ({_OCCURRENCE_KIND_VALUES})",
            name="ck_call_log_occurrence_kind",
        ),
        CheckConstraint(
            f"call_outcome_confidence IN ({_OUTCOME_CONFIDENCE_VALUES})"
            " OR call_outcome_confidence IS NULL",
            name="ck_call_log_call_outcome_confidence",
        ),
        CheckConstraint(
            f"reflection_confidence IN ({_OUTCOME_CONFIDENCE_VALUES})"
            " OR reflection_confidence IS NULL",
            name="ck_call_log_reflection_confidence",
        ),
    )

    # ── Core fields ──────────────────────────────────────────────────────
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    call_type: str  # CallType enum — enforced by CHECK constraint
    call_date: date = Field(index=True)
    scheduled_time: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    scheduled_timezone: str = Field(sa_column_kwargs={"nullable": False})  # snapshot of User.timezone at materialization
    actual_start_time: datetime | None = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default=None,
    )
    end_time: datetime | None = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default=None,
    )
    status: str = CallLogStatus.SCHEDULED.value
    occurrence_kind: str = OccurrenceKind.PLANNED.value
    attempt_number: int = Field(default=1)

    # ── Lineage ──────────────────────────────────────────────────────────
    root_call_log_id: int | None = Field(default=None, foreign_key="call_logs.id")
    replaced_call_log_id: int | None = Field(default=None, foreign_key="call_logs.id")
    origin_window_id: int | None = Field(default=None, foreign_key="call_windows.id")

    # ── Twilio ───────────────────────────────────────────────────────────
    twilio_call_sid: str | None = Field(default=None, index=True)
    celery_task_id: str | None = None
    answered_by: str | None = None
    duration_seconds: int | None = None
    last_twilio_sequence_number: int | None = None

    # ── Morning/afternoon structured outcome ─────────────────────────────
    goal: str | None = None
    next_action: str | None = None
    commitments: list[str] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )
    call_outcome_confidence: str | None = None

    # ── Evening structured outcome ───────────────────────────────────────
    accomplishments: str | None = None
    tomorrow_intention: str | None = None
    reflection_confidence: str | None = None

    # ── Transcript ───────────────────────────────────────────────────────
    transcript_filename: str | None = None

    # ── Recap tracking ───────────────────────────────────────────────────
    recap_sent_at: datetime | None = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default=None,
    )
    checkin_sent_at: datetime | None = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default=None,
    )

    # ── Concurrency ──────────────────────────────────────────────────────
    version: int = Field(default=1)
