"""Regression test: stale worker cannot mutate a row after sweep reclaims it.

Scenario under test:
  1. Worker A claims a due row (scheduled→dispatching, v1→v2).
  2. Worker A is slow (e.g. Twilio timeout).  The stale dispatching sweep
     reclaims the row (dispatching→scheduled, v2→v3).
  3. Worker B claims the reclaimed row (scheduled→dispatching, v3→v4).
  4. Worker A finally finishes and tries to transition with its stale
     current_version (v2).  The CAS must fail — worker A no longer owns
     the row.

This covers the ABA/duplicate-dispatch class of bug that the version
field is designed to prevent.  The fix in stale_dispatching_sweep
(bumping version on reclaim) is what makes step 4 fail correctly.

Validates: Property 34 (at-most-once call dispatch), fix for
stale_dispatching_sweep version bump.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update as sa_update
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.call_log import CallLog
from app.models.enums import CallLogStatus, CallType, OccurrenceKind
from app.models.user import User
from app.services.call_log_service import (
    CallLogService,
    StaleVersionError,
)


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


def _make_due_call_log(user_id: int) -> CallLog:
    scheduled = datetime.now(timezone.utc) - timedelta(minutes=5)
    return CallLog(
        user_id=user_id,
        call_type=CallType.MORNING.value,
        call_date=scheduled.date(),
        scheduled_time=scheduled,
        scheduled_timezone="America/New_York",
        status=CallLogStatus.SCHEDULED.value,
        occurrence_kind=OccurrenceKind.PLANNED.value,
        attempt_number=1,
        version=1,
    )


async def _atomic_claim(session: AsyncSession, call_log_id: int, version: int) -> int:
    """Simulate the dispatcher's atomic claim: scheduled → dispatching."""
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


async def _simulate_sweep_reclaim(
    session: AsyncSession, call_log_id: int
) -> int:
    """Simulate stale_dispatching_sweep: dispatching → scheduled with version bump.

    Matches the actual SQL in _run_stale_dispatching_sweep (after fix).
    """
    stmt = (
        sa_update(CallLog)
        .where(
            CallLog.id == call_log_id,
            CallLog.status == CallLogStatus.DISPATCHING.value,
        )
        .values(
            status=CallLogStatus.SCHEDULED.value,
            version=CallLog.version + 1,
            updated_at=datetime.now(timezone.utc),
        )
    )
    result = await session.exec(stmt)  # type: ignore[call-overload]
    await session.flush()
    return result.rowcount  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_worker_blocked_after_sweep_reclaim(session: AsyncSession):
    """Worker A's stale version is rejected after sweep reclaims the row.

    Timeline:
      1. Worker A claims (v1→v2), captures current_version=v2
      2. Sweep reclaims (v2→v3)
      3. Worker A tries dispatching→ringing with expected_version=v2 → fails
    """
    user = await _create_user(session)
    cl = _make_due_call_log(user.id)
    session.add(cl)
    await session.flush()
    await session.refresh(cl)
    assert cl.version == 1

    # Step 1: Worker A claims
    rc = await _atomic_claim(session, cl.id, 1)
    assert rc == 1
    await session.refresh(cl)
    assert cl.status == CallLogStatus.DISPATCHING.value
    assert cl.version == 2

    worker_a_version = 2  # what trigger_call_task captures as current_version

    # Step 2: Sweep reclaims the stuck row
    rc = await _simulate_sweep_reclaim(session, cl.id)
    assert rc == 1
    await session.refresh(cl)
    assert cl.status == CallLogStatus.SCHEDULED.value
    assert cl.version == 3

    # Step 3: Worker A tries to transition with its stale version
    svc = CallLogService(session)
    with pytest.raises(StaleVersionError):
        await svc.update_status(
            cl.id,
            CallLogStatus.RINGING,
            expected_version=worker_a_version,
            twilio_call_sid="CA_stale_worker",
        )


