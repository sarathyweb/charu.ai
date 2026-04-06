"""Unit tests for CallLogService.

Tests cover:
- State machine: VALID_TRANSITIONS, validate_transition
- create_call_log: basic creation
- update_status: valid transitions, invalid transitions, optimistic locking
- find_by_twilio_sid: lookup by SID
- find_next_scheduled: next scheduled call
- find_all_scheduled_today: timezone-aware "today" filtering
"""

from datetime import date, datetime, time, timedelta, timezone

import pytest
import pytest_asyncio
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.call_log import CallLog
from app.models.enums import (
    CallLogStatus,
    CallType,
    OccurrenceKind,
)
from app.models.user import User
from app.services.call_log_service import (
    TERMINAL_STATUSES,
    VALID_TRANSITIONS,
    CallLogService,
    InvalidTransitionError,
    StaleVersionError,
    validate_transition,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def svc(session: AsyncSession) -> CallLogService:
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
    hours_ahead: int = 1,
    tz: str = "America/New_York",
    **kwargs,
) -> CallLog:
    """Build a CallLog instance (not yet persisted)."""
    scheduled = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
    return CallLog(
        user_id=user_id,
        call_type=call_type,
        call_date=scheduled.date(),
        scheduled_time=scheduled,
        scheduled_timezone=tz,
        status=status,
        occurrence_kind=OccurrenceKind.PLANNED.value,
        attempt_number=1,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# State machine unit tests (pure functions, no DB)
# ---------------------------------------------------------------------------


class TestStateMachine:
    def test_terminal_states_have_no_outgoing(self):
        for status in TERMINAL_STATUSES:
            assert VALID_TRANSITIONS[status] == set(), (
                f"{status.value} should have no outgoing transitions"
            )

    def test_scheduled_can_reach_dispatching(self):
        assert validate_transition(CallLogStatus.SCHEDULED, CallLogStatus.DISPATCHING)

    def test_dispatching_can_reach_ringing(self):
        assert validate_transition(CallLogStatus.DISPATCHING, CallLogStatus.RINGING)

    def test_dispatching_can_revert_to_scheduled(self):
        assert validate_transition(CallLogStatus.DISPATCHING, CallLogStatus.SCHEDULED)

    def test_dispatching_can_reach_missed(self):
        assert validate_transition(CallLogStatus.DISPATCHING, CallLogStatus.MISSED)

    def test_ringing_to_in_progress(self):
        assert validate_transition(CallLogStatus.RINGING, CallLogStatus.IN_PROGRESS)

    def test_ringing_to_missed(self):
        assert validate_transition(CallLogStatus.RINGING, CallLogStatus.MISSED)

    def test_ringing_to_cancelled(self):
        assert validate_transition(CallLogStatus.RINGING, CallLogStatus.CANCELLED)

    def test_in_progress_to_completed(self):
        assert validate_transition(CallLogStatus.IN_PROGRESS, CallLogStatus.COMPLETED)

    def test_completed_cannot_transition(self):
        for target in CallLogStatus:
            if target != CallLogStatus.COMPLETED:
                assert not validate_transition(CallLogStatus.COMPLETED, target)

    def test_missed_cannot_transition(self):
        for target in CallLogStatus:
            if target != CallLogStatus.MISSED:
                assert not validate_transition(CallLogStatus.MISSED, target)

    def test_accepts_string_values(self):
        assert validate_transition("scheduled", "dispatching")
        assert not validate_transition("completed", "scheduled")

    def test_scheduled_to_skipped(self):
        assert validate_transition(CallLogStatus.SCHEDULED, CallLogStatus.SKIPPED)

    def test_scheduled_to_deferred(self):
        assert validate_transition(CallLogStatus.SCHEDULED, CallLogStatus.DEFERRED)

    def test_scheduled_to_cancelled(self):
        assert validate_transition(CallLogStatus.SCHEDULED, CallLogStatus.CANCELLED)

    def test_invalid_ringing_to_scheduled(self):
        assert not validate_transition(CallLogStatus.RINGING, CallLogStatus.SCHEDULED)

    def test_invalid_in_progress_to_ringing(self):
        assert not validate_transition(CallLogStatus.IN_PROGRESS, CallLogStatus.RINGING)


# ---------------------------------------------------------------------------
# create_call_log
# ---------------------------------------------------------------------------


class TestCreateCallLog:
    @pytest.mark.asyncio
    async def test_creates_and_returns_with_id(self, session, svc):
        user = await _create_user(session, "+15551100001")
        cl = _make_call_log(user.id)
        result = await svc.create_call_log(cl)
        assert result.id is not None
        assert result.user_id == user.id
        assert result.status == CallLogStatus.SCHEDULED.value
        assert result.version == 1

    @pytest.mark.asyncio
    async def test_default_version_is_one(self, session, svc):
        user = await _create_user(session, "+15551100002")
        cl = _make_call_log(user.id)
        result = await svc.create_call_log(cl)
        assert result.version == 1


# ---------------------------------------------------------------------------
# update_status
# ---------------------------------------------------------------------------


class TestUpdateStatus:
    @pytest.mark.asyncio
    async def test_valid_transition_succeeds(self, session, svc):
        user = await _create_user(session, "+15551200001")
        cl = await svc.create_call_log(_make_call_log(user.id))

        updated = await svc.update_status(
            cl.id, CallLogStatus.DISPATCHING, expected_version=1
        )
        assert updated.status == CallLogStatus.DISPATCHING.value
        assert updated.version == 2

    @pytest.mark.asyncio
    async def test_extra_fields_are_set(self, session, svc):
        user = await _create_user(session, "+15551200002")
        cl = await svc.create_call_log(_make_call_log(user.id))

        now = datetime.now(timezone.utc)
        updated = await svc.update_status(
            cl.id,
            CallLogStatus.DISPATCHING,
            expected_version=1,
            twilio_call_sid="CA_test_123",
        )
        assert updated.twilio_call_sid == "CA_test_123"

    @pytest.mark.asyncio
    async def test_invalid_transition_raises(self, session, svc):
        user = await _create_user(session, "+15551200003")
        cl = await svc.create_call_log(_make_call_log(user.id))

        # scheduled → completed is not valid (must go through dispatching/ringing/in_progress)
        with pytest.raises(InvalidTransitionError):
            await svc.update_status(cl.id, CallLogStatus.COMPLETED, expected_version=1)

    @pytest.mark.asyncio
    async def test_terminal_state_rejects_all(self, session, svc):
        user = await _create_user(session, "+15551200004")
        cl = await svc.create_call_log(_make_call_log(user.id))

        # Move to terminal: scheduled → cancelled
        await svc.update_status(cl.id, CallLogStatus.CANCELLED, expected_version=1)

        with pytest.raises(InvalidTransitionError):
            await svc.update_status(cl.id, CallLogStatus.SCHEDULED, expected_version=2)

    @pytest.mark.asyncio
    async def test_stale_version_raises(self, session, svc):
        user = await _create_user(session, "+15551200005")
        cl = await svc.create_call_log(_make_call_log(user.id))

        # First update succeeds
        await svc.update_status(cl.id, CallLogStatus.DISPATCHING, expected_version=1)

        # Second update with stale version fails
        with pytest.raises(StaleVersionError):
            await svc.update_status(cl.id, CallLogStatus.RINGING, expected_version=1)

    @pytest.mark.asyncio
    async def test_nonexistent_call_log_raises(self, svc):
        with pytest.raises(ValueError, match="not found"):
            await svc.update_status(
                999999, CallLogStatus.DISPATCHING, expected_version=1
            )

    @pytest.mark.asyncio
    async def test_chained_transitions(self, session, svc):
        """Walk through the full happy path: scheduled → dispatching → ringing → in_progress → completed."""
        user = await _create_user(session, "+15551200006")
        cl = await svc.create_call_log(_make_call_log(user.id))

        cl = await svc.update_status(
            cl.id, CallLogStatus.DISPATCHING, expected_version=1
        )
        assert cl.version == 2

        cl = await svc.update_status(cl.id, CallLogStatus.RINGING, expected_version=2)
        assert cl.version == 3

        cl = await svc.update_status(
            cl.id,
            CallLogStatus.IN_PROGRESS,
            expected_version=3,
            actual_start_time=datetime.now(timezone.utc),
        )
        assert cl.version == 4
        assert cl.actual_start_time is not None

        cl = await svc.update_status(cl.id, CallLogStatus.COMPLETED, expected_version=4)
        assert cl.version == 5
        assert cl.status == CallLogStatus.COMPLETED.value

    @pytest.mark.asyncio
    async def test_dispatching_revert_to_scheduled(self, session, svc):
        """Transport error: dispatching → scheduled."""
        user = await _create_user(session, "+15551200007")
        cl = await svc.create_call_log(_make_call_log(user.id))

        cl = await svc.update_status(
            cl.id, CallLogStatus.DISPATCHING, expected_version=1
        )
        cl = await svc.update_status(cl.id, CallLogStatus.SCHEDULED, expected_version=2)
        assert cl.status == CallLogStatus.SCHEDULED.value
        assert cl.version == 3

    @pytest.mark.asyncio
    async def test_accepts_string_status(self, session, svc):
        user = await _create_user(session, "+15551200008")
        cl = await svc.create_call_log(_make_call_log(user.id))

        updated = await svc.update_status(cl.id, "dispatching", expected_version=1)
        assert updated.status == CallLogStatus.DISPATCHING.value


# ---------------------------------------------------------------------------
# find_by_twilio_sid
# ---------------------------------------------------------------------------


class TestFindByTwilioSid:
    @pytest.mark.asyncio
    async def test_finds_existing(self, session, svc):
        user = await _create_user(session, "+15551300001")
        cl = _make_call_log(user.id, twilio_call_sid="CA_find_test")
        await svc.create_call_log(cl)

        found = await svc.find_by_twilio_sid("CA_find_test")
        assert found is not None
        assert found.twilio_call_sid == "CA_find_test"

    @pytest.mark.asyncio
    async def test_returns_none_for_missing(self, svc):
        found = await svc.find_by_twilio_sid("CA_nonexistent")
        assert found is None


# ---------------------------------------------------------------------------
# find_next_scheduled
# ---------------------------------------------------------------------------


class TestFindNextScheduled:
    @pytest.mark.asyncio
    async def test_returns_earliest_future(self, session, svc):
        user = await _create_user(session, "+15551400001")
        cl_far = _make_call_log(user.id, hours_ahead=48)
        cl_near = _make_call_log(
            user.id, call_type=CallType.AFTERNOON.value, hours_ahead=2
        )
        await svc.create_call_log(cl_far)
        await svc.create_call_log(cl_near)

        found = await svc.find_next_scheduled(user.id)
        assert found is not None
        assert found.id == cl_near.id

    @pytest.mark.asyncio
    async def test_ignores_non_scheduled(self, session, svc):
        user = await _create_user(session, "+15551400002")
        cl = await svc.create_call_log(_make_call_log(user.id))
        await svc.update_status(cl.id, CallLogStatus.CANCELLED, expected_version=1)

        found = await svc.find_next_scheduled(user.id)
        assert found is None

    @pytest.mark.asyncio
    async def test_returns_none_when_empty(self, session, svc):
        user = await _create_user(session, "+15551400003")
        found = await svc.find_next_scheduled(user.id)
        assert found is None


# ---------------------------------------------------------------------------
# find_all_scheduled_today
# ---------------------------------------------------------------------------


class TestFindAllScheduledToday:
    @pytest.mark.asyncio
    async def test_returns_todays_calls(self, session, svc):
        user = await _create_user(session, "+15551500001", tz="America/New_York")

        # Create a call log with call_date = today in NY timezone
        from zoneinfo import ZoneInfo

        ny = ZoneInfo("America/New_York")
        now_ny = datetime.now(timezone.utc).astimezone(ny)
        today_ny = now_ny.date()

        # A call scheduled for later today
        scheduled_time = datetime.now(timezone.utc) + timedelta(hours=1)
        cl = CallLog(
            user_id=user.id,
            call_type=CallType.MORNING.value,
            call_date=today_ny,
            scheduled_time=scheduled_time,
            scheduled_timezone="America/New_York",
            status=CallLogStatus.SCHEDULED.value,
            occurrence_kind=OccurrenceKind.PLANNED.value,
            attempt_number=1,
        )
        await svc.create_call_log(cl)

        results = await svc.find_all_scheduled_today(user.id)
        assert len(results) == 1
        assert results[0].id == cl.id

    @pytest.mark.asyncio
    async def test_excludes_other_days(self, session, svc):
        user = await _create_user(session, "+15551500002", tz="America/New_York")

        from zoneinfo import ZoneInfo

        ny = ZoneInfo("America/New_York")
        now_ny = datetime.now(timezone.utc).astimezone(ny)
        tomorrow_ny = now_ny.date() + timedelta(days=1)

        # A call scheduled for tomorrow
        scheduled_time = datetime.now(timezone.utc) + timedelta(hours=25)
        cl = CallLog(
            user_id=user.id,
            call_type=CallType.MORNING.value,
            call_date=tomorrow_ny,
            scheduled_time=scheduled_time,
            scheduled_timezone="America/New_York",
            status=CallLogStatus.SCHEDULED.value,
            occurrence_kind=OccurrenceKind.PLANNED.value,
            attempt_number=1,
        )
        await svc.create_call_log(cl)

        results = await svc.find_all_scheduled_today(user.id)
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_uses_scheduled_timezone_not_user_timezone(self, session, svc):
        """Verify that the query uses the CallLog's scheduled_timezone snapshot,
        not the user's current timezone."""
        user = await _create_user(session, "+15551500003", tz="Asia/Tokyo")

        from zoneinfo import ZoneInfo

        # The call was materialized when user was in NY
        ny = ZoneInfo("America/New_York")
        now_ny = datetime.now(timezone.utc).astimezone(ny)
        today_ny = now_ny.date()

        scheduled_time = datetime.now(timezone.utc) + timedelta(hours=1)
        cl = CallLog(
            user_id=user.id,
            call_type=CallType.MORNING.value,
            call_date=today_ny,
            scheduled_time=scheduled_time,
            scheduled_timezone="America/New_York",  # snapshot at materialization
            status=CallLogStatus.SCHEDULED.value,
            occurrence_kind=OccurrenceKind.PLANNED.value,
            attempt_number=1,
        )
        await svc.create_call_log(cl)

        # Even though user.timezone is now Asia/Tokyo, the query should
        # use scheduled_timezone (America/New_York) to determine "today"
        results = await svc.find_all_scheduled_today(user.id)
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_excludes_non_scheduled_status(self, session, svc):
        user = await _create_user(session, "+15551500004", tz="America/New_York")

        from zoneinfo import ZoneInfo

        ny = ZoneInfo("America/New_York")
        today_ny = datetime.now(timezone.utc).astimezone(ny).date()

        scheduled_time = datetime.now(timezone.utc) + timedelta(hours=1)
        cl = CallLog(
            user_id=user.id,
            call_type=CallType.MORNING.value,
            call_date=today_ny,
            scheduled_time=scheduled_time,
            scheduled_timezone="America/New_York",
            status=CallLogStatus.SCHEDULED.value,
            occurrence_kind=OccurrenceKind.PLANNED.value,
            attempt_number=1,
        )
        cl = await svc.create_call_log(cl)
        await svc.update_status(cl.id, CallLogStatus.CANCELLED, expected_version=1)

        results = await svc.find_all_scheduled_today(user.id)
        assert len(results) == 0
