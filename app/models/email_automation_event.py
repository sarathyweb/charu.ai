"""EmailAutomationEvent SQLModel — dedupe for Gmail automation side effects."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Float,
    String,
    UniqueConstraint,
)
from sqlmodel import Field, SQLModel

from app.models.enums import EmailAutomationEventType, EmailAutomationStatus
from app.models.mixins import TimestampMixin

_TYPE_VALUES = ", ".join(f"'{e.value}'" for e in EmailAutomationEventType)
_STATUS_VALUES = ", ".join(f"'{e.value}'" for e in EmailAutomationStatus)


class EmailAutomationEvent(TimestampMixin, SQLModel, table=True):
    """Tracks Gmail-thread actions taken by email automation sweeps."""

    __tablename__ = "email_automation_events"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "event_type",
            "gmail_thread_id",
            name="uq_email_automation_user_event_thread",
        ),
        CheckConstraint(
            f"event_type IN ({_TYPE_VALUES})",
            name="ck_email_automation_event_type",
        ),
        CheckConstraint(
            f"status IN ({_STATUS_VALUES})",
            name="ck_email_automation_status",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    event_type: str = Field(sa_column=Column(String(length=32), nullable=False))
    gmail_message_id: str = Field(sa_column=Column(String(length=256), nullable=False))
    gmail_thread_id: str = Field(sa_column=Column(String(length=256), nullable=False))
    status: str = EmailAutomationStatus.PROCESSING.value
    reason: str | None = Field(default=None, sa_column=Column(String(length=512)))
    confidence: float | None = Field(default=None, sa_column=Column(Float))
    task_id: int | None = Field(default=None, foreign_key="tasks.id")
    call_log_id: int | None = Field(default=None, foreign_key="call_logs.id")
    completed_at: datetime | None = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default=None,
    )
