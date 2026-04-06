"""Unit tests for CallWindowService.

Tests cover:
- save_call_window: validation, upsert, hard-delete on time change
- list_windows_for_user: active-only filtering
- update_window: partial updates, validation, hard-delete
- deactivate_window: soft-deactivate + hard-delete
"""

from datetime import datetime, time, timedelta, timezone

import pytest
import pytest_asyncio
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
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def svc(session: AsyncSession) -> CallWindowService:
    return CallWindowService(session)


async def _create_user(
    session: AsyncSession, phone: str, tz: str = "America/New_York"
) -> User:
    """Insert a test user and return it."""
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


async def _create_future_planned_call_log(
    session: AsyncSession, user_id: int, call_type: str, hours_ahead: int = 24
) -> CallLog:
    """Insert a future scheduled planned CallLog entry."""
    future_time = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
    cl = CallLog(
        user_id=user_id,
        call_type=call_type,
        call_date=future_time.date(),
        scheduled_time=future_time,
        scheduled_timezone="America/New_York",
        status=CallLogStatus.SCHEDULED.value,
        occurrence_kind=OccurrenceKind.PLANNED.value,
        attempt_number=1,
    )
    session.add(cl)
    await session.commit()
    await session.refresh(cl)
    return cl


# ---------------------------------------------------------------------------
# save_call_window
# ---------------------------------------------------------------------------


