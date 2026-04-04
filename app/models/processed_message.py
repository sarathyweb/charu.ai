"""ProcessedMessage table — WhatsApp MessageSid idempotency tracking."""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime
from sqlmodel import Field, SQLModel


class ProcessedMessage(SQLModel, table=True):
    __tablename__ = "processed_messages"

    message_sid: str = Field(primary_key=True)
    processed_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            default=lambda: datetime.now(timezone.utc),
        ),
        default_factory=lambda: datetime.now(timezone.utc),
    )
