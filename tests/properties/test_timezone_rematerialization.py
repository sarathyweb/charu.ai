"""Property tests for timezone change and call window edit rematerialization (P40, P44).

P40 — Call window edits hard-delete+regenerate future schedule:
  For *any* call window update, all future ``scheduled`` planned CallLog
  entries for that window type are hard-deleted (not status-changed) to
  free the partial unique index slot.  Already-completed, in-progress,
  or user-cancelled/skipped entries are NOT affected.

P44 — Timezone change hard-deletes+rematerializes all future planned calls:
  For *any* timezone change on a user, all future ``scheduled`` planned
  CallLog entries across ALL window types are hard-deleted.  Terminal and
  user-cancelled/skipped entries are preserved.

Validates: Design §3 Call Scheduling section, Concurrency Notes
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone as tz

import pytest
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
from app.services.call_window_service import CallWindowService

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_window_types = st.sampled_from([
    WindowType.MORNING.value,
    WindowType.AFTERNOON.value,
    WindowType.EVENING.value,
])

_non_scheduled_statuses = st.sampled_from([
    CallLogStatus.COMPLETED.value,
    CallLogStatus.MISSED.value,
    CallLogStatus.CANCELLED.value,
    CallLogStatus.SKIPPED.value,
    CallLogStatus.DEFERRED.value,
    CallLogStatus.IN_PROGRESS.value,
])

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


async def _create_call_log(
    session: AsyncSession,
    user_id: int,
    call_type: str,
    call_date: date,
    scheduled_time: datetime,
    status: str = CallLogStatus.SCHEDULED.value,
    occurrence_kind: str = OccurrenceKind.PLANNED.value,
    origin_window_id: int | None = None,
) -> CallLog:
    log = CallLog(
        user_id=user_id,
        call_type=call_type,
        call_date=call_date,
        scheduled_time=scheduled_time,
        scheduled_timezone="America/New_York",
        status=status,
        occurrence_kind=occurrence_kind,
        attempt_number=1,
        origin_window_id=origin_window_id,
    )
    session.add(log)
    await session.flush()
    await session.refresh(log)
    return log


async def _count_call_logs(
    session: AsyncSession,
    user_id: int,
    call_type: str | None = None,
    call_date: date | None = None,
    status: str | None = None,
    occurrence_kind: str | None = None,
) -> int:
    """Count CallLog entries matching the given filters."""
    stmt = select(CallLog).where(CallLog.user_id == user_id)
    if call_type is not None:
        stmt = stmt.where(CallLog.call_type == call_type)
    if call_date is not None:
        stmt = stmt.where(CallLog.call_date == call_date)
    if status is not None:
        stmt = stmt.where(CallLog.status == status)
    if occurrence_kind is not None:
        stmt = stmt.where(CallLog.occurrence_kind == occurrence_kind)
    result = await session.exec(stmt)
    return len(list(result.all()))


# A future UTC time used for all future scheduled entries
_FUTURE_BASE = datetime(2026, 7, 15, 12, 0, tzinfo=tz.utc)
_FUTURE_DATE = date(2026, 7, 15)


# ===================================================================
# Property 40: Call window edits hard-delete future scheduled planned
# ===================================================================


@given(window_type=_window_types)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=15,
)
@pytest.mark.asyncio
async def test_window_edit_deletes_future_scheduled_planned(
    window_type: str,
    session: AsyncSession,
):
    """Updating a call window hard-deletes future scheduled planned
    CallLog entries for that window type."""
    user = await _create_user(session)
    window = await _create_window(session, user.id, window_type)

    # Create a future scheduled planned entry
    await _create_call_log(
        session, user.id, window_type, _FUTURE_DATE, _FUTURE_BASE,
        origin_window_id=window.id,
    )

    before = await _count_call_logs(
        session, user.id, call_type=window_type,
        status=CallLogStatus.SCHEDULED.value,
        occurrence_kind=OccurrenceKind.PLANNED.value,
    )
    assert before == 1

    # Update the window times (triggers hard-delete)
    svc = CallWindowService(session)
    await svc.save_call_window(
        user_id=user.id,
        window_type=window_type,
        start_time=time(8, 0),
        end_time=time(10, 0),
    )

    original_date_count = await _count_call_logs(
        session, user.id, call_type=window_type, call_date=_FUTURE_DATE,
        status=CallLogStatus.SCHEDULED.value,
        occurrence_kind=OccurrenceKind.PLANNED.value,
    )
    assert original_date_count == 0

    after = await _count_call_logs(
        session, user.id, call_type=window_type,
        status=CallLogStatus.SCHEDULED.value,
        occurrence_kind=OccurrenceKind.PLANNED.value,
    )
    assert after >= 1, (
        "Expected replacement scheduled planned entries after rematerialization, "
        f"got {after}"
    )


@given(
    window_type=_window_types,
    non_sched_status=_non_scheduled_statuses,
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=30,
)
@pytest.mark.asyncio
async def test_window_edit_preserves_non_scheduled_entries(
    window_type: str,
    non_sched_status: str,
    session: AsyncSession,
):
    """Window edits do NOT delete entries with non-scheduled status
    (completed, missed, cancelled, skipped, deferred, in_progress)."""
    user = await _create_user(session)
    window = await _create_window(session, user.id, window_type)

    # Create a non-scheduled entry (e.g. completed, cancelled, skipped)
    await _create_call_log(
        session, user.id, window_type, _FUTURE_DATE, _FUTURE_BASE,
        status=non_sched_status,
        occurrence_kind=OccurrenceKind.PLANNED.value,
        origin_window_id=window.id,
    )

    # Update the window
    svc = CallWindowService(session)
    await svc.save_call_window(
        user_id=user.id,
        window_type=window_type,
        start_time=time(8, 0),
        end_time=time(10, 0),
    )

    # The non-scheduled entry should still exist
    remaining = await _count_call_logs(
        session, user.id, call_type=window_type,
        status=non_sched_status,
        occurrence_kind=OccurrenceKind.PLANNED.value,
    )
    assert remaining == 1, (
        f"Expected non-scheduled ({non_sched_status}) entry to be preserved, "
        f"but count is {remaining}"
    )


@pytest.mark.asyncio
async def test_window_edit_only_deletes_matching_window_type(
    session: AsyncSession,
):
    """Editing a morning window does NOT delete scheduled planned entries
    for afternoon or evening windows."""
    user = await _create_user(session)
    w_morning = await _create_window(session, user.id, WindowType.MORNING.value)
    w_afternoon = await _create_window(
        session, user.id, WindowType.AFTERNOON.value, time(13, 0), time(15, 0),
    )

    # Create future scheduled planned entries for both window types
    await _create_call_log(
        session, user.id, WindowType.MORNING.value, _FUTURE_DATE, _FUTURE_BASE,
        origin_window_id=w_morning.id,
    )
    await _create_call_log(
        session, user.id, WindowType.AFTERNOON.value, _FUTURE_DATE,
        _FUTURE_BASE + timedelta(hours=4),
        origin_window_id=w_afternoon.id,
    )

    # Edit morning window only
    svc = CallWindowService(session)
    await svc.save_call_window(
        user_id=user.id,
        window_type=WindowType.MORNING.value,
        start_time=time(8, 0),
        end_time=time(10, 0),
    )

    # Original morning entry should be deleted and replacements materialized.
    original_morning_date_count = await _count_call_logs(
        session, user.id, call_type=WindowType.MORNING.value,
        call_date=_FUTURE_DATE,
        status=CallLogStatus.SCHEDULED.value,
        occurrence_kind=OccurrenceKind.PLANNED.value,
    )
    assert original_morning_date_count == 0

    morning_count = await _count_call_logs(
        session, user.id, call_type=WindowType.MORNING.value,
        status=CallLogStatus.SCHEDULED.value,
        occurrence_kind=OccurrenceKind.PLANNED.value,
    )
    assert morning_count >= 1

    # Afternoon entries should be preserved
    afternoon_count = await _count_call_logs(
        session, user.id, call_type=WindowType.AFTERNOON.value,
        status=CallLogStatus.SCHEDULED.value,
        occurrence_kind=OccurrenceKind.PLANNED.value,
    )
    assert afternoon_count == 1, (
        f"Afternoon entry should be preserved, got {afternoon_count}"
    )


@pytest.mark.asyncio
async def test_window_edit_preserves_non_planned_occurrences(
    session: AsyncSession,
):
    """Window edits only delete planned entries — retries and on-demand
    entries are preserved."""
    user = await _create_user(session)
    window = await _create_window(session, user.id, WindowType.MORNING.value)

    # Create a retry entry (not planned)
    await _create_call_log(
        session, user.id, WindowType.MORNING.value, _FUTURE_DATE, _FUTURE_BASE,
        occurrence_kind=OccurrenceKind.RETRY.value,
        origin_window_id=window.id,
    )

    # Edit the window
    svc = CallWindowService(session)
    await svc.save_call_window(
        user_id=user.id,
        window_type=WindowType.MORNING.value,
        start_time=time(8, 0),
        end_time=time(10, 0),
    )

    # Retry entry should still exist
    retry_count = await _count_call_logs(
        session, user.id, call_type=WindowType.MORNING.value,
        occurrence_kind=OccurrenceKind.RETRY.value,
    )
    assert retry_count == 1, "Retry entry should be preserved after window edit"


@pytest.mark.asyncio
async def test_window_edit_frees_unique_index_slot(
    session: AsyncSession,
):
    """After hard-delete, the partial unique index slot is freed so a new
    planned entry can be inserted for the same user/type/date."""
    user = await _create_user(session)
    window = await _create_window(session, user.id, WindowType.MORNING.value)

    # Create and then hard-delete via window edit
    await _create_call_log(
        session, user.id, WindowType.MORNING.value, _FUTURE_DATE, _FUTURE_BASE,
        origin_window_id=window.id,
    )

    svc = CallWindowService(session)
    await svc.save_call_window(
        user_id=user.id,
        window_type=WindowType.MORNING.value,
        start_time=time(8, 0),
        end_time=time(10, 0),
    )

    # Now we should be able to insert a new planned entry for the same date
    # (the unique index slot was freed by hard-delete)
    new_log = CallLog(
        user_id=user.id,
        call_type=WindowType.MORNING.value,
        call_date=_FUTURE_DATE,
        scheduled_time=_FUTURE_BASE + timedelta(hours=1),
        scheduled_timezone=user.timezone,
        status=CallLogStatus.SCHEDULED.value,
        occurrence_kind=OccurrenceKind.PLANNED.value,
        attempt_number=1,
        origin_window_id=window.id,
    )
    session.add(new_log)
    await session.flush()  # Should NOT raise IntegrityError

    count_for_original_date = await _count_call_logs(
        session, user.id, call_type=WindowType.MORNING.value,
        call_date=_FUTURE_DATE,
        status=CallLogStatus.SCHEDULED.value,
        occurrence_kind=OccurrenceKind.PLANNED.value,
    )
    assert count_for_original_date == 1, (
        "New planned entry should be insertable after hard-delete"
    )


# ===================================================================
# Property 44: Timezone change hard-deletes all future planned calls
# ===================================================================


@pytest.mark.asyncio
async def test_timezone_change_deletes_all_window_types(
    session: AsyncSession,
):
    """Changing a user's timezone hard-deletes future scheduled planned
    CallLog entries across ALL window types."""
    user = await _create_user(session, tz_name="America/New_York")
    w_m = await _create_window(session, user.id, WindowType.MORNING.value)
    w_a = await _create_window(
        session, user.id, WindowType.AFTERNOON.value, time(13, 0), time(15, 0),
    )
    w_e = await _create_window(
        session, user.id, WindowType.EVENING.value, time(20, 0), time(21, 30),
    )

    # Create future scheduled planned entries for all three window types
    for wt, w, offset_h in [
        (WindowType.MORNING.value, w_m, 0),
        (WindowType.AFTERNOON.value, w_a, 4),
        (WindowType.EVENING.value, w_e, 8),
    ]:
        await _create_call_log(
            session, user.id, wt, _FUTURE_DATE,
            _FUTURE_BASE + timedelta(hours=offset_h),
            origin_window_id=w.id,
        )

    # Verify all three exist
    total_before = await _count_call_logs(
        session, user.id,
        status=CallLogStatus.SCHEDULED.value,
        occurrence_kind=OccurrenceKind.PLANNED.value,
    )
    assert total_before == 3

    # Simulate timezone change: hard-delete all future planned across all types
    cw_svc = CallWindowService(session)
    for wt in WindowType:
        await cw_svc._hard_delete_future_planned(user.id, wt.value)

    total_after = await _count_call_logs(
        session, user.id,
        status=CallLogStatus.SCHEDULED.value,
        occurrence_kind=OccurrenceKind.PLANNED.value,
    )
    assert total_after == 0, (
        f"Expected 0 scheduled planned entries after timezone change, got {total_after}"
    )


@given(non_sched_status=_non_scheduled_statuses)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=15,
)
@pytest.mark.asyncio
async def test_timezone_change_preserves_non_scheduled_entries(
    non_sched_status: str,
    session: AsyncSession,
):
    """Timezone change does NOT delete entries with non-scheduled status."""
    user = await _create_user(session, tz_name="America/New_York")
    window = await _create_window(session, user.id, WindowType.MORNING.value)

    # Create a non-scheduled planned entry
    await _create_call_log(
        session, user.id, WindowType.MORNING.value, _FUTURE_DATE, _FUTURE_BASE,
        status=non_sched_status,
        occurrence_kind=OccurrenceKind.PLANNED.value,
        origin_window_id=window.id,
    )

    # Simulate timezone change
    cw_svc = CallWindowService(session)
    for wt in WindowType:
        await cw_svc._hard_delete_future_planned(user.id, wt.value)

    remaining = await _count_call_logs(
        session, user.id,
        status=non_sched_status,
        occurrence_kind=OccurrenceKind.PLANNED.value,
    )
    assert remaining == 1, (
        f"Non-scheduled ({non_sched_status}) entry should survive timezone change"
    )


@pytest.mark.asyncio
async def test_timezone_change_preserves_non_planned_occurrences(
    session: AsyncSession,
):
    """Timezone change only deletes planned entries — retries, on-demand,
    and rescheduled entries are preserved."""
    user = await _create_user(session, tz_name="America/New_York")
    window = await _create_window(session, user.id, WindowType.MORNING.value)

    # Create entries with non-planned occurrence kinds
    for kind in [OccurrenceKind.RETRY.value, OccurrenceKind.ON_DEMAND.value, OccurrenceKind.RESCHEDULED.value]:
        call_type = WindowType.MORNING.value if kind != OccurrenceKind.ON_DEMAND.value else CallType.ON_DEMAND.value
        await _create_call_log(
            session, user.id, call_type,
            _FUTURE_DATE, _FUTURE_BASE,
            occurrence_kind=kind,
            origin_window_id=window.id if kind != OccurrenceKind.ON_DEMAND.value else None,
        )

    # Simulate timezone change
    cw_svc = CallWindowService(session)
    for wt in WindowType:
        await cw_svc._hard_delete_future_planned(user.id, wt.value)

    # All non-planned entries should survive
    total = await _count_call_logs(session, user.id)
    assert total == 3, (
        f"Expected 3 non-planned entries to survive timezone change, got {total}"
    )


@pytest.mark.asyncio
async def test_timezone_change_frees_all_unique_index_slots(
    session: AsyncSession,
):
    """After timezone change hard-delete, all partial unique index slots
    are freed so new planned entries can be inserted for every window type."""
    user = await _create_user(session, tz_name="America/New_York")
    w_m = await _create_window(session, user.id, WindowType.MORNING.value)
    w_a = await _create_window(
        session, user.id, WindowType.AFTERNOON.value, time(13, 0), time(15, 0),
    )

    # Create entries for both types
    for wt, w, offset_h in [
        (WindowType.MORNING.value, w_m, 0),
        (WindowType.AFTERNOON.value, w_a, 4),
    ]:
        await _create_call_log(
            session, user.id, wt, _FUTURE_DATE,
            _FUTURE_BASE + timedelta(hours=offset_h),
            origin_window_id=w.id,
        )

    # Simulate timezone change
    cw_svc = CallWindowService(session)
    for wt in WindowType:
        await cw_svc._hard_delete_future_planned(user.id, wt.value)

    # Re-insert planned entries for both types — should succeed
    for wt, w, offset_h in [
        (WindowType.MORNING.value, w_m, 1),
        (WindowType.AFTERNOON.value, w_a, 5),
    ]:
        new_log = CallLog(
            user_id=user.id,
            call_type=wt,
            call_date=_FUTURE_DATE,
            scheduled_time=_FUTURE_BASE + timedelta(hours=offset_h),
            scheduled_timezone="Asia/Kolkata",  # new timezone
            status=CallLogStatus.SCHEDULED.value,
            occurrence_kind=OccurrenceKind.PLANNED.value,
            attempt_number=1,
            origin_window_id=w.id,
        )
        session.add(new_log)

    await session.flush()  # Should NOT raise IntegrityError

    total = await _count_call_logs(
        session, user.id,
        status=CallLogStatus.SCHEDULED.value,
        occurrence_kind=OccurrenceKind.PLANNED.value,
    )
    assert total == 2, "Both re-inserted planned entries should exist"


@pytest.mark.asyncio
async def test_timezone_change_mixed_entries(
    session: AsyncSession,
):
    """Timezone change with a mix of scheduled-planned, cancelled-planned,
    and retry entries: only scheduled-planned are deleted."""
    user = await _create_user(session, tz_name="America/New_York")
    window = await _create_window(session, user.id, WindowType.MORNING.value)

    # 1. Future scheduled planned (should be deleted)
    await _create_call_log(
        session, user.id, WindowType.MORNING.value, _FUTURE_DATE, _FUTURE_BASE,
        status=CallLogStatus.SCHEDULED.value,
        occurrence_kind=OccurrenceKind.PLANNED.value,
        origin_window_id=window.id,
    )

    # 2. Future cancelled planned (should be preserved — user intent)
    await _create_call_log(
        session, user.id, WindowType.MORNING.value,
        _FUTURE_DATE + timedelta(days=1),
        _FUTURE_BASE + timedelta(days=1),
        status=CallLogStatus.CANCELLED.value,
        occurrence_kind=OccurrenceKind.PLANNED.value,
        origin_window_id=window.id,
    )

    # 3. Future skipped planned (should be preserved — user intent)
    await _create_call_log(
        session, user.id, WindowType.MORNING.value,
        _FUTURE_DATE + timedelta(days=2),
        _FUTURE_BASE + timedelta(days=2),
        status=CallLogStatus.SKIPPED.value,
        occurrence_kind=OccurrenceKind.PLANNED.value,
        origin_window_id=window.id,
    )

    # 4. Future scheduled retry (should be preserved — not planned)
    await _create_call_log(
        session, user.id, WindowType.MORNING.value,
        _FUTURE_DATE + timedelta(days=3),
        _FUTURE_BASE + timedelta(days=3),
        status=CallLogStatus.SCHEDULED.value,
        occurrence_kind=OccurrenceKind.RETRY.value,
        origin_window_id=window.id,
    )

    # Simulate timezone change
    cw_svc = CallWindowService(session)
    for wt in WindowType:
        await cw_svc._hard_delete_future_planned(user.id, wt.value)

    # Only entry #1 should be deleted
    total = await _count_call_logs(session, user.id)
    assert total == 3, f"Expected 3 surviving entries, got {total}"

    # Verify each survivor
    scheduled_planned = await _count_call_logs(
        session, user.id,
        status=CallLogStatus.SCHEDULED.value,
        occurrence_kind=OccurrenceKind.PLANNED.value,
    )
    assert scheduled_planned == 0, "Scheduled planned entry should be deleted"

    cancelled = await _count_call_logs(
        session, user.id, status=CallLogStatus.CANCELLED.value,
    )
    assert cancelled == 1, "Cancelled entry should be preserved"

    skipped = await _count_call_logs(
        session, user.id, status=CallLogStatus.SKIPPED.value,
    )
    assert skipped == 1, "Skipped entry should be preserved"

    retry = await _count_call_logs(
        session, user.id, occurrence_kind=OccurrenceKind.RETRY.value,
    )
    assert retry == 1, "Retry entry should be preserved"
