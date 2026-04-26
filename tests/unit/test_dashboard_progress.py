"""Dashboard progress and integration-connect regression tests."""

from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

import app.api.dashboard as dashboard
from app.api.dashboard import (
    CallWindowRequest,
    CallWindowUpdateRequest,
    GoalCreateRequest,
    GoalUpdateRequest,
    TaskSnoozeRequest,
    TaskUpdateRequest,
    UserProfileUpdateRequest,
    abandon_goal,
    complete_goal,
    complete_task,
    connect_integration,
    create_call_window,
    create_goal,
    delete_call_window,
    delete_goal,
    delete_task,
    get_call_history,
    get_goals,
    get_progress,
    get_tasks,
    snooze_task,
    unsnooze_task,
    update_call_window,
    update_goal,
    update_task,
    update_user_profile,
)
from app.models.call_log import CallLog
from app.models.call_window import CallWindow
from app.models.enums import (
    CallLogStatus,
    CallType,
    GoalStatus,
    OccurrenceKind,
    OutcomeConfidence,
)
from app.models.schemas import FirebasePrincipal
from app.models.user import User
from app.services.call_window_service import CallWindowService
from app.services.goal_service import GoalService
from app.services.task_service import TaskService
from app.services.user_service import UserService


async def _create_user(session, *, timezone_name: str = "UTC") -> User:
    user = User(
        phone="+14155550100",
        firebase_uid="dashboard-test-uid",
        timezone=timezone_name,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _add_window(session, user_id: int, window_type: str, hour: int) -> None:
    session.add(
        CallWindow(
            user_id=user_id,
            window_type=window_type,
            start_time=time(hour, 0),
            end_time=time(hour, 30),
            is_active=True,
        )
    )


async def _add_completed_call(
    session,
    user_id: int,
    call_date: date,
    call_type: str,
    *,
    confidence: str | None = None,
    occurrence_kind: str = OccurrenceKind.PLANNED.value,
) -> None:
    session.add(
        CallLog(
            user_id=user_id,
            call_type=call_type,
            call_date=call_date,
            scheduled_time=datetime.combine(
                call_date, time(12, 0), tzinfo=timezone.utc
            ),
            scheduled_timezone="UTC",
            status=CallLogStatus.COMPLETED.value,
            occurrence_kind=occurrence_kind,
            call_outcome_confidence=confidence,
        )
    )


async def _progress_for_user(session, user: User) -> dict:
    principal = FirebasePrincipal(
        uid=user.firebase_uid or "uid", phone_number=user.phone
    )
    return await get_progress(
        principal=principal,
        user_service=UserService(session),
        session=session,
    )


def test_today_for_user_uses_user_timezone_at_utc_boundary(monkeypatch):
    """Dashboard dates follow the user's timezone, not the server/UTC date."""

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed_utc = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)
            return fixed_utc.astimezone(tz)

    monkeypatch.setattr(dashboard, "datetime", FrozenDateTime)

    pacific_user = User(id=1, phone="+14155550100", timezone="America/Los_Angeles")
    utc_user = User(id=2, phone="+14155550101", timezone="UTC")

    assert dashboard._today_for_user(pacific_user) == date(2025, 12, 31)
    assert dashboard._today_for_user(utc_user) == date(2026, 1, 1)


@pytest.mark.asyncio
async def test_progress_counts_call_rows_and_dynamic_weekly_total(session):
    """Weekly dashboard stats count scheduled calls, not active dates."""
    user = await _create_user(session, timezone_name="America/Los_Angeles")
    assert user.id is not None

    await _add_window(session, user.id, "morning", 9)
    await _add_window(session, user.id, "afternoon", 13)
    await _add_window(session, user.id, "evening", 18)

    today = datetime.now(ZoneInfo("America/Los_Angeles")).date()
    week_start = today - timedelta(days=today.weekday())
    prev_week_start = week_start - timedelta(days=7)

    await _add_completed_call(
        session,
        user.id,
        week_start,
        CallType.MORNING.value,
        confidence=OutcomeConfidence.CLEAR.value,
    )
    await _add_completed_call(
        session,
        user.id,
        week_start,
        CallType.AFTERNOON.value,
        confidence=OutcomeConfidence.PARTIAL.value,
    )
    await _add_completed_call(session, user.id, today, CallType.EVENING.value)
    await _add_completed_call(
        session,
        user.id,
        today,
        CallType.ON_DEMAND.value,
        occurrence_kind=OccurrenceKind.ON_DEMAND.value,
    )
    await _add_completed_call(
        session,
        user.id,
        prev_week_start,
        CallType.MORNING.value,
        confidence=OutcomeConfidence.CLEAR.value,
    )
    await session.commit()

    progress = await _progress_for_user(session, user)

    assert progress["week"]["calls_completed"] == 3
    assert progress["week"]["calls_total"] == 21
    assert progress["week"]["prev_calls_completed"] == 1
    assert progress["goals"]["completion_pct"] == 100
    assert "3 out of 21 calls" in progress["weekly_summary"]


