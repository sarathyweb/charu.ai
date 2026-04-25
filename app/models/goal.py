"""Goal SQLModel — higher-level objectives tracked across calls."""

from datetime import date, datetime

from sqlalchemy import CheckConstraint, Column, Date, DateTime, Text
from sqlmodel import Field, SQLModel

from app.models.enums import GoalStatus
from app.models.mixins import TimestampMixin

_STATUS_VALUES = ", ".join(f"'{e.value}'" for e in GoalStatus)


class Goal(TimestampMixin, SQLModel, table=True):
    """A user-owned objective that can span days or weeks."""

    __tablename__ = "goals"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_STATUS_VALUES})",
            name="ck_goal_status",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    title: str = Field(sa_column=Column(Text, nullable=False))
    description: str | None = Field(
        sa_column=Column(Text, nullable=True),
        default=None,
    )
    status: str = GoalStatus.ACTIVE.value
    target_date: date | None = Field(
        sa_column=Column(Date, nullable=True),
        default=None,
    )
    completed_at: datetime | None = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default=None,
    )
