"""Dashboard progress and integration-connect regression tests."""

from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

import app.api.dashboard as dashboard
from app.api.dashboard import connect_integration, get_progress
from app.models.call_log import CallLog
from app.models.call_window import CallWindow
from app.models.enums import (
    CallLogStatus,
    CallType,
    OccurrenceKind,
    OutcomeConfidence,
)
from app.models.schemas import FirebasePrincipal
from app.models.user import User
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