@pytest.mark.asyncio
async def test_progress_streak_uses_history_beyond_heatmap_window(session):
    """Best streak is not capped by the 84-day heatmap."""
    user = await _create_user(session)
    assert user.id is not None

    await _add_window(session, user.id, "morning", 9)

    today = datetime.now(ZoneInfo("UTC")).date()
    for offset in range(90):
        await _add_completed_call(
            session,
            user.id,
            today - timedelta(days=offset),
            CallType.MORNING.value,
            confidence=OutcomeConfidence.CLEAR.value,
        )
    await session.commit()

    progress = await _progress_for_user(session, user)

    assert progress["streak"]["current"] == 90
    assert progress["streak"]["best"] == 90
    assert len(progress["heatmap"]) == 84


@pytest.mark.asyncio
async def test_connect_integration_can_return_authenticated_oauth_url(monkeypatch):
    """Frontend can use Bearer auth, then navigate to the returned OAuth URL."""
    user = User(id=42, phone="+14155550100", firebase_uid="firebase-uid")
    user_service = AsyncMock()
    user_service.get_by_phone = AsyncMock(return_value=user)

    async def fake_create_ephemeral_token(user_id: int, service: str) -> str:
        assert user_id == 42
        assert service == "gmail"
        return "ephemeral-token"

    monkeypatch.setattr(
        "app.api.dashboard.create_ephemeral_token", fake_create_ephemeral_token
    )
    monkeypatch.setattr(
        "app.api.dashboard.get_settings",
        lambda: SimpleNamespace(WEBHOOK_BASE_URL="https://api.example.test"),
    )

    result = await connect_integration(
        service="gmail",
        redirect=False,
        principal=FirebasePrincipal(uid="firebase-uid", phone_number="+14155550100"),
        user_service=user_service,
    )

    assert result == {
        "url": "https://api.example.test/auth/google/start?token=ephemeral-token"
    }


@pytest.mark.asyncio
async def test_dashboard_task_mutation_routes(session):
    user = await _create_user(session)
    principal = FirebasePrincipal(uid=user.firebase_uid or "uid", phone_number=user.phone)
    user_service = UserService(session)
    task_service = TaskService(session)

    task, _ = await task_service.save_task(
        user_id=user.id,
        title="File taxes",
        priority=50,
    )

    updated = await update_task(
        task.id,
        TaskUpdateRequest(title="File quarterly taxes", priority=90),
        principal=principal,
        user_service=user_service,
        task_service=task_service,
    )
    assert updated["task"]["title"] == "File quarterly taxes"
    assert updated["task"]["priority"] == 90

    snooze_until = datetime.now(timezone.utc) + timedelta(days=1)
    snoozed = await snooze_task(
        task.id,
        TaskSnoozeRequest(snooze_until=snooze_until),
        principal=principal,
        user_service=user_service,
        task_service=task_service,
    )
    assert snoozed["task"]["status"] == "snoozed"

    snoozed_list = await get_tasks(
        status="snoozed",
        principal=principal,
        user_service=user_service,
        task_service=task_service,
    )
    assert snoozed_list["tasks"][0]["id"] == task.id

    unsnoozed = await unsnooze_task(
        task.id,
        principal=principal,
        user_service=user_service,
        task_service=task_service,
    )
    assert unsnoozed["task"]["status"] == "pending"

    completed = await complete_task(
        task.id,
        principal=principal,
        user_service=user_service,
        task_service=task_service,
    )
    assert completed["task"]["status"] == "completed"

    deleted = await delete_task(
        task.id,
        principal=principal,
        user_service=user_service,
        task_service=task_service,
    )
    assert deleted["status"] == "deleted"
    assert deleted["task"]["id"] == task.id


