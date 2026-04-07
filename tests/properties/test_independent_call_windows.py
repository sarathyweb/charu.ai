"""Property tests for independent call windows (Property 28).

Property 28 — Multiple call windows per user are independent:
  For *any* user with multiple call windows, operations on one window
  (save, update, deactivate, schedule materialization) do NOT affect
  the other windows or their scheduled CallLog entries.

Validates: Requirements 16.1, 16.4, 16.6
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest
from hypothesis import HealthCheck, given, settings, assume, strategies as st
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
from app.services.call_window_service import CallWindowService
from app.tasks.calls import _materialize_call

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Pairs of distinct window types for testing independence
_WINDOW_TYPES = [WindowType.MORNING.value, WindowType.AFTERNOON.value, WindowType.EVENING.value]

_distinct_window_pairs = st.sampled_from(
    [(a, b) for a in _WINDOW_TYPES for b in _WINDOW_TYPES if a != b]
)

_target_dates = st.dates(min_value=date(2025, 1, 1), max_value=date(2027, 12, 31))

# Non-overlapping time ranges for two windows
_time_offsets = st.tuples(
    st.integers(min_value=0, max_value=8),   # start hour for window A
    st.integers(min_value=12, max_value=18),  # start hour for window B
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_phone_counter = 0


async def _create_user(session: AsyncSession, tz: str = "America/New_York") -> User:
    global _phone_counter
    _phone_counter += 1
    user = User(
        phone=f"+1555900{_phone_counter:04d}",
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
    result = await session.exec(
        select(CallLog).where(
            CallLog.user_id == user_id,
            CallLog.call_type == call_type,
            CallLog.call_date == call_date,
            CallLog.occurrence_kind == OccurrenceKind.PLANNED.value,
        )
    )
    return len(list(result.all()))


async def _get_planned_call(
    session: AsyncSession,
    user_id: int,
    call_type: str,
    call_date: date,
) -> CallLog | None:
    result = await session.exec(
        select(CallLog).where(
            CallLog.user_id == user_id,
            CallLog.call_type == call_type,
            CallLog.call_date == call_date,
            CallLog.occurrence_kind == OccurrenceKind.PLANNED.value,
        )
    )
    return result.first()



# ===================================================================
# P28a: Materializing one window does not affect another window's calls
# ===================================================================


@given(
    window_pair=_distinct_window_pairs,
    target_date=_target_dates,
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=30,
)
@pytest.mark.asyncio
async def test_materialize_one_window_does_not_affect_other(
    window_pair: tuple[str, str],
    target_date: date,
    session: AsyncSession,
):
    """Materializing a call for window A does not create, modify, or
    delete any CallLog entries for window B."""
    wt_a, wt_b = window_pair

    user = await _create_user(session)
    win_a = await _create_window(session, user.id, wt_a, time(7, 0), time(9, 0))
    win_b = await _create_window(session, user.id, wt_b, time(13, 0), time(15, 0))

    # Materialize window B first
    await _materialize_call(session, user, win_b, target_date)
    call_b_before = await _get_planned_call(session, user.id, wt_b, target_date)
    assert call_b_before is not None
    b_scheduled_time = call_b_before.scheduled_time

    # Now materialize window A
    await _materialize_call(session, user, win_a, target_date)

    # Window B's call should be completely unchanged
    call_b_after = await _get_planned_call(session, user.id, wt_b, target_date)
    assert call_b_after is not None
    assert call_b_after.id == call_b_before.id
    assert call_b_after.scheduled_time == b_scheduled_time
    assert call_b_after.status == CallLogStatus.SCHEDULED.value

    # Both windows should have exactly one planned call
    assert await _count_planned_calls(session, user.id, wt_a, target_date) == 1
    assert await _count_planned_calls(session, user.id, wt_b, target_date) == 1


# ===================================================================
# P28b: Updating one window's times does not affect other windows
# ===================================================================


@given(
    window_pair=_distinct_window_pairs,
    target_date=_target_dates,
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=20,
)
@pytest.mark.asyncio
async def test_update_window_does_not_affect_other_windows(
    window_pair: tuple[str, str],
    target_date: date,
    session: AsyncSession,
):
    """Updating window A's times (which hard-deletes its future planned
    calls) does not touch window B's CallLog entries."""
    wt_a, wt_b = window_pair

    user = await _create_user(session)
    win_a = await _create_window(session, user.id, wt_a, time(7, 0), time(9, 0))
    win_b = await _create_window(session, user.id, wt_b, time(13, 0), time(15, 0))

    # Materialize both windows for a future date
    future_date = date.today() + timedelta(days=2)
    await _materialize_call(session, user, win_a, future_date)
    await _materialize_call(session, user, win_b, future_date)

    call_b_before = await _get_planned_call(session, user.id, wt_b, future_date)
    assert call_b_before is not None
    b_id = call_b_before.id
    b_time = call_b_before.scheduled_time

    # Update window A's times via the service (triggers hard-delete of A's calls)
    svc = CallWindowService(session)
    await svc.update_window(win_a.id, start_time=time(8, 0), end_time=time(10, 0))

    # Window A's planned call should be deleted
    assert await _count_planned_calls(session, user.id, wt_a, future_date) == 0

    # Window B's planned call should be untouched
    call_b_after = await _get_planned_call(session, user.id, wt_b, future_date)
    assert call_b_after is not None
    assert call_b_after.id == b_id
    assert call_b_after.scheduled_time == b_time
    assert call_b_after.status == CallLogStatus.SCHEDULED.value


