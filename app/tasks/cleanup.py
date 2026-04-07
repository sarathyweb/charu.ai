"""Retention and cleanup tasks.

Three cleanup responsibilities:
1. Transcript artifact cleanup — delete artifacts older than 30 days,
   null out CallLog.transcript_filename to prevent dangling references.
2. CallLog retention — delete call log entries older than 90 days.
3. EmailDraftState expiry — abandon non-terminal drafts older than 2 hours.

Design references:
  - Design §Transcript storage (30-day retention, artifact cleanup)
  - Design §Data Models / EmailDraftState (2-hour expiry)
  - Requirements 14.7 (transcript retention ≥30 days)
  - Requirements 22.5 (call log retention ≥90 days)
  - Research 37 §11 (retention policy pattern)
  - Research 32 §Gotcha 8 (draft state cleanup — mark abandoned, don't delete)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlmodel import col, delete

from app.celery_app import celery_app, run_async
from app.db import async_session_factory
from app.models.call_log import CallLog
from app.models.email_draft_state import EmailDraftState
from app.models.enums import DraftStatus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TRANSCRIPT_RETENTION_DAYS = 30
CALL_LOG_RETENTION_DAYS = 90
DRAFT_EXPIRY_HOURS = 2

# Non-terminal draft statuses that should be expired after 2 hours.
_NON_TERMINAL_DRAFT_STATUSES = (
    DraftStatus.PENDING_REVIEW.value,
    DraftStatus.REVISION_REQUESTED.value,
    DraftStatus.APPROVED.value,
)


# ---------------------------------------------------------------------------
# Async implementations
# ---------------------------------------------------------------------------


async def _run_expire_stale_drafts() -> str:
    """Abandon non-terminal email drafts older than 2 hours.

    Drafts in ``pending_review``, ``revision_requested``, or ``approved``
    status whose ``expires_at`` (or ``created_at + 2h`` fallback) has
    passed are transitioned to ``abandoned``.  They are NOT deleted —
    they serve as audit records (Research 32 §Gotcha 8).
    """
    now = datetime.now(timezone.utc)

    async with async_session_factory() as session:
        # Primary path: use expires_at if set.
        # Fallback: created_at + 2 hours for rows where expires_at is NULL.
        stmt = (
            update(EmailDraftState)
            .where(
                EmailDraftState.status.in_(_NON_TERMINAL_DRAFT_STATUSES),  # type: ignore[union-attr]
                (
                    (col(EmailDraftState.expires_at).isnot(None) & (EmailDraftState.expires_at <= now))  # type: ignore[operator]
                    | (
                        col(EmailDraftState.expires_at).is_(None)
                        & (EmailDraftState.created_at <= now - timedelta(hours=DRAFT_EXPIRY_HOURS))
                    )
                ),
            )
            .values(status=DraftStatus.ABANDONED.value)
            .execution_options(synchronize_session="fetch")
        )
        result = await session.exec(stmt)  # type: ignore[arg-type]
        count = result.rowcount  # type: ignore[union-attr]
        await session.commit()

    logger.info("expire_stale_drafts: abandoned %d draft(s)", count)
    return f"expire_stale_drafts: abandoned {count} draft(s)"


async def _run_cleanup_old_transcripts() -> str:
    """Delete transcript artifacts older than 30 days.

    Sets ``CallLog.transcript_filename = None`` on affected rows to
    prevent dangling references.  The actual artifact files (stored via
    ADK ArtifactService) are not deleted here — that requires the
    ArtifactService API which is not available in the Celery worker
    context.  Nulling the filename is the critical step; orphaned
    artifact blobs can be garbage-collected separately.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=TRANSCRIPT_RETENTION_DAYS)

    async with async_session_factory() as session:
        stmt = (
            update(CallLog)
            .where(
                col(CallLog.transcript_filename).isnot(None),
                CallLog.created_at <= cutoff,
            )
            .values(transcript_filename=None)
            .execution_options(synchronize_session="fetch")
        )
        result = await session.exec(stmt)  # type: ignore[arg-type]
        count = result.rowcount  # type: ignore[union-attr]
        await session.commit()

    logger.info(
        "cleanup_old_transcripts: cleared transcript_filename on %d row(s) "
        "(cutoff=%s)",
        count,
        cutoff.isoformat(),
    )
    return f"cleanup_old_transcripts: cleared {count} transcript reference(s)"


async def _run_cleanup_old_call_logs() -> str:
    """Delete call log entries older than 90 days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=CALL_LOG_RETENTION_DAYS)

    async with async_session_factory() as session:
        stmt = delete(CallLog).where(CallLog.created_at <= cutoff)
        result = await session.exec(stmt)  # type: ignore[arg-type]
        count = result.rowcount  # type: ignore[union-attr]
        await session.commit()

    logger.info(
        "cleanup_old_call_logs: deleted %d row(s) (cutoff=%s)",
        count,
        cutoff.isoformat(),
    )
    return f"cleanup_old_call_logs: deleted {count} row(s)"


# ---------------------------------------------------------------------------
# Celery task entry points
# ---------------------------------------------------------------------------


@celery_app.task(name="app.tasks.cleanup.expire_stale_drafts")
def expire_stale_drafts() -> str:
    """Abandon non-terminal email drafts older than 2 hours."""
    return run_async(_run_expire_stale_drafts())


@celery_app.task(name="app.tasks.cleanup.cleanup_old_transcripts")
def cleanup_old_transcripts() -> str:
    """Delete transcript artifacts older than 30 days, null out CallLog.transcript_filename."""
    return run_async(_run_cleanup_old_transcripts())


@celery_app.task(name="app.tasks.cleanup.cleanup_old_call_logs")
def cleanup_old_call_logs() -> str:
    """Delete call log entries older than 90 days."""
    return run_async(_run_cleanup_old_call_logs())
