"""User SQLModel — database table and related schemas."""

from datetime import date, datetime, time

from sqlalchemy import Boolean, Column, DateTime, Time, text
from sqlmodel import Field, SQLModel

from app.models.mixins import TimestampMixin


class User(TimestampMixin, SQLModel, table=True):
    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    phone: str = Field(unique=True, index=True)
    firebase_uid: str | None = Field(default=None, unique=True, index=True)
    name: str | None = None
    timezone: str | None = None  # IANA identifier, e.g. "America/New_York"
    onboarding_complete: bool = Field(default=False)

    # Google OAuth (encrypted at rest via Fernet)
    google_access_token_encrypted: str | None = None
    google_refresh_token_encrypted: str | None = None
    google_token_expiry: datetime | None = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default=None,
    )
    google_granted_scopes: str | None = None  # space-separated scope list

    # Anti-habituation tracking
    last_opener_id: str | None = None
    last_approach: str | None = None
    consecutive_active_days: int = Field(default=0)
    last_active_date: date | None = None
    last_checkin_template: str | None = None

    # Metadata
    last_login_at: datetime | None = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default=None,
    )
    last_weekly_summary_sent_at: datetime | None = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default=None,
    )
    last_user_whatsapp_message_at: datetime | None = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default=None,
    )

    # Email automation opt-ins. Defaults stay off until the user explicitly
    # enables them from settings or an internal admin flow.
    urgent_email_calls_enabled: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    auto_task_from_emails_enabled: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    email_automation_quiet_hours_start: time = Field(
        default=time(21, 0),
        sa_column=Column(
            Time(timezone=False),
            nullable=False,
            server_default="21:00:00",
        ),
    )
    email_automation_quiet_hours_end: time = Field(
        default=time(8, 0),
        sa_column=Column(
            Time(timezone=False),
            nullable=False,
            server_default="08:00:00",
        ),
    )
