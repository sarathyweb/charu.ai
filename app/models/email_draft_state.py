"""EmailDraftState SQLModel — tracks pending email drafts awaiting user approval."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import CheckConstraint, Column, DateTime, Index, Text, text
from sqlmodel import Field, SQLModel

from app.models.enums import DraftStatus
from app.models.mixins import TimestampMixin

_STATUS_VALUES = ", ".join(f"'{e.value}'" for e in DraftStatus)


class EmailDraftState(TimestampMixin, SQLModel, table=True):
    __tablename__ = "email_draft_states"
    __table_args__ = (
        # One active draft per user per thread
        Index(
            "ix_email_draft_active_unique",
            "user_id",
            "thread_id",
            unique=True,
            postgresql_where=text(
                "status IN ('pending_review', 'revision_requested', 'approved')"
            ),
        ),
        CheckConstraint(
            f"status IN ({_STATUS_VALUES})",
            name="ck_email_draft_state_status",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    thread_id: str
    original_email_id: str
    original_from: str
    original_subject: str
    original_message_id: str  # MIME Message-ID for threading
    draft_text: str = Field(sa_column=Column(Text, nullable=False))
    status: str = DraftStatus.PENDING_REVIEW.value
    revision_count: int = Field(default=0)
    draft_review_sent_at: datetime | None = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default=None,
    )
    expires_at: datetime | None = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default_factory=lambda: datetime.now(timezone.utc) + timedelta(hours=2),
    )
