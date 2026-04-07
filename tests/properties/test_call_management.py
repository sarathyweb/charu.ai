"""Property tests for call management state transitions (P30).

P30 — Call management state transitions are valid: for any call management
      operation (skip, reschedule, cancel_all_calls_today, schedule_callback,
      get_next_call), the resulting CallLog state transitions are always valid.

      - skip_call transitions a scheduled call to skipped status
      - reschedule_call keeps the call in scheduled status with updated time
      - cancel_all_calls_today transitions scheduled/ringing calls to cancelled
      - Operations on terminal states return errors (except idempotent no-ops
        on matching terminal states)
      - schedule_callback creates a new on-demand CallLog with scheduled status
      - get_next_call is read-only and doesn't change state
      - State guards: in_progress calls only allow get_next_call and
        schedule_callback (deferred mode)

**Validates: Requirements 21.1, 21.2, 21.3, 21.5**
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from hypothesis import HealthCheck, assume, given, settings, strategies as st
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.call_log import CallLog
from app.models.call_window import CallWindow
from app.models.enums import (
    CallLogStatus,
    CallType,
    OccurrenceKind,
)
from app.models.user import User
from app.services.call_log_service import CallLogService, TERMINAL_STATUSES
from app.services.call_management_service import CallManagementService

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_window_call_types = st.sampled_from([
    CallType.MORNING.value,
    CallType.AFTERNOON.value,
    CallType.EVENING.value,
])

_all_call_types = st.sampled_from([ct.value for ct in CallType])

_terminal_statuses = st.sampled_from(list(TERMINAL_STATUSES))

_non_terminal_non_inprogress = st.sampled_from([
    CallLogStatus.SCHEDULED,
    CallLogStatus.RINGING,
])

_valid_minutes = st.integers(min_value=1, max_value=120)
_invalid_minutes = st.one_of(
    st.integers(min_value=-100, max_value=0),
    st.integers(min_value=121, max_value=500),
)

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
        phone=f"+1555900{_phone_counter:04d}",
        timezone=tz,
        onboarding_complete=False,
        consecutive_active_days=0,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


def _make_call_log(
    user_id: int,
    call_type: str = CallType.MORNING.value,
    status: str = CallLogStatus.SCHEDULED.value,
    hours_ahead: float = 1.0,
    tz: str = "America/New_York",
    occurrence_kind: str = OccurrenceKind.PLANNED.value,
) -> CallLog:
    """Build a CallLog instance (not yet persisted).

    ``call_date`` is always set to *today* in the given timezone so that
    ``find_today`` will match the row regardless of wall-clock proximity to
    midnight.  ``scheduled_time`` is placed ``hours_ahead`` from now but
    clamped to the same local date.
    """
    now_utc = datetime.now(timezone.utc)
    local_now = now_utc.astimezone(ZoneInfo(tz))
    local_today = local_now.date()
    scheduled = now_utc + timedelta(hours=hours_ahead)
    # Clamp: if hours_ahead pushes into next local day, pin to 23:59 today
    if scheduled.astimezone(ZoneInfo(tz)).date() != local_today:
        scheduled = datetime.combine(
            local_today, time(23, 59), tzinfo=ZoneInfo(tz)
        ).astimezone(timezone.utc)
    return CallLog(
        user_id=user_id,
        call_type=call_type,
        call_date=local_today,
        scheduled_time=scheduled,
        scheduled_timezone=tz,
        status=status,
        occurrence_kind=occurrence_kind,
        attempt_number=1,
    )


async def _create_call_at_status(
    cls: CallLogService,
    user_id: int,
    target_status: CallLogStatus,
    call_type: str = CallType.MORNING.value,
    hours_ahead: float = 1.0,
    occurrence_kind: str = OccurrenceKind.PLANNED.value,
) -> CallLog:
    """Create a CallLog and walk it through the state machine to target_status."""
    cl = await cls.create_call_log(
        _make_call_log(
            user_id,
            call_type=call_type,
            hours_ahead=hours_ahead,
            occurrence_kind=occurrence_kind,
        )
    )

    if target_status == CallLogStatus.SCHEDULED:
        return cl

    # Paths to reach various states
    if target_status == CallLogStatus.DISPATCHING:
        await cls.update_status(cl.id, CallLogStatus.DISPATCHING, cl.version)
    elif target_status == CallLogStatus.RINGING:
        await cls.update_status(cl.id, CallLogStatus.DISPATCHING, 1)
        await cls.update_status(cl.id, CallLogStatus.RINGING, 2)
    elif target_status == CallLogStatus.IN_PROGRESS:
        await cls.update_status(cl.id, CallLogStatus.DISPATCHING, 1)
        await cls.update_status(cl.id, CallLogStatus.RINGING, 2)
        await cls.update_status(cl.id, CallLogStatus.IN_PROGRESS, 3)
    elif target_status == CallLogStatus.COMPLETED:
        await cls.update_status(cl.id, CallLogStatus.DISPATCHING, 1)
        await cls.update_status(cl.id, CallLogStatus.RINGING, 2)
        await cls.update_status(cl.id, CallLogStatus.IN_PROGRESS, 3)
        await cls.update_status(cl.id, CallLogStatus.COMPLETED, 4)
    elif target_status == CallLogStatus.MISSED:
        await cls.update_status(cl.id, CallLogStatus.DISPATCHING, 1)
        await cls.update_status(cl.id, CallLogStatus.MISSED, 2)
    elif target_status == CallLogStatus.CANCELLED:
        await cls.update_status(cl.id, CallLogStatus.CANCELLED, 1)
    elif target_status == CallLogStatus.SKIPPED:
        await cls.update_status(cl.id, CallLogStatus.SKIPPED, 1)
    elif target_status == CallLogStatus.DEFERRED:
        await cls.update_status(cl.id, CallLogStatus.DISPATCHING, 1)
        await cls.update_status(cl.id, CallLogStatus.RINGING, 2)
        await cls.update_status(cl.id, CallLogStatus.IN_PROGRESS, 3)
        await cls.update_status(cl.id, CallLogStatus.DEFERRED, 4)

    refreshed = await cls.session.get(CallLog, cl.id)
    await cls.session.refresh(refreshed)
    return refreshed


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def svc(session: AsyncSession) -> CallManagementService:
    return CallManagementService(session, twilio_client=None)


@pytest_asyncio.fixture
async def cls(session: AsyncSession) -> CallLogService:
    return CallLogService(session)



# ---------------------------------------------------------------------------
# P30a: skip_call transitions scheduled → skipped
# ---------------------------------------------------------------------------


@given(call_type=_window_call_types)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=20)
@pytest.mark.asyncio
async def test_skip_transitions_scheduled_to_skipped(
    call_type: str,
    session: AsyncSession,
):
    """skip_call on a scheduled call always produces status=skipped."""
    cls = CallLogService(session)
    svc = CallManagementService(session, twilio_client=None)
    user = await _create_user(session)
    cl = await cls.create_call_log(
        _make_call_log(user.id, call_type=call_type)
    )

    result = await svc.skip_call(user.id, call_type)
    assert result.success is True

    updated = await session.get(CallLog, cl.id)
    await session.refresh(updated)
    assert updated.status == CallLogStatus.SKIPPED.value


# ---------------------------------------------------------------------------
# P30b: skip_call is idempotent on already-skipped calls
# ---------------------------------------------------------------------------


@given(call_type=_window_call_types)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=20)
@pytest.mark.asyncio
async def test_skip_idempotent_on_already_skipped(
    call_type: str,
    session: AsyncSession,
):
    """Skipping an already-skipped call returns success (idempotent no-op)."""
    cls = CallLogService(session)
    svc = CallManagementService(session, twilio_client=None)
    user = await _create_user(session)
    await _create_call_at_status(cls, user.id, CallLogStatus.SKIPPED, call_type=call_type)

    result = await svc.skip_call(user.id, call_type)
    assert result.success is True
    assert "already skipped" in result.message.lower()


# ---------------------------------------------------------------------------
# P30c: Operations on terminal states (except matching idempotent) return errors
# ---------------------------------------------------------------------------


@given(terminal=_terminal_statuses, call_type=_window_call_types)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=50)
@pytest.mark.asyncio
async def test_skip_on_non_matching_terminal_fails(
    terminal: CallLogStatus,
    call_type: str,
    session: AsyncSession,
):
    """skip_call on a terminal state that isn't 'skipped' returns failure
    (no scheduled/ringing calls found)."""
    assume(terminal != CallLogStatus.SKIPPED)
    cls = CallLogService(session)
    svc = CallManagementService(session, twilio_client=None)
    user = await _create_user(session)
    await _create_call_at_status(cls, user.id, terminal, call_type=call_type)

    result = await svc.skip_call(user.id, call_type)
    # Should fail because there are no scheduled/ringing calls
    assert result.success is False


# ---------------------------------------------------------------------------
# P30d: cancel_all_calls_today transitions scheduled/ringing → cancelled
# ---------------------------------------------------------------------------


@given(
    num_calls=st.integers(min_value=1, max_value=3),
)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=20)
@pytest.mark.asyncio
async def test_cancel_all_transitions_to_cancelled(
    num_calls: int,
    session: AsyncSession,
):
    """cancel_all_calls_today transitions all scheduled calls to cancelled."""
    cls = CallLogService(session)
    svc = CallManagementService(session, twilio_client=None)
    user = await _create_user(session)

    call_types = [CallType.MORNING.value, CallType.AFTERNOON.value, CallType.EVENING.value]
    created_ids = []
    for i in range(num_calls):
        ct = call_types[i % len(call_types)]
        # Use a small hours_ahead offset (minutes apart) so all calls land on
        # the same local date regardless of when the test runs.
        cl = await cls.create_call_log(
            _make_call_log(user.id, call_type=ct, hours_ahead=0.1 + i * 0.01)
        )
        created_ids.append(cl.id)

    result = await svc.cancel_all_calls_today(user.id)
    assert result.success is True
    assert result.cancelled_count == num_calls

    for cid in created_ids:
        log = await session.get(CallLog, cid)
        await session.refresh(log)
        assert log.status == CallLogStatus.CANCELLED.value


# ---------------------------------------------------------------------------
# P30e: cancel_all_calls_today with zero calls returns success + count=0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_all_zero_calls(session: AsyncSession):
    """cancel_all_calls_today with no scheduled calls returns success with count=0."""
    svc = CallManagementService(session, twilio_client=None)
    user = await _create_user(session)

    result = await svc.cancel_all_calls_today(user.id)
    assert result.success is True
    assert result.cancelled_count == 0


# ---------------------------------------------------------------------------
# P30f: schedule_callback creates on-demand CallLog with scheduled status
# ---------------------------------------------------------------------------


@given(minutes=_valid_minutes)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=30)
@pytest.mark.asyncio
async def test_schedule_callback_creates_scheduled_on_demand(
    minutes: int,
    session: AsyncSession,
):
    """schedule_callback creates a new on-demand CallLog with status=scheduled."""
    svc = CallManagementService(session, twilio_client=None)
    user = await _create_user(session)

    result = await svc.schedule_callback(user.id, minutes)
    assert result.success is True
    assert result.call_log_id is not None

    log = await session.get(CallLog, result.call_log_id)
    await session.refresh(log)
    assert log.status == CallLogStatus.SCHEDULED.value
    assert log.call_type == CallType.ON_DEMAND.value
    assert log.occurrence_kind == OccurrenceKind.ON_DEMAND.value


# ---------------------------------------------------------------------------
# P30g: schedule_callback rejects invalid minutes
# ---------------------------------------------------------------------------


@given(minutes=_invalid_minutes)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=30)
@pytest.mark.asyncio
async def test_schedule_callback_rejects_invalid_minutes(
    minutes: int,
    session: AsyncSession,
):
    """schedule_callback with minutes outside [1, 120] returns failure."""
    svc = CallManagementService(session, twilio_client=None)
    user = await _create_user(session)

    result = await svc.schedule_callback(user.id, minutes)
    assert result.success is False


# ---------------------------------------------------------------------------
# P30h: schedule_callback replaces existing pending on-demand call
# ---------------------------------------------------------------------------


@given(
    first_min=st.integers(min_value=10, max_value=60),
    second_min=st.integers(min_value=10, max_value=60),
)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=20)
@pytest.mark.asyncio
async def test_schedule_callback_replaces_existing(
    first_min: int,
    second_min: int,
    session: AsyncSession,
):
    """A new schedule_callback cancels the previous pending on-demand call."""
    svc = CallManagementService(session, twilio_client=None)
    user = await _create_user(session)

    r1 = await svc.schedule_callback(user.id, first_min)
    assert r1.success is True
    first_id = r1.call_log_id

    r2 = await svc.schedule_callback(user.id, second_min)
    assert r2.success is True
    assert r2.call_log_id != first_id

    # First should be cancelled
    first_log = await session.get(CallLog, first_id)
    await session.refresh(first_log)
    assert first_log.status == CallLogStatus.CANCELLED.value

    # Second should be scheduled
    second_log = await session.get(CallLog, r2.call_log_id)
    await session.refresh(second_log)
    assert second_log.status == CallLogStatus.SCHEDULED.value


# ---------------------------------------------------------------------------
# P30i: get_next_call is read-only — doesn't change any state
# ---------------------------------------------------------------------------


@given(call_type=_window_call_types)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=20)
@pytest.mark.asyncio
async def test_get_next_call_is_read_only(
    call_type: str,
    session: AsyncSession,
):
    """get_next_call never modifies the CallLog status or version."""
    cls = CallLogService(session)
    svc = CallManagementService(session, twilio_client=None)
    user = await _create_user(session)
    cl = await cls.create_call_log(
        _make_call_log(user.id, call_type=call_type)
    )
    original_status = cl.status
    original_version = cl.version

    result = await svc.get_next_call(user.id)
    assert result.success is True

    refreshed = await session.get(CallLog, cl.id)
    await session.refresh(refreshed)
    assert refreshed.status == original_status
    assert refreshed.version == original_version


# ---------------------------------------------------------------------------
# P30j: schedule_callback deferred mode transitions in_progress → deferred
# ---------------------------------------------------------------------------


@given(minutes=_valid_minutes)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=20)
@pytest.mark.asyncio
async def test_schedule_callback_deferred_transitions_in_progress(
    minutes: int,
    session: AsyncSession,
):
    """schedule_callback with current_call_log_id transitions the in-progress
    call to deferred and creates a new on-demand call."""
    cls = CallLogService(session)
    svc = CallManagementService(session, twilio_client=None)
    user = await _create_user(session)

    # Create an in-progress call
    cl = await _create_call_at_status(
        cls, user.id, CallLogStatus.IN_PROGRESS,
    )

    result = await svc.schedule_callback(
        user.id, minutes, current_call_log_id=cl.id,
    )
    assert result.success is True

    # Original call should be deferred
    original = await session.get(CallLog, cl.id)
    await session.refresh(original)
    assert original.status == CallLogStatus.DEFERRED.value

    # New call should reference the deferred one
    new_log = await session.get(CallLog, result.call_log_id)
    await session.refresh(new_log)
    assert new_log.replaced_call_log_id == cl.id
    assert new_log.status == CallLogStatus.SCHEDULED.value
    assert new_log.call_type == CallType.ON_DEMAND.value


# ---------------------------------------------------------------------------
# P30k: schedule_callback deferred mode rejects non-in-progress calls
# ---------------------------------------------------------------------------


@given(
    status=st.sampled_from([
        CallLogStatus.SCHEDULED,
        CallLogStatus.COMPLETED,
        CallLogStatus.MISSED,
        CallLogStatus.CANCELLED,
        CallLogStatus.SKIPPED,
    ]),
)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=20)
@pytest.mark.asyncio
async def test_schedule_callback_deferred_rejects_non_in_progress(
    status: CallLogStatus,
    session: AsyncSession,
):
    """schedule_callback with current_call_log_id fails if the call is not in_progress."""
    cls = CallLogService(session)
    svc = CallManagementService(session, twilio_client=None)
    user = await _create_user(session)

    cl = await _create_call_at_status(cls, user.id, status)

    result = await svc.schedule_callback(
        user.id, 10, current_call_log_id=cl.id,
    )
    assert result.success is False


# ---------------------------------------------------------------------------
# P30l: in_progress calls block skip and cancel operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_progress_blocks_skip(session: AsyncSession):
    """Cannot skip an in-progress call — find_today with SCHEDULED/RINGING
    won't find it, so skip returns failure."""
    cls = CallLogService(session)
    svc = CallManagementService(session, twilio_client=None)
    user = await _create_user(session)

    await _create_call_at_status(
        cls, user.id, CallLogStatus.IN_PROGRESS,
        call_type=CallType.MORNING.value,
    )

    result = await svc.skip_call(user.id, CallType.MORNING.value)
    assert result.success is False


@pytest.mark.asyncio
async def test_in_progress_blocks_cancel_all(session: AsyncSession):
    """cancel_all_calls_today does not cancel in-progress calls."""
    cls = CallLogService(session)
    svc = CallManagementService(session, twilio_client=None)
    user = await _create_user(session)

    await _create_call_at_status(
        cls, user.id, CallLogStatus.IN_PROGRESS,
        call_type=CallType.MORNING.value,
    )

    result = await svc.cancel_all_calls_today(user.id)
    assert result.cancelled_count == 0
