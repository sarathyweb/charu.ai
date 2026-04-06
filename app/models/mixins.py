"""Reusable SQLModel mixins for common column patterns."""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, func
from sqlmodel import Field, SQLModel


class TimestampMixin(SQLModel):
    """Adds ``created_at`` and ``updated_at`` columns to any table model.

    * ``created_at`` — set once at INSERT time (DB-level ``DEFAULT now()`` +
      Python ``default_factory``).
    * ``updated_at`` — set to ``now()`` on every UPDATE at the ORM layer via
      a SQLAlchemy ``before_flush`` listener.  A companion PostgreSQL trigger
      (created in an Alembic migration) catches direct-SQL updates that
      bypass the ORM.

    All timestamps are timezone-aware UTC (``TIMESTAMPTZ``).

    Uses ``sa_type`` + ``sa_column_kwargs`` instead of ``sa_column`` so that
    SQLModel creates a fresh ``Column`` instance per table model — required
    when multiple models inherit from this mixin.
    """

    created_at: datetime = Field(
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"nullable": False, "server_default": func.now()},
        default_factory=lambda: datetime.now(timezone.utc),
    )

    updated_at: datetime | None = Field(
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"nullable": True},
        default=None,
    )


# ---------------------------------------------------------------------------
# ORM-layer auto-update for ``updated_at``
# ---------------------------------------------------------------------------
# This catches all ORM-based updates.  Direct SQL updates are handled by the
# PostgreSQL trigger created in the Alembic migration.

from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session as _SASession


@sa_event.listens_for(_SASession, "before_flush")
def _set_updated_at_on_flush(
    session: _SASession,
    flush_context: object,
    instances: object,
) -> None:
    """Set ``updated_at = now(UTC)`` on every dirty instance that has the column."""
    now = datetime.now(timezone.utc)
    for obj in session.dirty:
        if session.is_modified(obj) and hasattr(obj, "updated_at"):
            obj.updated_at = now  # type: ignore[attr-defined]
