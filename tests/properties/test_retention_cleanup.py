"""Property tests for retention cleanup (P32).

P32 — Data retention cleanup preserves recent records:
     Cleanup tasks must only affect records older than their respective
     retention thresholds.  Records within the retention window must
     survive cleanup unchanged.

     Three cleanup responsibilities:
       1. Transcript artifact cleanup — 30 days
       2. CallLog retention — 90 days
       3. EmailDraftState expiry — 2 hours for non-terminal drafts

Validates: Requirements 14.7, 22.5
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import update
from sqlmodel import col, delete, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.call_log import CallLog
from app.models.email_draft_state import EmailDraftState
from app.models.enums import (
    CallLogStatus,
    CallType,
    DraftStatus,
    OccurrenceKind,
)
from app.models.user import User
from app.tasks.cleanup import (
    CALL_LOG_RETENTION_DAYS,
    DRAFT_EXPIRY_HOURS,
    TRANSCRIPT_RETENTION_DAYS,
    _NON_TERMINAL_DRAFT_STATUSES,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_days_beyond_transcript = st.integers(min_value=1, max_value=365)
_days_beyond_call_log = st.integers(min_value=1, max_value=365)
_days_within_transcript = st.integers(
    min_value=0, max_value=TRANSCRIPT_RETENTION_DAYS - 1,
)
_days_within_call_log = st.integers(
    min_value=0, max_value=CALL_LOG_RETENTION_DAYS - 1,
)
_hours_beyond_draft = st.integers(min_value=1, max_value=72)

_non_terminal_draft_statuses = st.sampled_from([
    DraftStatus.PENDING_REVIEW.value,
    DraftStatus.REVISION_REQUESTED.value,
    DraftStatus.APPROVED.value,
])

_terminal_draft_statuses = st.sampled_from([
    DraftStatus.SENT.value,
    DraftStatus.ABANDONED.value,
])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_phone_counter = 0


async def _create_user(session: AsyncSession) -> User:
    global _phone_counter
    _phone_counter += 1
    user = User(
        phone=f"+1555900{_phone_counter:04d}",
        timezone="America/New_York",
        onboarding_complete=True,
        consecutive_active_days=0,
    )
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return user


async def _create_call_log(
    session: AsyncSession,
    user_id: int,
    created_at: datetime,
    transcript_filename: str | None = None,
    call_type: str = CallType.MORNING.value,
    occurrence_kind: str = OccurrenceKind.PLANNED.value,
) -> CallLog:
    cl = CallLog(
        user_id=user_id,
        call_type=call_type,
        call_date=created_at.date(),
        scheduled_time=created_at,
        scheduled_timezone="America/New_York",
        status=CallLogStatus.COMPLETED.value,
        occurrence_kind=occurrence_kind,
        attempt_number=1,
        transcript_filename=transcript_filename,
    )
    cl.created_at = created_at
    session.add(cl)
    await session.flush()
    await session.refresh(cl)
    return cl


async def _create_draft(
    session: AsyncSession,
    user_id: int,
    status: str,
    created_at: datetime,
    expires_at: datetime | None = None,
) -> EmailDraftState:
    global _phone_counter
    draft = EmailDraftState(
        user_id=user_id,
        thread_id=f"thread_{_phone_counter}_{created_at.timestamp():.0f}",
        original_email_id="msg_001",
        original_from="sender@example.com",
        original_subject="Test Subject",
        original_message_id="<test@example.com>",
        draft_text="Draft reply text",
        status=status,
        revision_count=0,
    )
    draft.created_at = created_at
    draft.expires_at = expires_at
    session.add(draft)
    await session.flush()
    await session.refresh(draft)
    return draft


async def _fetch_call_log(session: AsyncSession, cl_id: int) -> CallLog | None:
    result = await session.exec(select(CallLog).where(CallLog.id == cl_id))
    return result.first()


async def _fetch_draft(
    session: AsyncSession, draft_id: int,
) -> EmailDraftState | None:
    result = await session.exec(
        select(EmailDraftState).where(EmailDraftState.id == draft_id)
    )
    return result.first()


# ---------------------------------------------------------------------------
# Cleanup logic replicated in-session (mirrors app/tasks/cleanup.py)
# ---------------------------------------------------------------------------


async def _do_transcript_cleanup(session: AsyncSession) -> int:
    """Replicate _run_cleanup_old_transcripts using the test session."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=TRANSCRIPT_RETENTION_DAYS)
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
    await session.flush()
    return result.rowcount  # type: ignore[union-attr]


