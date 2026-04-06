"""OutboundMessage SQLModel — at-most-once dedup for proactive WhatsApp sends."""

from datetime import datetime

from sqlalchemy import CheckConstraint, Column, DateTime, String, UniqueConstraint
from sqlmodel import Field, SQLModel

from app.models.enums import OutboundMessageStatus
from app.models.mixins import TimestampMixin

_STATUS_VALUES = ", ".join(f"'{e.value}'" for e in OutboundMessageStatus)


class OutboundMessage(TimestampMixin, SQLModel, table=True):
    __tablename__ = "outbound_messages"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_outbound_message_dedup"),
        CheckConstraint(
            f"status IN ({_STATUS_VALUES})",
            name="ck_outbound_message_status",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    dedup_key: str = Field(index=True)
    status: str = OutboundMessageStatus.PENDING.value
    twilio_message_sid: str | None = None
    sent_at: datetime | None = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default=None,
    )
    # Ownership token: a UUID written on claim/reclaim.  All subsequent
    # mutations (_mark_sent, _mark_failed, _release_claim) must match this
    # token, preventing a stale worker from mutating a row reclaimed by
    # another worker.
    claim_token: str | None = Field(
        sa_column=Column(String, nullable=True),
        default=None,
    )