# ===================================================================
# P28c: Deactivating one window does not affect other windows
# ===================================================================


@given(window_pair=_distinct_window_pairs)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=20,
)
@pytest.mark.asyncio
async def test_deactivate_window_does_not_affect_other_windows(
    window_pair: tuple[str, str],
    session: AsyncSession,
):
    """Deactivating (removing) window A does not touch window B's
    CallWindow record or its scheduled CallLog entries."""
    wt_a, wt_b = window_pair

    user = await _create_user(session)
    win_a = await _create_window(session, user.id, wt_a, time(7, 0), time(9, 0))
    win_b = await _create_window(session, user.id, wt_b, time(13, 0), time(15, 0))

    future_date = date.today() + timedelta(days=2)
    await _materialize_call(session, user, win_a, future_date)
    await _materialize_call(session, user, win_b, future_date)

    call_b_before = await _get_planned_call(session, user.id, wt_b, future_date)
    assert call_b_before is not None

    # Deactivate window A
    svc = CallWindowService(session)
    await svc.deactivate_window(win_a.id)

    # Window A should be inactive and its calls deleted
    await session.refresh(win_a)
    assert win_a.is_active is False
    assert await _count_planned_calls(session, user.id, wt_a, future_date) == 0

    # Window B should be completely unaffected
    await session.refresh(win_b)
    assert win_b.is_active is True
    call_b_after = await _get_planned_call(session, user.id, wt_b, future_date)
    assert call_b_after is not None
    assert call_b_after.id == call_b_before.id
    assert call_b_after.status == CallLogStatus.SCHEDULED.value


# ===================================================================
# P28d: Each window's CallLog entries are independent across all three
# ===================================================================


@given(target_date=_target_dates)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=15,
)
@pytest.mark.asyncio
async def test_all_three_windows_independent(
    target_date: date,
    session: AsyncSession,
):
    """A user with morning, afternoon, and evening windows gets exactly
    one planned CallLog per window, and each has the correct call_type
    and origin_window_id."""
    user = await _create_user(session)
    win_m = await _create_window(session, user.id, WindowType.MORNING.value, time(7, 0), time(9, 0))
    win_a = await _create_window(session, user.id, WindowType.AFTERNOON.value, time(13, 0), time(15, 0))
    win_e = await _create_window(session, user.id, WindowType.EVENING.value, time(20, 0), time(21, 30))

    for win in [win_m, win_a, win_e]:
        await _materialize_call(session, user, win, target_date)

    for wt, win in [
        (WindowType.MORNING.value, win_m),
        (WindowType.AFTERNOON.value, win_a),
        (WindowType.EVENING.value, win_e),
    ]:
        count = await _count_planned_calls(session, user.id, wt, target_date)
        assert count == 1, f"Expected 1 planned {wt} call, got {count}"

        call = await _get_planned_call(session, user.id, wt, target_date)
        assert call is not None
        assert call.call_type == wt
        assert call.origin_window_id == win.id
        assert call.occurrence_kind == OccurrenceKind.PLANNED.value


# ===================================================================
# P28e: Saving a new window via service does not affect existing windows
# ===================================================================


@given(window_pair=_distinct_window_pairs)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=15,
)
@pytest.mark.asyncio
async def test_save_new_window_does_not_affect_existing(
    window_pair: tuple[str, str],
    session: AsyncSession,
):
    """Adding a new call window via CallWindowService.save_call_window
    does not modify or delete existing windows or their scheduled calls."""
    wt_existing, wt_new = window_pair

    user = await _create_user(session)
    svc = CallWindowService(session)

    # Create the first window and materialize a call
    existing_win = await svc.save_call_window(
        user_id=user.id,
        window_type=wt_existing,
        start_time=time(7, 0),
        end_time=time(9, 0),
    )
    future_date = date.today() + timedelta(days=2)
    await _materialize_call(session, user, existing_win, future_date)

    call_before = await _get_planned_call(session, user.id, wt_existing, future_date)
    assert call_before is not None
    before_id = call_before.id

    # Now add a second window
    new_win = await svc.save_call_window(
        user_id=user.id,
        window_type=wt_new,
        start_time=time(14, 0),
        end_time=time(16, 0),
    )

    # Existing window's call should be untouched
    call_after = await _get_planned_call(session, user.id, wt_existing, future_date)
    assert call_after is not None
    assert call_after.id == before_id

    # Existing window record should be unchanged
    await session.refresh(existing_win)
    assert existing_win.is_active is True
    assert existing_win.start_time == time(7, 0)
    assert existing_win.end_time == time(9, 0)