@pytest.mark.asyncio
async def test_dashboard_goal_routes(session):
    user = await _create_user(session)
    principal = FirebasePrincipal(uid=user.firebase_uid or "uid", phone_number=user.phone)
    user_service = UserService(session)
    goal_service = GoalService(session)

    created = await create_goal(
        GoalCreateRequest(
            title="Prepare launch",
            description="Finish the launch checklist",
            target_date=date(2026, 5, 1),
        ),
        principal=principal,
        user_service=user_service,
        goal_service=goal_service,
    )
    goal_id = created["goal"]["id"]
    assert created["goal"]["title"] == "Prepare launch"

    listed = await get_goals(
        status=GoalStatus.ACTIVE.value,
        principal=principal,
        user_service=user_service,
        goal_service=goal_service,
    )
    assert [goal["id"] for goal in listed["goals"]] == [goal_id]

    updated = await update_goal(
        goal_id,
        GoalUpdateRequest(title="Prepare beta launch"),
        principal=principal,
        user_service=user_service,
        goal_service=goal_service,
    )
    assert updated["goal"]["title"] == "Prepare beta launch"

    completed = await complete_goal(
        goal_id,
        principal=principal,
        user_service=user_service,
        goal_service=goal_service,
    )
    assert completed["goal"]["status"] == GoalStatus.COMPLETED.value

    abandoned = await abandon_goal(
        goal_id,
        principal=principal,
        user_service=user_service,
        goal_service=goal_service,
    )
    assert abandoned["goal"]["status"] == GoalStatus.ABANDONED.value

    deleted = await delete_goal(
        goal_id,
        principal=principal,
        user_service=user_service,
        goal_service=goal_service,
    )
    assert deleted["status"] == "deleted"
    assert deleted["goal"]["id"] == goal_id


@pytest.mark.asyncio
async def test_dashboard_profile_call_window_and_call_history_routes(session):
    user = await _create_user(session)
    principal = FirebasePrincipal(uid=user.firebase_uid or "uid", phone_number=user.phone)
    user_service = UserService(session)
    cw_service = CallWindowService(session)

    profile = await update_user_profile(
        UserProfileUpdateRequest(
            name="Asha",
            timezone="America/New_York",
            urgent_email_calls_enabled=True,
            auto_task_from_emails_enabled=True,
            email_automation_quiet_hours_start=time(22, 0),
            email_automation_quiet_hours_end=time(7, 30),
        ),
        principal=principal,
        user_service=user_service,
    )
    assert profile["name"] == "Asha"
    assert profile["timezone"] == "America/New_York"
    assert profile["urgent_email_calls_enabled"] is True
    assert profile["auto_task_from_emails_enabled"] is True
    assert profile["email_automation_quiet_hours_start"] == "22:00"
    assert profile["email_automation_quiet_hours_end"] == "07:30"

    created_window = await create_call_window(
        CallWindowRequest(
            window_type="morning",
            start_time="08:00",
            end_time="08:30",
        ),
        principal=principal,
        user_service=user_service,
        cw_service=cw_service,
        session=session,
    )
    assert created_window["window"]["type"] == "morning"

    updated_window = await update_call_window(
        "morning",
        CallWindowUpdateRequest(start_time="08:15", end_time="08:45"),
        principal=principal,
        user_service=user_service,
        cw_service=cw_service,
        session=session,
    )
    assert updated_window["window"]["start_time"] == "08:15"

    call_date = datetime.now(timezone.utc).date()
    await _add_completed_call(
        session,
        user.id,
        call_date,
        CallType.MORNING.value,
        confidence=OutcomeConfidence.CLEAR.value,
    )
    await session.commit()

    history = await get_call_history(
        status=CallLogStatus.COMPLETED.value,
        call_type=CallType.MORNING.value,
        limit=5,
        principal=principal,
        user_service=user_service,
        session=session,
    )
    assert history["count"] == 1
    assert history["calls"][0]["call_type"] == CallType.MORNING.value

    removed = await delete_call_window(
        "morning",
        principal=principal,
        user_service=user_service,
        cw_service=cw_service,
        session=session,
    )
    assert removed["status"] == "removed"