@pytest.mark.asyncio
async def test_stale_worker_blocked_after_sweep_and_reclaim(session: AsyncSession):
    """Full scenario: worker A claims, sweep reclaims, worker B re-claims,
    worker A's late transition is rejected.

    Timeline:
      1. Worker A claims (v1→v2), captures current_version=v2
      2. Sweep reclaims (v2→v3)
      3. Worker B claims (v3→v4)
      4. Worker A tries dispatching→ringing with expected_version=v2 → fails
    """
    user = await _create_user(session)
    cl = _make_due_call_log(user.id)
    session.add(cl)
    await session.flush()
    await session.refresh(cl)

    # Step 1: Worker A claims
    await _atomic_claim(session, cl.id, 1)
    worker_a_version = 2

    # Step 2: Sweep reclaims
    await _simulate_sweep_reclaim(session, cl.id)
    await session.refresh(cl)
    assert cl.version == 3

    # Step 3: Worker B claims
    rc = await _atomic_claim(session, cl.id, 3)
    assert rc == 1
    await session.refresh(cl)
    assert cl.status == CallLogStatus.DISPATCHING.value
    assert cl.version == 4

    # Step 4: Worker A tries to transition — must fail
    svc = CallLogService(session)
    with pytest.raises(StaleVersionError):
        await svc.update_status(
            cl.id,
            CallLogStatus.RINGING,
            expected_version=worker_a_version,
            twilio_call_sid="CA_stale_worker_a",
        )

    # Verify worker B's row is untouched
    await session.refresh(cl)
    assert cl.status == CallLogStatus.DISPATCHING.value
    assert cl.version == 4
    assert cl.twilio_call_sid is None


@pytest.mark.asyncio
async def test_stale_worker_revert_blocked_after_sweep(session: AsyncSession):
    """Worker A's error-recovery revert (dispatching→scheduled) is also
    blocked after the sweep has reclaimed and the version has moved on.

    This covers the Twilio 5xx / network error path in trigger_call_task.
    """
    user = await _create_user(session)
    cl = _make_due_call_log(user.id)
    session.add(cl)
    await session.flush()
    await session.refresh(cl)

    # Worker A claims
    await _atomic_claim(session, cl.id, 1)
    worker_a_version = 2

    # Sweep reclaims
    await _simulate_sweep_reclaim(session, cl.id)
    await session.refresh(cl)
    assert cl.status == CallLogStatus.SCHEDULED.value
    assert cl.version == 3

    # Worker A tries to revert (dispatching→scheduled) with stale version
    # This should fail because the row is already scheduled (status mismatch)
    # AND the version doesn't match
    svc = CallLogService(session)

    # The state machine allows dispatching→scheduled, but the row is
    # already in scheduled state, so the transition check fails
    from app.services.call_log_service import InvalidTransitionError

    with pytest.raises(InvalidTransitionError):
        await svc.update_status(
            cl.id,
            CallLogStatus.SCHEDULED,
            expected_version=worker_a_version,
        )


@pytest.mark.asyncio
async def test_worker_b_succeeds_after_sweep_reclaim(session: AsyncSession):
    """After sweep reclaims, worker B can successfully claim and transition
    the row through the full happy path."""
    user = await _create_user(session)
    cl = _make_due_call_log(user.id)
    session.add(cl)
    await session.flush()
    await session.refresh(cl)

    # Worker A claims, gets stuck
    await _atomic_claim(session, cl.id, 1)

    # Sweep reclaims
    await _simulate_sweep_reclaim(session, cl.id)
    await session.refresh(cl)
    assert cl.version == 3

    # Worker B claims
    await _atomic_claim(session, cl.id, 3)
    worker_b_version = 4

    # Worker B transitions to ringing — should succeed
    svc = CallLogService(session)
    updated = await svc.update_status(
        cl.id,
        CallLogStatus.RINGING,
        expected_version=worker_b_version,
        twilio_call_sid="CA_worker_b",
    )
    assert updated.status == CallLogStatus.RINGING.value
    assert updated.twilio_call_sid == "CA_worker_b"
    assert updated.version == 5
