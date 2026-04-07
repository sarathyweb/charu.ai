"""Property tests for planner respecting user cancellations (P45).

P45 — Planner respects user-cancelled/skipped occurrences: for any
      CallLog entry that was explicitly cancelled or skipped by the user,
      the daily planner does NOT recreate a replacement planned occurrence
      for the same user/type/date.  The partial unique index on
      ``(user_id, call_type, call_date) WHERE occurrence_kind='planned'``
      prevents this at the DB level.

      System-initiated rematerialization (window edits, timezone changes)
      uses hard-delete of ``scheduled`` planned rows, which does not
      conflict — user-cancelled/skipped rows have non-``scheduled`` status
      and are never deleted by rematerialization.

Validates: Design §3 Call Scheduling section
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone as tz

import pytest
import pytest_asyncio
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.call_log import CallLog
from app.models.call_window import CallWindow
from app.models.enums import (
    CallLogStatus,
    CallType,
    OccurrenceKind,
    WindowType,
)
from app.models.user import User
from app.tasks.calls import _materialize_call, _materialize_for_user

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_window_types = st.sampled_from([
    WindowType.MORNING.value,
    WindowType.AFTERNOON.value,
    WindowType.EVENING.value,
])

_cancelled_statuses = st.sampled_from([
    CallLogStatus.CANCELLED.value,
    CallLogStatus.SKIPPED.value,
])

_target_dates = st.dates(min_value=date(2025, 1, 1), max_value=date(2027, 12, 31))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_phone_counter = 0


async def _create_user(
    session: AsyncSession,
    tz_name: str = "America/New_York",
) -> User:
    global _phone_counter
    _phone_counter += 1
    user = User(
        phone=f"+1555900{_phone_counter:04d}",
        timezone=tz_name,
        onboarding_complete=True,
        consecutive_active_days=0,
    )
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return user


async def _create_window(
    session: AsyncSession,
    user_id: int,
    window_type: str,
    start: time = time(7, 0),
    end: time = time(9, 0),
) -> CallWindow:
    window = CallWindow(
        user_id=user_id,
        window_type=window_type,
        start_time=start,
        end_time=end,
        is_active=True,
    )
    session.add(window)
    await session.flush()
    await session.refresh(window)
    return window


async def _insert_planned_call(
    session: AsyncSession,
    user: User,
    window_type: str,
    target_date: date,
    status: str = CallLogStatus.SCHEDULED.value,
) -> CallLog:
    """Insert a planned CallLog entry with the given status."""
    call_log = CallLog(
        user_id=user.id,
        call_type=window_type,
        call_date=target_date,
        scheduled_time=datetime(
            target_date.year, target_date.month, target_date.day,
            12, 0, tzinfo=tz.utc,
        ),
        scheduled_timezone=user.timezone,
        status=status,
        occurrence_kind=OccurrenceKind.PLANNED.value,
        attempt_number=1,
    )
    session.add(call_log)
    await session.flush()
    await session.refresh(call_log)
    return call_log


async def _count_planned_calls(
    session: AsyncSession,
    user_id: int,
    call_type: str,
    call_date: date,
) -> int:
    result = await session.exec(
        select(CallLog).where(
            CallLog.user_id == user_id,
            CallLog.call_type == call_type,
            CallLog.call_date == call_date,
            CallLog.occurrence_kind == OccurrenceKind.PLANNED.value,
        )
    )
    return len(list(result.all()))


# ---------------------------------------------------------------------------
# P45a: Planner cannot recreate a cancelled/skipped planned call
# ---------------------------------------------------------------------------
# Feature: accountability-call-onboarding, Property 45: Planner respects user-cancelled/skipped occurrences


@given(
    window_type=_window_types,
    target_date=_target_dates,
    terminal_status=_cancelled_statuses,
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=30,
)
@pytest.mark.asyncio
async def test_planner_skips_cancelled_or_skipped_date(
    window_type: str,
    target_date: date,
    terminal_status: str,
    session: AsyncSession,
):
    """When a planned CallLog has been cancelled or skipped, calling
    _materialize_call for the same user/window/date returns False and
    does NOT create a second planned row.  The partial unique index
    blocks the insert because the original row still has
    occurrence_kind='planned'."""
    user = await _create_user(session)
    window = await _create_window(session, user.id, window_type)

    # Simulate user cancelling/skipping: insert a planned row then
    # change its status to cancelled or skipped.
    existing = await _insert_planned_call(
        session, user, window_type, target_date, status=terminal_status,
    )
    assert existing.occurrence_kind == OccurrenceKind.PLANNED.value
    assert existing.status == terminal_status

    # Planner tries to materialize — should be blocked
    result = await _materialize_call(session, user, window, target_date)
    assert result is False, (
        f"Planner should NOT recreate a planned call when an existing "
        f"planned row has status={terminal_status}"
    )

    count = await _count_planned_calls(session, user.id, window_type, target_date)
    assert count == 1, (
        f"Expected exactly 1 planned row (the {terminal_status} one), got {count}"
    )


# ---------------------------------------------------------------------------
# P45b: _materialize_for_user skips all cancelled/skipped windows
# ---------------------------------------------------------------------------


@given(target_date=_target_dates, terminal_status=_cancelled_statuses)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=20,
)
@pytest.mark.asyncio
async def test_materialize_for_user_skips_all_cancelled(
    target_date: date,
    terminal_status: str,
    session: AsyncSession,
):
    """When ALL three windows have been cancelled/skipped for a date,
    _materialize_for_user creates zero new rows."""
    user = await _create_user(session)
    for wt, s, e in [
        (WindowType.MORNING.value, time(7, 0), time(9, 0)),
        (WindowType.AFTERNOON.value, time(13, 0), time(15, 0)),
        (WindowType.EVENING.value, time(20, 0), time(21, 30)),
    ]:
        await _create_window(session, user.id, wt, s, e)
        await _insert_planned_call(session, user, wt, target_date, terminal_status)

    created, total = await _materialize_for_user(session, user, target_date)
    assert created == 0, (
        f"Expected 0 new calls when all windows are {terminal_status}, got {created}"
    )
    assert total == 3

    # Still exactly one planned row per window type
    for wt in [WindowType.MORNING.value, WindowType.AFTERNOON.value, WindowType.EVENING.value]:
        count = await _count_planned_calls(session, user.id, wt, target_date)
        assert count == 1


# ---------------------------------------------------------------------------
# P45c: Partial cancellation — planner creates only for non-cancelled windows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_cancellation_allows_remaining_windows(
    session: AsyncSession,
):
    """If only one window is cancelled, the planner still materializes
    the other windows normally."""
    user = await _create_user(session)
    target = date(2026, 8, 15)

    # Morning: cancelled by user
    await _create_window(session, user.id, WindowType.MORNING.value, time(7, 0), time(9, 0))
    await _insert_planned_call(
        session, user, WindowType.MORNING.value, target,
        status=CallLogStatus.CANCELLED.value,
    )

    # Afternoon + evening: no existing planned rows
    await _create_window(session, user.id, WindowType.AFTERNOON.value, time(13, 0), time(15, 0))
    await _create_window(session, user.id, WindowType.EVENING.value, time(20, 0), time(21, 30))

    created, total = await _materialize_for_user(session, user, target)
    assert created == 2, f"Expected 2 new calls (afternoon + evening), got {created}"
    assert total == 3

    # Morning still has exactly 1 (the cancelled one)
    assert await _count_planned_calls(
        session, user.id, WindowType.MORNING.value, target
    ) == 1
    # Afternoon and evening each have 1 (newly created)
    assert await _count_planned_calls(
        session, user.id, WindowType.AFTERNOON.value, target
    ) == 1
    assert await _count_planned_calls(
        session, user.id, WindowType.EVENING.value, target
    ) == 1


# ---------------------------------------------------------------------------
# P45d: Other terminal statuses (completed, missed, deferred) also block
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", [
    CallLogStatus.COMPLETED.value,
    CallLogStatus.MISSED.value,
    CallLogStatus.DEFERRED.value,
])
async def test_other_terminal_statuses_also_block_planner(
    terminal_status: str,
    session: AsyncSession,
):
    """Any planned row — regardless of terminal status — occupies the
    partial unique index slot and blocks re-materialization."""
    user = await _create_user(session)
    window = await _create_window(session, user.id, WindowType.MORNING.value)
    target = date(2026, 9, 1)

    await _insert_planned_call(session, user, WindowType.MORNING.value, target, terminal_status)

    result = await _materialize_call(session, user, window, target)
    assert result is False, (
        f"Planner should not recreate when existing planned row has status={terminal_status}"
    )


# ---------------------------------------------------------------------------
# P45e: Cancelled/skipped row is NOT deleted by the planner
# ---------------------------------------------------------------------------


@given(
    window_type=_window_types,
    target_date=_target_dates,
    terminal_status=_cancelled_statuses,
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=20,
)
@pytest.mark.asyncio
async def test_cancelled_row_preserved_after_planner_run(
    window_type: str,
    target_date: date,
    terminal_status: str,
    session: AsyncSession,
):
    """The planner never deletes or modifies a cancelled/skipped planned
    row.  After a planner run, the original row is still present with
    its original status."""
    user = await _create_user(session)
    window = await _create_window(session, user.id, window_type)

    original = await _insert_planned_call(
        session, user, window_type, target_date, terminal_status,
    )
    original_id = original.id

    # Run planner — should be a no-op
    await _materialize_call(session, user, window, target_date)

    # Verify original row is untouched
    await session.refresh(original)
    assert original.id == original_id
    assert original.status == terminal_status
    assert original.occurrence_kind == OccurrenceKind.PLANNED.value
