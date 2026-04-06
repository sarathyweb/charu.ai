"""Property tests for planner idempotency (P33).

P33 — Planner idempotency: running the daily planner or catch-up sweep
      multiple times for the same user/date never creates duplicate
      planned CallLog entries.  The partial unique index
      ``ix_call_log_planned_unique`` on ``(user_id, call_type, call_date)
      WHERE occurrence_kind='planned'`` enforces this at the DB level.

Validates: Design §3 Call Scheduling section
"""

from __future__ import annotations

from datetime import date, time, timedelta

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

_target_dates = st.dates(min_value=date(2025, 1, 1), max_value=date(2027, 12, 31))

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
        phone=f"+1555800{_phone_counter:04d}",
        timezone=tz,
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


async def _count_planned_calls(
    session: AsyncSession,
    user_id: int,
    call_type: str,
    call_date: date,
) -> int:
    """Count planned CallLog entries for a specific user/type/date."""
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
# P33a: _materialize_call is idempotent — second call returns False
# ---------------------------------------------------------------------------


@given(window_type=_window_types, target_date=_target_dates)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=30,
)
@pytest.mark.asyncio
async def test_materialize_call_idempotent(
    window_type: str,
    target_date: date,
    session: AsyncSession,
):
    """Calling _materialize_call twice for the same user/window/date
    creates exactly one planned CallLog row.  The second call returns
    False (duplicate skipped)."""
    user = await _create_user(session)
    window = await _create_window(session, user.id, window_type)

    first = await _materialize_call(session, user, window, target_date)
    second = await _materialize_call(session, user, window, target_date)

    assert first is True, "First materialization should create a row"
    assert second is False, "Second materialization should skip (idempotent)"

    count = await _count_planned_calls(
        session, user.id, window.window_type, target_date
    )
    assert count == 1, f"Expected exactly 1 planned call, got {count}"


# ---------------------------------------------------------------------------
# P33b: _materialize_for_user is idempotent across all windows
# ---------------------------------------------------------------------------


@given(target_date=_target_dates)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=20,
)
@pytest.mark.asyncio
async def test_materialize_for_user_idempotent(
    target_date: date,
    session: AsyncSession,
):
    """Running _materialize_for_user twice for the same user/date
    creates exactly one planned CallLog per active window."""
    user = await _create_user(session)
    await _create_window(session, user.id, WindowType.MORNING.value, time(7, 0), time(9, 0))
    await _create_window(session, user.id, WindowType.AFTERNOON.value, time(13, 0), time(15, 0))
    await _create_window(session, user.id, WindowType.EVENING.value, time(20, 0), time(21, 30))

    created_1, total_1 = await _materialize_for_user(session, user, target_date)
    created_2, total_2 = await _materialize_for_user(session, user, target_date)

    assert created_1 == 3, f"First run should create 3 calls, got {created_1}"
    assert total_1 == 3
    assert created_2 == 0, f"Second run should create 0 calls, got {created_2}"
    assert total_2 == 3

    # Verify exactly one planned row per window type
    for wt in [WindowType.MORNING.value, WindowType.AFTERNOON.value, WindowType.EVENING.value]:
        count = await _count_planned_calls(session, user.id, wt, target_date)
        assert count == 1, f"Expected 1 planned {wt} call, got {count}"


# ---------------------------------------------------------------------------
# P33c: Different dates produce separate rows (no cross-date collision)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_different_dates_are_independent(session: AsyncSession):
    """Materializing the same window for two different dates creates
    two separate planned CallLog rows."""
    user = await _create_user(session)
    window = await _create_window(session, user.id, WindowType.MORNING.value)

    date_a = date(2026, 6, 15)
    date_b = date(2026, 6, 16)

    res_a = await _materialize_call(session, user, window, date_a)
    res_b = await _materialize_call(session, user, window, date_b)

    assert res_a is True
    assert res_b is True

    count_a = await _count_planned_calls(session, user.id, window.window_type, date_a)
    count_b = await _count_planned_calls(session, user.id, window.window_type, date_b)
    assert count_a == 1
    assert count_b == 1


# ---------------------------------------------------------------------------
# P33d: Different users on the same date are independent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_different_users_are_independent(session: AsyncSession):
    """Two users can each have a planned morning call on the same date."""
    user_a = await _create_user(session)
    user_b = await _create_user(session)
    window_a = await _create_window(session, user_a.id, WindowType.MORNING.value)
    window_b = await _create_window(session, user_b.id, WindowType.MORNING.value)

    target = date(2026, 6, 15)

    assert await _materialize_call(session, user_a, window_a, target) is True
    assert await _materialize_call(session, user_b, window_b, target) is True

    count_a = await _count_planned_calls(session, user_a.id, WindowType.MORNING.value, target)
    count_b = await _count_planned_calls(session, user_b.id, WindowType.MORNING.value, target)
    assert count_a == 1
    assert count_b == 1


# ---------------------------------------------------------------------------
# P33e: Non-planned occurrence kinds don't block planned materialization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_planned_does_not_block_planned(session: AsyncSession):
    """A retry or rescheduled CallLog for the same user/type/date does
    not prevent the planner from creating a planned entry."""
    user = await _create_user(session)
    window = await _create_window(session, user.id, WindowType.MORNING.value)
    target = date(2026, 6, 15)

    # Insert a retry row for the same user/type/date
    from datetime import datetime, timezone as tz

    retry_log = CallLog(
        user_id=user.id,
        call_type=WindowType.MORNING.value,
        call_date=target,
        scheduled_time=datetime(2026, 6, 15, 12, 0, tzinfo=tz.utc),
        scheduled_timezone="America/New_York",
        status=CallLogStatus.SCHEDULED.value,
        occurrence_kind=OccurrenceKind.RETRY.value,
        attempt_number=2,
    )
    session.add(retry_log)
    await session.flush()

    # Planner should still be able to create a planned entry
    result = await _materialize_call(session, user, window, target)
    assert result is True, "Planned materialization should succeed despite existing retry row"

    count = await _count_planned_calls(session, user.id, WindowType.MORNING.value, target)
    assert count == 1


# ---------------------------------------------------------------------------
# P33f: Materialized CallLog has correct field values
# ---------------------------------------------------------------------------


@given(window_type=_window_types)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=15,
)
@pytest.mark.asyncio
async def test_materialized_call_has_correct_fields(
    window_type: str,
    session: AsyncSession,
):
    """A materialized CallLog has the expected status, occurrence_kind,
    attempt_number, and references the correct user/window."""
    user = await _create_user(session)
    window = await _create_window(session, user.id, window_type)
    target = date(2026, 7, 1)

    await _materialize_call(session, user, window, target)

    result = await session.exec(
        select(CallLog).where(
            CallLog.user_id == user.id,
            CallLog.call_type == window_type,
            CallLog.call_date == target,
            CallLog.occurrence_kind == OccurrenceKind.PLANNED.value,
        )
    )
    row = result.one()

    assert row.status == CallLogStatus.SCHEDULED.value
    assert row.occurrence_kind == OccurrenceKind.PLANNED.value
    assert row.attempt_number == 1
    assert row.origin_window_id == window.id
    assert row.scheduled_timezone == user.timezone
    assert row.scheduled_time is not None
    assert row.scheduled_time.tzinfo is not None  # UTC-aware
