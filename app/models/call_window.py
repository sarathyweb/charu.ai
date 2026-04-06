"""CallWindow SQLModel — per-user call scheduling windows."""

from datetime import time

from sqlalchemy import CheckConstraint, Column, Time, UniqueConstraint
from sqlmodel import Field, SQLModel

from app.models.enums import WindowType
from app.models.mixins import TimestampMixin

# Build the CHECK constraint value list from the enum at import time.
_WINDOW_TYPE_VALUES = ", ".join(f"'{e.value}'" for e in WindowType)


class CallWindow(TimestampMixin, SQLModel, table=True):
    __tablename__ = "call_windows"
    __table_args__ = (
        UniqueConstraint("user_id", "window_type", name="uq_user_window_type"),
        CheckConstraint(
            f"window_type IN ({_WINDOW_TYPE_VALUES})",
            name="ck_call_window_window_type",
        ),
        CheckConstraint(
            "end_time > start_time",
            name="ck_call_window_no_cross_midnight",
        ),
        CheckConstraint(
            "(EXTRACT(HOUR FROM end_time) * 60 + EXTRACT(MINUTE FROM end_time)) - "
            "(EXTRACT(HOUR FROM start_time) * 60 + EXTRACT(MINUTE FROM start_time)) >= 20",
            name="ck_call_window_min_width_20min",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    window_type: str  # WindowType enum value — enforced by CHECK constraint
    start_time: time = Field(sa_column=Column(Time(), nullable=False))
    end_time: time = Field(sa_column=Column(Time(), nullable=False))
    is_active: bool = Field(default=True)