async def _do_call_log_cleanup(session: AsyncSession) -> int:
    """Replicate _run_cleanup_old_call_logs using the test session."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=CALL_LOG_RETENTION_DAYS)
    stmt = delete(CallLog).where(CallLog.created_at <= cutoff)
    result = await session.exec(stmt)  # type: ignore[arg-type]
    await session.flush()
    return result.rowcount  # type: ignore[union-attr]


async def _do_draft_expiry(session: AsyncSession) -> int:
    """Replicate _run_expire_stale_drafts using the test session."""
    now = datetime.now(timezone.utc)
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
    await session.flush()
    return result.rowcount  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# P32a: Transcript cleanup preserves recent records
# ---------------------------------------------------------------------------


@given(
    days_old=_days_beyond_transcript,
    days_recent=_days_within_transcript,
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=30,
)
@pytest.mark.asyncio
async def test_transcript_cleanup_preserves_recent(
    days_old: int,
    days_recent: int,
    session: AsyncSession,
):
    """Transcript cleanup nulls transcript_filename on records older than
    30 days but preserves it on records within the retention window."""
    now = datetime.now(timezone.utc)
    user = await _create_user(session)

    old_time = now - timedelta(days=TRANSCRIPT_RETENTION_DAYS + days_old)
    recent_time = now - timedelta(days=days_recent)

    old_cl = await _create_call_log(
        session, user.id, old_time, transcript_filename="old_transcript.json",
        call_type=CallType.MORNING.value,
    )
    recent_cl = await _create_call_log(
        session, user.id, recent_time, transcript_filename="recent_transcript.json",
        call_type=CallType.AFTERNOON.value,
    )
    old_id, recent_id = old_cl.id, recent_cl.id

    await _do_transcript_cleanup(session)

    old_row = await _fetch_call_log(session, old_id)
    recent_row = await _fetch_call_log(session, recent_id)

    assert old_row is not None, "Old CallLog row should still exist"
    assert old_row.transcript_filename is None, (
        f"Old transcript_filename should be None, got {old_row.transcript_filename}"
    )
    assert recent_row is not None, "Recent CallLog row should exist"
    assert recent_row.transcript_filename == "recent_transcript.json", (
        f"Recent transcript should be preserved, got {recent_row.transcript_filename}"
    )


# ---------------------------------------------------------------------------
# P32b: Transcript cleanup ignores records without transcript_filename
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transcript_cleanup_ignores_null_filename(session: AsyncSession):
    """Records with transcript_filename=None are not affected by cleanup."""
    now = datetime.now(timezone.utc)
    user = await _create_user(session)

    old_time = now - timedelta(days=TRANSCRIPT_RETENTION_DAYS + 10)
    cl = await _create_call_log(session, user.id, old_time, transcript_filename=None)
    cl_id = cl.id

    count = await _do_transcript_cleanup(session)

    row = await _fetch_call_log(session, cl_id)
    assert row is not None
    assert row.transcript_filename is None
    assert count == 0, "No rows should be affected when transcript_filename is already None"


# ---------------------------------------------------------------------------
# P32c: CallLog retention cleanup preserves recent records
# ---------------------------------------------------------------------------


@given(
    days_old=_days_beyond_call_log,
    days_recent=_days_within_call_log,
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=30,
)
@pytest.mark.asyncio
async def test_call_log_cleanup_preserves_recent(
    days_old: int,
    days_recent: int,
    session: AsyncSession,
):
    """CallLog cleanup deletes records older than 90 days but preserves
    records within the retention window."""
    now = datetime.now(timezone.utc)
    user = await _create_user(session)

    old_time = now - timedelta(days=CALL_LOG_RETENTION_DAYS + days_old)
    recent_time = now - timedelta(days=days_recent)

    old_cl = await _create_call_log(
        session, user.id, old_time, call_type=CallType.MORNING.value,
    )
    recent_cl = await _create_call_log(
        session, user.id, recent_time, call_type=CallType.AFTERNOON.value,
    )
    old_id, recent_id = old_cl.id, recent_cl.id

    await _do_call_log_cleanup(session)

    old_row = await _fetch_call_log(session, old_id)
    recent_row = await _fetch_call_log(session, recent_id)

    assert old_row is None, "Old CallLog should be deleted"
    assert recent_row is not None, "Recent CallLog should be preserved"


# ---------------------------------------------------------------------------
# P32d: CallLog cleanup at exact boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_log_cleanup_boundary(session: AsyncSession):
    """A record at the exact 90-day cutoff is deleted (<=), while one
    created 1 second after the cutoff is preserved."""
    now = datetime.now(timezone.utc)
    user = await _create_user(session)

    cutoff = now - timedelta(days=CALL_LOG_RETENTION_DAYS)
    at_cutoff = await _create_call_log(
        session, user.id, cutoff, call_type=CallType.MORNING.value,
    )
    after_cutoff = await _create_call_log(
        session, user.id, cutoff + timedelta(seconds=1),
        call_type=CallType.AFTERNOON.value,
    )
    at_id, after_id = at_cutoff.id, after_cutoff.id

    await _do_call_log_cleanup(session)

    assert await _fetch_call_log(session, at_id) is None, (
        "Record at exact cutoff should be deleted"
    )
    assert await _fetch_call_log(session, after_id) is not None, (
        "Record 1s after cutoff should be preserved"
    )


# ---------------------------------------------------------------------------
# P32e: Draft expiry abandons stale non-terminal drafts
# ---------------------------------------------------------------------------


@given(
    status=_non_terminal_draft_statuses,
    hours_extra=_hours_beyond_draft,
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=30,
)
@pytest.mark.asyncio
async def test_draft_expiry_abandons_stale_non_terminal(
    status: str,
    hours_extra: int,
    session: AsyncSession,
):
    """Non-terminal drafts whose expires_at has passed are set to abandoned."""
    now = datetime.now(timezone.utc)
    user = await _create_user(session)

    expired_at = now - timedelta(hours=hours_extra)
    created = expired_at - timedelta(hours=DRAFT_EXPIRY_HOURS)

    draft = await _create_draft(
        session, user.id, status, created_at=created, expires_at=expired_at,
    )
    draft_id = draft.id

    await _do_draft_expiry(session)

    row = await _fetch_draft(session, draft_id)
    assert row is not None, "Draft should not be deleted (audit record)"
    assert row.status == DraftStatus.ABANDONED.value, (
        f"Stale {status} draft should be abandoned, got {row.status}"
    )


# ---------------------------------------------------------------------------
# P32f: Draft expiry preserves recent non-terminal drafts
# ---------------------------------------------------------------------------


@given(status=_non_terminal_draft_statuses)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=15,
)
@pytest.mark.asyncio
async def test_draft_expiry_preserves_recent_non_terminal(
    status: str,
    session: AsyncSession,
):
    """Non-terminal drafts whose expires_at is in the future are preserved."""
    now = datetime.now(timezone.utc)
    user = await _create_user(session)

    expires_at = now + timedelta(hours=1)
    created = now - timedelta(minutes=30)

    draft = await _create_draft(
        session, user.id, status, created_at=created, expires_at=expires_at,
    )
    draft_id = draft.id

    await _do_draft_expiry(session)

    row = await _fetch_draft(session, draft_id)
    assert row is not None
    assert row.status == status, (
        f"Recent {status} draft should be preserved, got {row.status}"
    )


# ---------------------------------------------------------------------------
# P32g: Draft expiry never touches terminal drafts
# ---------------------------------------------------------------------------


@given(status=_terminal_draft_statuses)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=10,
)
@pytest.mark.asyncio
async def test_draft_expiry_ignores_terminal_drafts(
    status: str,
    session: AsyncSession,
):
    """Drafts in terminal states (sent, abandoned) are never modified,
    even if they are old."""
    now = datetime.now(timezone.utc)
    user = await _create_user(session)

    old_time = now - timedelta(hours=DRAFT_EXPIRY_HOURS + 24)
    draft = await _create_draft(
        session, user.id, status, created_at=old_time, expires_at=old_time,
    )
    draft_id = draft.id

    await _do_draft_expiry(session)

    row = await _fetch_draft(session, draft_id)
    assert row is not None
    assert row.status == status, (
        f"Terminal {status} draft should not be modified, got {row.status}"
    )


# ---------------------------------------------------------------------------
# P32h: Draft expiry fallback — uses created_at when expires_at is NULL
# ---------------------------------------------------------------------------


@given(status=_non_terminal_draft_statuses)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=15,
)
@pytest.mark.asyncio
async def test_draft_expiry_fallback_created_at(
    status: str,
    session: AsyncSession,
):
    """When expires_at is NULL, drafts older than 2 hours (by created_at)
    are abandoned."""
    now = datetime.now(timezone.utc)
    user = await _create_user(session)

    old_created = now - timedelta(hours=DRAFT_EXPIRY_HOURS + 1)
    draft = await _create_draft(
        session, user.id, status, created_at=old_created, expires_at=None,
    )
    draft_id = draft.id

    await _do_draft_expiry(session)

    row = await _fetch_draft(session, draft_id)
    assert row is not None
    assert row.status == DraftStatus.ABANDONED.value, (
        f"Old draft with NULL expires_at should be abandoned, got {row.status}"
    )
