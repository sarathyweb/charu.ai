"""Property tests for at-most-once dispatch (P34) and cancellation prevents dispatch (P35).

P34 — At-most-once call dispatch: For any CallLog entry, at most one Twilio
      outbound call is placed, even if the Celery trigger task executes more
      than once.  The atomic ``UPDATE … WHERE status='scheduled' AND version=?``
      claim pattern ensures only one worker proceeds to the Twilio API.

P35 — Cancellation prevents future dispatch: For any CallLog entry that has
      been successfully cancelled or skipped (or is in any non-scheduled state),
      no new outbound Twilio call may be placed for that entry.  The dispatcher's
      atomic claim rejects non-scheduled entries.

**Validates: Design Concurrency Notes**
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import update as sa_update
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.call_log import CallLog
from app.models.enums import (
    CallLogStatus,
    CallType,
    OccurrenceKind,
)
from app.models.user import User

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_call_types = st.sampled_from([ct.value for ct in CallType])

_non_scheduled_statuses = st.sampled_from([
    CallLogStatus.DISPATCHING,
    CallLogStatus.RINGING,
    CallLogStatus.IN_PROGRESS,
    CallLogStatus.COMPLETED,
    CallLogStatus.MISSED,
    CallLogStatus.DEFERRED,
    CallLogStatus.CANCELLED,
    CallLogStatus.SKIPPED,
])

_terminal_statuses = st.sampled_from([
    CallLogStatus.COMPLETED,
    CallLogStatus.MISSED,
    CallLogStatus.DEFERRED,
    CallLogStatus.CANCELLED,
    CallLogStatus.SKIPPED,
])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_phone_counter = 0


async def _create_user(
    session: AsyncSession,
    tz: str = "America/New_York",
) -> User:
    global _phone_counter
    _phone_counter += 1
    user = User(
        phone=f"+1555700{_phone_counter:04d}",
        timezone=tz,
        onboarding_complete=True,
        consecutive_active_days=0,
    )
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return user


def _make_due_call_log(
    user_id: int,
    call_type: str = CallType.MORNING.value,
    status: str = CallLogStatus.SCHEDULED.value,
    tz: str = "America/New_York",
    version: int = 1,
) -> CallLog:
    """Build a CallLog that is already due (scheduled_time in the past)."""
    scheduled = datetime.now(timezone.utc) - timedelta(minutes=5)
    return CallLog(
        user_id=user_id,
        call_type=call_type,
        call_date=scheduled.date(),
        scheduled_time=scheduled,
        scheduled_timezone=tz,
        status=status,
        occurrence_kind=OccurrenceKind.PLANNED.value,
        attempt_number=1,
        version=version,
    )


async def _atomic_claim(session: AsyncSession, call_log_id: int, version: int) -> int:
    """Simulate the dispatcher's atomic claim pattern.

    Returns the number of rows affected (0 = already claimed / not scheduled,
    1 = successfully claimed).
    """
    stmt = (
        sa_update(CallLog)
        .where(
            CallLog.id == call_log_id,
            CallLog.status == CallLogStatus.SCHEDULED.value,
            CallLog.version == version,
        )
        .values(
            status=CallLogStatus.DISPATCHING.value,
            version=version + 1,
            updated_at=datetime.now(timezone.utc),
        )
    )
    result = await session.exec(stmt)  # type: ignore[call-overload]
    await session.flush()
    return result.rowcount  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# P34a: Atomic claim succeeds exactly once — second attempt returns 0 rows
# ---------------------------------------------------------------------------


@given(call_type=_call_types)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=30,
)
@pytest.mark.asyncio
async def test_atomic_claim_succeeds_at_most_once(
    call_type: str,
    session: AsyncSession,
):
    """Running the atomic claim twice on the same row succeeds exactly once.
    The second attempt sees status='dispatching' and returns rowcount=0."""
    user = await _create_user(session)
    cl = _make_due_call_log(user.id, call_type=call_type)
    session.add(cl)
    await session.flush()
    await session.refresh(cl)

    first = await _atomic_claim(session, cl.id, cl.version)
    assert first == 1, "First claim should succeed"

    # Re-read to get current version
    await session.refresh(cl)
    assert cl.status == CallLogStatus.DISPATCHING.value

    # Second claim with the OLD version should fail (version mismatch)
    second = await _atomic_claim(session, cl.id, cl.version - 1)
    assert second == 0, "Second claim with stale version should fail"

    # Second claim with the NEW version should also fail (status != scheduled)
    third = await _atomic_claim(session, cl.id, cl.version)
    assert third == 0, "Claim on dispatching row should fail"


# ---------------------------------------------------------------------------
# P34b: Multiple due rows — each claimed at most once across two passes
# ---------------------------------------------------------------------------


@given(num_rows=st.integers(min_value=1, max_value=5))
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=20,
)
@pytest.mark.asyncio
async def test_multiple_due_rows_each_claimed_at_most_once(
    num_rows: int,
    session: AsyncSession,
):
    """Simulating two dispatcher passes over the same due rows: each row
    is claimed at most once across both passes."""
    user = await _create_user(session)
    call_types = [CallType.MORNING.value, CallType.AFTERNOON.value,
                  CallType.EVENING.value, CallType.ON_DEMAND.value]

    rows: list[tuple[int, int]] = []  # (id, version)
    for i in range(num_rows):
        cl = _make_due_call_log(
            user.id,
            call_type=call_types[i % len(call_types)],
        )
        # Use RETRY occurrence_kind to avoid planned unique index conflicts
        if i > 0:
            cl.occurrence_kind = OccurrenceKind.RETRY.value
            cl.attempt_number = i + 1
        session.add(cl)
        await session.flush()
        await session.refresh(cl)
        rows.append((cl.id, cl.version))

    # Pass 1: claim all rows
    pass1_claimed = 0
    for cid, ver in rows:
        rc = await _atomic_claim(session, cid, ver)
        pass1_claimed += rc

    assert pass1_claimed == num_rows, "First pass should claim all rows"

    # Pass 2: attempt to claim the same rows again (stale versions)
    pass2_claimed = 0
    for cid, ver in rows:
        rc = await _atomic_claim(session, cid, ver)
        pass2_claimed += rc

    assert pass2_claimed == 0, "Second pass should claim zero rows"


# ---------------------------------------------------------------------------
# P34c: Already-dispatching row cannot be claimed again
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatching_row_not_claimable(session: AsyncSession):
    """A CallLog already in 'dispatching' status cannot be claimed."""
    user = await _create_user(session)
    cl = _make_due_call_log(user.id, status=CallLogStatus.DISPATCHING.value, version=2)
    session.add(cl)
    await session.flush()
    await session.refresh(cl)

    rc = await _atomic_claim(session, cl.id, cl.version)
    assert rc == 0, "Dispatching row should not be claimable"


# ---------------------------------------------------------------------------
# P34d: Terminal-status rows cannot be claimed
# ---------------------------------------------------------------------------


@given(terminal=_terminal_statuses)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=20,
)
@pytest.mark.asyncio
async def test_terminal_status_not_claimable(
    terminal: CallLogStatus,
    session: AsyncSession,
):
    """A CallLog in any terminal status cannot be claimed by the dispatcher."""
    user = await _create_user(session)
    cl = _make_due_call_log(user.id, status=terminal.value)
    session.add(cl)
    await session.flush()
    await session.refresh(cl)

    rc = await _atomic_claim(session, cl.id, cl.version)
    assert rc == 0, f"Row with status={terminal.value} should not be claimable"


# ---------------------------------------------------------------------------
# P35a: Cancelled CallLog is never dispatched
# ---------------------------------------------------------------------------


@given(call_type=_call_types)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=20,
)
@pytest.mark.asyncio
async def test_cancelled_prevents_dispatch(
    call_type: str,
    session: AsyncSession,
):
    """A CallLog with status='cancelled' is never dispatched, even if
    scheduled_time <= now()."""
    user = await _create_user(session)
    cl = _make_due_call_log(user.id, call_type=call_type,
                            status=CallLogStatus.CANCELLED.value)
    session.add(cl)
    await session.flush()
    await session.refresh(cl)

    rc = await _atomic_claim(session, cl.id, cl.version)
    assert rc == 0, "Cancelled row must not be dispatched"

    await session.refresh(cl)
    assert cl.status == CallLogStatus.CANCELLED.value, "Status must remain cancelled"


# ---------------------------------------------------------------------------
# P35b: Skipped CallLog is never dispatched
# ---------------------------------------------------------------------------


@given(call_type=_call_types)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=20,
)
@pytest.mark.asyncio
async def test_skipped_prevents_dispatch(
    call_type: str,
    session: AsyncSession,
):
    """A CallLog with status='skipped' is never dispatched."""
    user = await _create_user(session)
    cl = _make_due_call_log(user.id, call_type=call_type,
                            status=CallLogStatus.SKIPPED.value)
    session.add(cl)
    await session.flush()
    await session.refresh(cl)

    rc = await _atomic_claim(session, cl.id, cl.version)
    assert rc == 0, "Skipped row must not be dispatched"

    await session.refresh(cl)
    assert cl.status == CallLogStatus.SKIPPED.value, "Status must remain skipped"


# ---------------------------------------------------------------------------
# P35c: No non-scheduled status is ever dispatched (generalized)
# ---------------------------------------------------------------------------


@given(status=_non_scheduled_statuses, call_type=_call_types)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=40,
)
@pytest.mark.asyncio
async def test_non_scheduled_status_prevents_dispatch(
    status: CallLogStatus,
    call_type: str,
    session: AsyncSession,
):
    """The atomic claim pattern rejects any CallLog whose status is not
    'scheduled', regardless of what the status actually is."""
    user = await _create_user(session)
    cl = _make_due_call_log(user.id, call_type=call_type, status=status.value)
    session.add(cl)
    await session.flush()
    await session.refresh(cl)

    original_status = cl.status
    rc = await _atomic_claim(session, cl.id, cl.version)
    assert rc == 0, f"Row with status={status.value} must not be claimable"

    await session.refresh(cl)
    assert cl.status == original_status, "Status must not change on failed claim"


# ---------------------------------------------------------------------------
# P35d: Completed and missed rows are never dispatched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completed_prevents_dispatch(session: AsyncSession):
    """A completed CallLog is never dispatched."""
    user = await _create_user(session)
    cl = _make_due_call_log(user.id, status=CallLogStatus.COMPLETED.value)
    session.add(cl)
    await session.flush()
    await session.refresh(cl)

    rc = await _atomic_claim(session, cl.id, cl.version)
    assert rc == 0


@pytest.mark.asyncio
async def test_missed_prevents_dispatch(session: AsyncSession):
    """A missed CallLog is never dispatched."""
    user = await _create_user(session)
    cl = _make_due_call_log(user.id, status=CallLogStatus.MISSED.value)
    session.add(cl)
    await session.flush()
    await session.refresh(cl)

    rc = await _atomic_claim(session, cl.id, cl.version)
    assert rc == 0


@pytest.mark.asyncio
async def test_deferred_prevents_dispatch(session: AsyncSession):
    """A deferred CallLog is never dispatched."""
    user = await _create_user(session)
    cl = _make_due_call_log(user.id, status=CallLogStatus.DEFERRED.value)
    session.add(cl)
    await session.flush()
    await session.refresh(cl)

    rc = await _atomic_claim(session, cl.id, cl.version)
    assert rc == 0


# ---------------------------------------------------------------------------
# P34e: Version mismatch alone prevents claim (even if status is scheduled)
# ---------------------------------------------------------------------------


@given(wrong_version=st.integers(min_value=2, max_value=100))
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=20,
)
@pytest.mark.asyncio
async def test_version_mismatch_prevents_claim(
    wrong_version: int,
    session: AsyncSession,
):
    """Even if status is 'scheduled', a version mismatch prevents the claim.
    This simulates a concurrent modification between the SELECT and UPDATE."""
    user = await _create_user(session)
    cl = _make_due_call_log(user.id)
    session.add(cl)
    await session.flush()
    await session.refresh(cl)

    assert cl.version == 1
    # Attempt claim with wrong version
    rc = await _atomic_claim(session, cl.id, wrong_version)
    assert rc == 0, "Version mismatch should prevent claim"

    await session.refresh(cl)
    assert cl.status == CallLogStatus.SCHEDULED.value, "Status must remain scheduled"
    assert cl.version == 1, "Version must not change"
