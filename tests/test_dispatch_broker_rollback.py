"""Regression tests for due-row dispatcher broker-publish rollback.

Verifies that when celery_app.send_task() fails (broker outage), the
dispatcher reverts the CallLog from 'dispatching' back to 'scheduled'
so the next sweep can re-claim it.

These tests simulate the dispatcher's claim-then-revert sequence directly
against the test DB, matching the exact SQL patterns used in
_run_due_row_dispatcher.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update as sa_update
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.call_log import CallLog
from app.models.enums import CallLogStatus, CallType, OccurrenceKind
from app.models.user import User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_phone_counter = 0


async def _create_user(session: AsyncSession) -> User:
    global _phone_counter
    _phone_counter += 1
    user = User(
        phone=f"+1555800{_phone_counter:04d}",
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


async def _revert_to_scheduled(
    session: AsyncSession, call_log_id: int, version: int
) -> int:
    """Simulate the dispatcher's revert: dispatching → scheduled.

    This is the exact pattern from _run_due_row_dispatcher's broker-failure
    handler.  *version* is the version the row was at when it was claimed
    (i.e. the original version before claim incremented it).
    """
    stmt = (
        sa_update(CallLog)
        .where(
            CallLog.id == call_log_id,
            CallLog.status == CallLogStatus.DISPATCHING.value,
            CallLog.version == version + 1,
        )
        .values(
            status=CallLogStatus.SCHEDULED.value,
            version=version + 2,
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
async def test_broker_failure_revert_restores_scheduled(session: AsyncSession):
    """After claim (scheduled→dispatching) and a simulated broker failure,
    the revert (dispatching→scheduled) restores the row so the next
    dispatcher sweep can re-claim it."""
    user = await _create_user(session)
    cl = _make_due_call_log(user.id)
    session.add(cl)
    await session.flush()
    await session.refresh(cl)

    original_version = cl.version  # 1

    # Step 1: Claim succeeds
    rc = await _atomic_claim(session, cl.id, original_version)
    assert rc == 1

    await session.refresh(cl)
    assert cl.status == CallLogStatus.DISPATCHING.value
    assert cl.version == original_version + 1

    # Step 2: Broker publish fails (simulated) — revert
    rc = await _revert_to_scheduled(session, cl.id, original_version)
    assert rc == 1, "Revert should affect exactly one row"

    await session.refresh(cl)
    assert cl.status == CallLogStatus.SCHEDULED.value, (
        "Row should be back to scheduled after broker failure revert"
    )
    assert cl.version == original_version + 2


@pytest.mark.asyncio
async def test_reverted_row_is_reclaimable(session: AsyncSession):
    """After a broker-failure revert, the row can be claimed again by
    the next dispatcher sweep."""
    user = await _create_user(session)
    cl = _make_due_call_log(user.id)
    session.add(cl)
    await session.flush()
    await session.refresh(cl)

    original_version = cl.version  # 1

    # Claim → revert cycle
    await _atomic_claim(session, cl.id, original_version)
    await _revert_to_scheduled(session, cl.id, original_version)

    await session.refresh(cl)
    assert cl.status == CallLogStatus.SCHEDULED.value
    new_version = cl.version  # 3

    # Second claim should succeed
    rc = await _atomic_claim(session, cl.id, new_version)
    assert rc == 1, "Reverted row should be reclaimable"

    await session.refresh(cl)
    assert cl.status == CallLogStatus.DISPATCHING.value
    assert cl.version == new_version + 1


@pytest.mark.asyncio
async def test_revert_is_version_fenced(session: AsyncSession):
    """The revert only succeeds if the version matches — a concurrent
    modification (e.g. another worker already processed the row) prevents
    the revert from clobbering."""
    user = await _create_user(session)
    cl = _make_due_call_log(user.id)
    session.add(cl)
    await session.flush()
    await session.refresh(cl)

    original_version = cl.version  # 1

    # Claim
    await _atomic_claim(session, cl.id, original_version)

    # Simulate a concurrent modification: bump version further
    stmt = (
        sa_update(CallLog)
        .where(CallLog.id == cl.id)
        .values(version=original_version + 5)
    )
    await session.exec(stmt)  # type: ignore[call-overload]
    await session.flush()

    # Revert with the original version should fail (version mismatch)
    rc = await _revert_to_scheduled(session, cl.id, original_version)
    assert rc == 0, "Revert with stale version should not affect any rows"


@pytest.mark.asyncio
async def test_revert_only_affects_dispatching_status(session: AsyncSession):
    """The revert WHERE clause requires status='dispatching'.  If the row
    has already moved to another status (e.g. ringing), the revert is a no-op."""
    user = await _create_user(session)
    cl = _make_due_call_log(user.id)
    session.add(cl)
    await session.flush()
    await session.refresh(cl)

    original_version = cl.version  # 1

    # Claim
    await _atomic_claim(session, cl.id, original_version)

    # Simulate the row moving to 'ringing' (trigger_call_task succeeded)
    stmt = (
        sa_update(CallLog)
        .where(CallLog.id == cl.id)
        .values(
            status=CallLogStatus.RINGING.value,
            version=original_version + 2,
        )
    )
    await session.exec(stmt)  # type: ignore[call-overload]
    await session.flush()

    # Revert should be a no-op (status is ringing, not dispatching)
    rc = await _revert_to_scheduled(session, cl.id, original_version)
    assert rc == 0, "Revert should not affect a row that moved past dispatching"

    await session.refresh(cl)
    assert cl.status == CallLogStatus.RINGING.value
