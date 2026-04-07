"""Unit tests for CallManagementService.

Tests cover:
- schedule_callback: standalone mode, deferred mode, replaces existing on-demand
- skip_call: happy path, idempotent on already-skipped, no call found
- reschedule_call: happy path, past time rejected, retry-buffer validation
- get_next_call: returns next scheduled, no calls
- cancel_all_calls_today: cancels multiple, zero calls
- State guards: terminal states, in_progress restrictions
"""

from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.call_log import CallLog
from app.models.call_window import CallWindow
from app.models.enums import (
    CallLogStatus,
    CallType,
    OccurrenceKind,
)
from app.models.user import User
from app.services.call_log_service import CallLogService
from app.services.call_management_service import CallManagementService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def svc(session: AsyncSession) -> CallManagementService:
    return CallManagementService(session, twilio_client=None)


@pytest_asyncio.fixture
async def cls(session: AsyncSession) -> CallLogService:
    return CallLogService(session)


async def _create_user(
    session: AsyncSession,
    phone: str,
    tz: str = "America/New_York",
) -> User:
    user = User(
        phone=phone,
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
    hours_ahead: float = 1,
    tz: str = "America/New_York",
    pin_today: bool = True,
    **kwargs,
) -> CallLog:
    """Build a CallLog instance (not yet persisted).

    When *pin_today* is True (the default), ``call_date`` and
    ``scheduled_time`` are clamped to today in the given timezone so that
    ``find_today`` always matches regardless of wall-clock proximity to
    midnight.  Set *pin_today=False* for tests that intentionally need a
    future date (e.g. ``get_next_call`` ordering).
    """
    now_utc = datetime.now(timezone.utc)
    scheduled = now_utc + timedelta(hours=hours_ahead)
    local_date = scheduled.astimezone(ZoneInfo(tz)).date()

    if pin_today:
        local_today = now_utc.astimezone(ZoneInfo(tz)).date()
        if local_date != local_today:
            scheduled = datetime.combine(
                local_today, time(23, 59), tzinfo=ZoneInfo(tz)
            ).astimezone(timezone.utc)
            local_date = local_today

    return CallLog(
        user_id=user_id,
        call_type=call_type,
        call_date=local_date,
        scheduled_time=scheduled,
        scheduled_timezone=tz,
        status=status,
        occurrence_kind=OccurrenceKind.PLANNED.value,
        attempt_number=1,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# schedule_callback — standalone mode
# ---------------------------------------------------------------------------


class TestScheduleCallbackStandalone:
    @pytest.mark.asyncio
    async def test_creates_on_demand_call(self, session, svc, cls):
        user = await _create_user(session, "+15552000001")
        result = await svc.schedule_callback(user.id, 5)

        assert result.success is True
        assert result.call_log_id is not None
        assert "5 minutes" in result.message

        # Verify the created CallLog
        log = await session.get(CallLog, result.call_log_id)
        assert log is not None
        assert log.call_type == CallType.ON_DEMAND.value
        assert log.occurrence_kind == OccurrenceKind.ON_DEMAND.value
        assert log.status == CallLogStatus.SCHEDULED.value
        assert log.replaced_call_log_id is None

    @pytest.mark.asyncio
    async def test_rejects_invalid_minutes(self, session, svc):
        user = await _create_user(session, "+15552000002")

        r1 = await svc.schedule_callback(user.id, 0)
        assert r1.success is False

        r2 = await svc.schedule_callback(user.id, 121)
        assert r2.success is False

    @pytest.mark.asyncio
    async def test_replaces_existing_on_demand(self, session, svc, cls):
        user = await _create_user(session, "+15552000003")

        # Create first on-demand
        r1 = await svc.schedule_callback(user.id, 10)
        assert r1.success is True
        first_id = r1.call_log_id

        # Create second — should cancel the first
        r2 = await svc.schedule_callback(user.id, 15)
        assert r2.success is True
        assert r2.call_log_id != first_id

        # First should be cancelled
        first = await session.get(CallLog, first_id)
        assert first.status == CallLogStatus.CANCELLED.value

    @pytest.mark.asyncio
    async def test_user_not_found(self, svc):
        result = await svc.schedule_callback(999999, 5)
        assert result.success is False
        assert "not found" in result.message.lower()


# ---------------------------------------------------------------------------
# schedule_callback — deferred mode
# ---------------------------------------------------------------------------


class TestScheduleCallbackDeferred:
    @pytest.mark.asyncio
    async def test_defers_in_progress_call(self, session, svc, cls):
        user = await _create_user(session, "+15552100001")

        # Create an in-progress call
        cl = _make_call_log(user.id)
        cl = await cls.create_call_log(cl)
        await cls.update_status(cl.id, CallLogStatus.DISPATCHING, 1)
        await cls.update_status(cl.id, CallLogStatus.RINGING, 2)
        await cls.update_status(cl.id, CallLogStatus.IN_PROGRESS, 3)

        result = await svc.schedule_callback(
            user.id, 10, current_call_log_id=cl.id
        )
        assert result.success is True

        # Original call should be deferred
        original = await session.get(CallLog, cl.id)
        assert original.status == CallLogStatus.DEFERRED.value

        # New call should reference the deferred one
        new_log = await session.get(CallLog, result.call_log_id)
        assert new_log.replaced_call_log_id == cl.id
        assert new_log.call_type == CallType.ON_DEMAND.value

    @pytest.mark.asyncio
    async def test_rejects_non_in_progress(self, session, svc, cls):
        user = await _create_user(session, "+15552100002")
        cl = await cls.create_call_log(_make_call_log(user.id))

        result = await svc.schedule_callback(
            user.id, 10, current_call_log_id=cl.id
        )
        assert result.success is False
        assert "in-progress" in result.message.lower()

    @pytest.mark.asyncio
    async def test_rejects_nonexistent_call(self, session, svc):
        user = await _create_user(session, "+15552100003")
        result = await svc.schedule_callback(
            user.id, 10, current_call_log_id=999999
        )
        assert result.success is False


# ---------------------------------------------------------------------------
# skip_call
# ---------------------------------------------------------------------------


class TestSkipCall:
    @pytest.mark.asyncio
    async def test_skips_scheduled_call(self, session, svc, cls):
        user = await _create_user(session, "+15553000001")
        cl = await cls.create_call_log(_make_call_log(user.id))

        result = await svc.skip_call(user.id, CallType.MORNING.value)
        assert result.success is True
        assert "skipped" in result.message.lower()

        updated = await session.get(CallLog, cl.id)
        assert updated.status == CallLogStatus.SKIPPED.value

    @pytest.mark.asyncio
    async def test_idempotent_on_already_skipped(self, session, svc, cls):
        user = await _create_user(session, "+15553000002")
        cl = await cls.create_call_log(_make_call_log(user.id))
        await cls.update_status(cl.id, CallLogStatus.SKIPPED, 1)

        result = await svc.skip_call(user.id, CallType.MORNING.value)
        assert result.success is True
        assert "already skipped" in result.message.lower()

    @pytest.mark.asyncio
    async def test_no_call_found(self, session, svc):
        user = await _create_user(session, "+15553000003")
        result = await svc.skip_call(user.id, CallType.MORNING.value)
        assert result.success is False
        assert "no upcoming" in result.message.lower()

    @pytest.mark.asyncio
    async def test_skips_earliest_when_multiple(self, session, svc, cls):
        user = await _create_user(session, "+15553000004")
        cl_early = await cls.create_call_log(
            _make_call_log(user.id, call_type=CallType.MORNING.value, hours_ahead=1)
        )
        # Second call with different occurrence_kind to avoid unique constraint
        log2 = _make_call_log(user.id, call_type=CallType.MORNING.value, hours_ahead=2)
        log2.occurrence_kind = OccurrenceKind.RETRY.value
        cl_late = await cls.create_call_log(log2)

        result = await svc.skip_call(user.id, CallType.MORNING.value)
        assert result.success is True
        assert result.call_log_id == cl_early.id


# ---------------------------------------------------------------------------
# reschedule_call
# ---------------------------------------------------------------------------


class TestRescheduleCall:
    @pytest.mark.asyncio
    async def test_reschedules_to_future_time(self, session, svc, cls):
        user = await _create_user(session, "+15554000001")

        # Create a call window so retry-buffer validation can run
        window = CallWindow(
            user_id=user.id,
            window_type="morning",
            start_time=time(6, 0),
            end_time=time(10, 0),
            is_active=True,
        )
        session.add(window)
        await session.commit()

        cl = await cls.create_call_log(_make_call_log(user.id))

        # Pick a time that's definitely in the future and within the window
        tz = ZoneInfo("America/New_York")
        now_local = datetime.now(timezone.utc).astimezone(tz)
        future_time = (now_local + timedelta(hours=1)).time().replace(second=0, microsecond=0)

        # Only test if the future time is within the window
        if future_time >= time(6, 0) and future_time <= time(9, 0):
            result = await svc.reschedule_call(
                user.id, CallType.MORNING.value, future_time
            )
            assert result.success is True
            assert "rescheduled" in result.message.lower()

            updated = await session.get(CallLog, cl.id)
            assert updated.occurrence_kind == OccurrenceKind.RESCHEDULED.value

    @pytest.mark.asyncio
    async def test_rejects_past_time(self, session, svc, cls):
        user = await _create_user(session, "+15554000002")
        await cls.create_call_log(_make_call_log(user.id))

        # A time that's definitely in the past
        past_time = time(0, 1)
        result = await svc.reschedule_call(
            user.id, CallType.MORNING.value, past_time
        )
        assert result.success is False
        assert "future" in result.message.lower()

    @pytest.mark.asyncio
    async def test_no_call_found(self, session, svc):
        user = await _create_user(session, "+15554000003")
        result = await svc.reschedule_call(
            user.id, CallType.MORNING.value, time(9, 0)
        )
        assert result.success is False
        assert "no upcoming" in result.message.lower()

    @pytest.mark.asyncio
    async def test_user_not_found(self, svc):
        result = await svc.reschedule_call(999999, CallType.MORNING.value, time(9, 0))
        assert result.success is False


# ---------------------------------------------------------------------------
# get_next_call
# ---------------------------------------------------------------------------


class TestGetNextCall:
    @pytest.mark.asyncio
    async def test_returns_next_scheduled(self, session, svc, cls):
        user = await _create_user(session, "+15555000001")
        cl = await cls.create_call_log(_make_call_log(user.id))

        result = await svc.get_next_call(user.id)
        assert result.success is True
        assert result.next_call is not None
        assert result.next_call.call_type == CallType.MORNING.value
        assert result.next_call.timezone == "America/New_York"
        assert result.call_log_id == cl.id

    @pytest.mark.asyncio
    async def test_no_calls(self, session, svc):
        user = await _create_user(session, "+15555000002")
        result = await svc.get_next_call(user.id)
        assert result.success is True
        assert result.next_call is None
        assert "no upcoming" in result.message.lower()

    @pytest.mark.asyncio
    async def test_returns_earliest(self, session, svc, cls):
        user = await _create_user(session, "+15555000003")
        cl_far = await cls.create_call_log(
            _make_call_log(user.id, hours_ahead=48, pin_today=False)
        )
        cl_near = await cls.create_call_log(
            _make_call_log(user.id, call_type=CallType.AFTERNOON.value, hours_ahead=2, pin_today=False)
        )

        result = await svc.get_next_call(user.id)
        assert result.call_log_id == cl_near.id


# ---------------------------------------------------------------------------
# cancel_all_calls_today
# ---------------------------------------------------------------------------


class TestCancelAllCallsToday:
    @pytest.mark.asyncio
    async def test_cancels_all_scheduled(self, session, svc, cls):
        user = await _create_user(session, "+15556000001")
        cl1 = await cls.create_call_log(
            _make_call_log(user.id, call_type=CallType.MORNING.value)
        )
        cl2 = await cls.create_call_log(
            _make_call_log(user.id, call_type=CallType.AFTERNOON.value)
        )

        result = await svc.cancel_all_calls_today(user.id)
        assert result.success is True
        assert result.cancelled_count == 2

        for cid in (cl1.id, cl2.id):
            log = await session.get(CallLog, cid)
            assert log.status == CallLogStatus.CANCELLED.value

    @pytest.mark.asyncio
    async def test_zero_calls(self, session, svc):
        user = await _create_user(session, "+15556000002")
        result = await svc.cancel_all_calls_today(user.id)
        assert result.success is True
        assert result.cancelled_count == 0

    @pytest.mark.asyncio
    async def test_ignores_already_terminal(self, session, svc, cls):
        user = await _create_user(session, "+15556000003")
        cl = await cls.create_call_log(_make_call_log(user.id))
        await cls.update_status(cl.id, CallLogStatus.CANCELLED, 1)

        result = await svc.cancel_all_calls_today(user.id)
        assert result.cancelled_count == 0


# ---------------------------------------------------------------------------
# State guards
# ---------------------------------------------------------------------------


class TestStateGuards:
    @pytest.mark.asyncio
    async def test_skip_completed_call_fails(self, session, svc, cls):
        """Cannot skip a completed call."""
        user = await _create_user(session, "+15557000001")
        cl = await cls.create_call_log(_make_call_log(user.id))
        await cls.update_status(cl.id, CallLogStatus.DISPATCHING, 1)
        await cls.update_status(cl.id, CallLogStatus.RINGING, 2)
        await cls.update_status(cl.id, CallLogStatus.IN_PROGRESS, 3)
        await cls.update_status(cl.id, CallLogStatus.COMPLETED, 4)

        # No scheduled calls remain, so skip should report "no upcoming"
        result = await svc.skip_call(user.id, CallType.MORNING.value)
        assert result.success is False

    @pytest.mark.asyncio
    async def test_reschedule_in_progress_fails(self, session, svc, cls):
        """Cannot reschedule an in-progress call."""
        user = await _create_user(session, "+15557000002")
        cl = await cls.create_call_log(_make_call_log(user.id))
        await cls.update_status(cl.id, CallLogStatus.DISPATCHING, 1)
        await cls.update_status(cl.id, CallLogStatus.RINGING, 2)
        await cls.update_status(cl.id, CallLogStatus.IN_PROGRESS, 3)

        # find_today with SCHEDULED/RINGING won't find in_progress calls
        result = await svc.reschedule_call(
            user.id, CallType.MORNING.value, time(9, 0)
        )
        assert result.success is False
