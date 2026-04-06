"""Task SQLModel — user task list with pg_trgm fuzzy dedup support."""

from datetime import datetime

from sqlalchemy import CheckConstraint, Column, DateTime, Text
from sqlmodel import Field, SQLModel

from app.models.enums import TaskSource, TaskStatus
from app.models.mixins import TimestampMixin

# Build CHECK constraint value lists from enums at import time.
_STATUS_VALUES = ", ".join(f"'{e.value}'" for e in TaskStatus)
_SOURCE_VALUES = ", ".join(f"'{e.value}'" for e in TaskSource)


class Task(TimestampMixin, SQLModel, table=True):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_STATUS_VALUES})",
            name="ck_task_status",
        ),
        CheckConstraint(
            f"source IN ({_SOURCE_VALUES})",
            name="ck_task_source",
        ),
        CheckConstraint(
            "priority >= 0 AND priority <= 100",
            name="ck_task_priority_range",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    title: str = Field(sa_column=Column(Text, nullable=False))
    status: str = TaskStatus.PENDING.value
    priority: int = Field(default=50)
    source: str = TaskSource.USER_MENTION.value
    snoozed_until: datetime | None = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default=None,
    )
    completed_at: datetime | None = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default=None,
    )
