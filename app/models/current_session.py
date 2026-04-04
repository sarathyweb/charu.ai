"""CurrentSession mapping table — phone to active ADK session."""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime
from sqlmodel import Field, SQLModel


class CurrentSession(SQLModel, table=True):
    __tablename__ = "current_sessions"

    phone: str = Field(primary_key=True)
    session_id: str = Field(index=True)
    updated_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            default=lambda: datetime.now(timezone.utc),
        ),
        default_factory=lambda: datetime.now(timezone.utc),
    )