class TestSaveCallWindow:
    @pytest.mark.asyncio
    async def test_creates_new_window(self, session, svc):
        user = await _create_user(session, "+15551000001")
        window = await svc.save_call_window(
            user.id, WindowType.MORNING.value, time(7, 0), time(8, 0)
        )
        assert window.id is not None
        assert window.user_id == user.id
        assert window.window_type == WindowType.MORNING.value
        assert window.start_time == time(7, 0)
        assert window.end_time == time(8, 0)
        assert window.is_active is True

    @pytest.mark.asyncio
    async def test_upsert_same_values_is_idempotent(self, session, svc):
        user = await _create_user(session, "+15551000002")
        w1 = await svc.save_call_window(
            user.id, WindowType.MORNING.value, time(7, 0), time(8, 0)
        )
        w2 = await svc.save_call_window(
            user.id, WindowType.MORNING.value, time(7, 0), time(8, 0)
        )
        assert w1.id == w2.id

    @pytest.mark.asyncio
    async def test_upsert_updates_times(self, session, svc):
        user = await _create_user(session, "+15551000003")
        w1 = await svc.save_call_window(
            user.id, WindowType.MORNING.value, time(7, 0), time(8, 0)
        )
        w2 = await svc.save_call_window(
            user.id, WindowType.MORNING.value, time(7, 30), time(8, 30)
        )
        assert w1.id == w2.id
        assert w2.start_time == time(7, 30)
        assert w2.end_time == time(8, 30)

    @pytest.mark.asyncio
    async def test_rejects_narrow_window(self, session, svc):
        user = await _create_user(session, "+15551000004")
        with pytest.raises(ValueError, match="20 minutes"):
            await svc.save_call_window(
                user.id, WindowType.MORNING.value, time(7, 0), time(7, 10)
            )

    @pytest.mark.asyncio
    async def test_rejects_cross_midnight(self, session, svc):
        user = await _create_user(session, "+15551000005")
        with pytest.raises(ValueError, match="after start time"):
            await svc.save_call_window(
                user.id, WindowType.EVENING.value, time(23, 0), time(0, 30)
            )

    @pytest.mark.asyncio
    async def test_rejects_missing_timezone(self, session, svc):
        user = User(
            phone="+15551000006",
            timezone=None,
            onboarding_complete=False,
            consecutive_active_days=0,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

        with pytest.raises(ValueError, match="no timezone"):
            await svc.save_call_window(
                user.id, WindowType.MORNING.value, time(7, 0), time(8, 0)
            )

    @pytest.mark.asyncio
    async def test_rejects_nonexistent_user(self, svc):
        with pytest.raises(ValueError, match="not found"):
            await svc.save_call_window(
                999999, WindowType.MORNING.value, time(7, 0), time(8, 0)
            )

    @pytest.mark.asyncio
    async def test_hard_deletes_future_planned_on_time_change(self, session, svc):
        user = await _create_user(session, "+15551000007")
        await svc.save_call_window(
            user.id, WindowType.MORNING.value, time(7, 0), time(8, 0)
        )
        cl = await _create_future_planned_call_log(
            session, user.id, CallType.MORNING.value
        )
        cl_id = cl.id

        await svc.save_call_window(
            user.id, WindowType.MORNING.value, time(7, 30), time(8, 30)
        )

        deleted_cl = await session.get(CallLog, cl_id)
        assert deleted_cl is None

    @pytest.mark.asyncio
    async def test_no_delete_when_times_unchanged(self, session, svc):
        user = await _create_user(session, "+15551000008")
        await svc.save_call_window(
            user.id, WindowType.MORNING.value, time(7, 0), time(8, 0)
        )
        cl = await _create_future_planned_call_log(
            session, user.id, CallType.MORNING.value
        )
        cl_id = cl.id

        await svc.save_call_window(
            user.id, WindowType.MORNING.value, time(7, 0), time(8, 0)
        )

        still_exists = await session.get(CallLog, cl_id)
        assert still_exists is not None

    @pytest.mark.asyncio
    async def test_reactivates_inactive_window(self, session, svc):
        user = await _create_user(session, "+15551000009")
        w = await svc.save_call_window(
            user.id, WindowType.MORNING.value, time(7, 0), time(8, 0)
        )
        await svc.deactivate_window(w.id)

        w2 = await svc.save_call_window(
            user.id, WindowType.MORNING.value, time(7, 0), time(8, 0)
        )
        assert w2.id == w.id
        assert w2.is_active is True


# ---------------------------------------------------------------------------
# list_windows_for_user
# ---------------------------------------------------------------------------


class TestListWindowsForUser:
    @pytest.mark.asyncio
    async def test_returns_active_only(self, session, svc):
        user = await _create_user(session, "+15552000001")
        await svc.save_call_window(
            user.id, WindowType.MORNING.value, time(7, 0), time(8, 0)
        )
        w_eve = await svc.save_call_window(
            user.id, WindowType.EVENING.value, time(20, 0), time(21, 0)
        )
        await svc.deactivate_window(w_eve.id)

        windows = await svc.list_windows_for_user(user.id)
        assert len(windows) == 1
        assert windows[0].window_type == WindowType.MORNING.value

    @pytest.mark.asyncio
    async def test_returns_empty_for_no_windows(self, session, svc):
        user = await _create_user(session, "+15552000002")
        windows = await svc.list_windows_for_user(user.id)
        assert windows == []


# ---------------------------------------------------------------------------
# update_window
# ---------------------------------------------------------------------------


class TestUpdateWindow:
    @pytest.mark.asyncio
    async def test_updates_start_time_only(self, session, svc):
        user = await _create_user(session, "+15553000001")
        w = await svc.save_call_window(
            user.id, WindowType.MORNING.value, time(7, 0), time(8, 0)
        )
        updated = await svc.update_window(w.id, start_time=time(6, 30))
        assert updated.start_time == time(6, 30)
        assert updated.end_time == time(8, 0)

    @pytest.mark.asyncio
    async def test_updates_end_time_only(self, session, svc):
        user = await _create_user(session, "+15553000002")
        w = await svc.save_call_window(
            user.id, WindowType.MORNING.value, time(7, 0), time(8, 0)
        )
        updated = await svc.update_window(w.id, end_time=time(9, 0))
        assert updated.start_time == time(7, 0)
        assert updated.end_time == time(9, 0)

    @pytest.mark.asyncio
    async def test_rejects_invalid_update(self, session, svc):
        user = await _create_user(session, "+15553000003")
        w = await svc.save_call_window(
            user.id, WindowType.MORNING.value, time(7, 0), time(8, 0)
        )
        with pytest.raises(ValueError, match="20 minutes"):
            await svc.update_window(w.id, start_time=time(7, 50))

    @pytest.mark.asyncio
    async def test_hard_deletes_on_update(self, session, svc):
        user = await _create_user(session, "+15553000004")
        w = await svc.save_call_window(
            user.id, WindowType.AFTERNOON.value, time(13, 0), time(14, 0)
        )
        cl = await _create_future_planned_call_log(
            session, user.id, CallType.AFTERNOON.value
        )
        cl_id = cl.id

        await svc.update_window(w.id, start_time=time(13, 30))

        deleted_cl = await session.get(CallLog, cl_id)
        assert deleted_cl is None

    @pytest.mark.asyncio
    async def test_nonexistent_window_raises(self, svc):
        with pytest.raises(ValueError, match="not found"):
            await svc.update_window(999999, start_time=time(7, 0))


# ---------------------------------------------------------------------------
# deactivate_window
# ---------------------------------------------------------------------------


class TestDeactivateWindow:
    @pytest.mark.asyncio
    async def test_sets_inactive(self, session, svc):
        user = await _create_user(session, "+15554000001")
        w = await svc.save_call_window(
            user.id, WindowType.EVENING.value, time(20, 0), time(21, 0)
        )
        deactivated = await svc.deactivate_window(w.id)
        assert deactivated.is_active is False

    @pytest.mark.asyncio
    async def test_hard_deletes_future_planned(self, session, svc):
        user = await _create_user(session, "+15554000002")
        w = await svc.save_call_window(
            user.id, WindowType.EVENING.value, time(20, 0), time(21, 0)
        )
        cl = await _create_future_planned_call_log(
            session, user.id, CallType.EVENING.value
        )
        cl_id = cl.id

        await svc.deactivate_window(w.id)

        deleted_cl = await session.get(CallLog, cl_id)
        assert deleted_cl is None

    @pytest.mark.asyncio
    async def test_nonexistent_window_raises(self, svc):
        with pytest.raises(ValueError, match="not found"):
            await svc.deactivate_window(999999)
