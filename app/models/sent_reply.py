"""SentReply SQLModel — Gmail thread-level duplicate send prevention."""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


class SentReply(SQLModel, table=True):
    __tablename__ = "sent_replies"
    __table_args__ = (
        UniqueConstraint("user_id", "thread_id", name="uq_sent_reply_user_thread"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    thread_id: str = Field(index=True)
    gmail_message_id: str
    reply_text: str = Field(sa_column=Column(Text, nullable=False))
    sent_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
        ),
        default_factory=lambda: datetime.now(timezone.utc),
    )
