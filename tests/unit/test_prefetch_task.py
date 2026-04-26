"""Unit tests for voice context prefetch task."""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.models.call_log import CallLog
from app.models.enums import CallLogStatus, CallType, OccurrenceKind
from app.models.user import User
from app.tasks import prefetch


class SessionFactory:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


async def _user_and_call(session, *, status: str = CallLogStatus.DISPATCHING.value):
    user = User(phone="+15559990100", name="Test", timezone="UTC")
    session.add(user)
    await session.commit()
    await session.refresh(user)

    call = CallLog(
        user_id=user.id,
        call_type=CallType.MORNING.value,
        call_date=date.today(),
        scheduled_time=datetime.now(timezone.utc),
        scheduled_timezone="UTC",
        status=status,
        occurrence_kind=OccurrenceKind.PLANNED.value,
    )
    session.add(call)
    await session.commit()
    await session.refresh(call)
    return user, call


@pytest.mark.asyncio
async def test_prefetch_builds_and_stores_context(session, monkeypatch):
    _, call = await _user_and_call(session)
    prepare = AsyncMock(return_value=("instruction", {"opener": {"id": "a"}}))
    store = AsyncMock()
    monkeypatch.setattr(prefetch, "async_session_factory", SessionFactory(session))
    monkeypatch.setattr(prefetch, "prepare_call_context", prepare)
    monkeypatch.setattr(prefetch, "store_call_context", store)

    result = await prefetch._run_prefetch_call_context(call.id)

    assert result == f"Prefetched context for CallLog {call.id}"
    prepare.assert_awaited_once()
    store.assert_awaited_once_with(call.id, "instruction", {"opener": {"id": "a"}})


@pytest.mark.asyncio
async def test_prefetch_skips_terminal_call(session, monkeypatch):
    _, call = await _user_and_call(session, status=CallLogStatus.COMPLETED.value)
    prepare = AsyncMock()
    store = AsyncMock()
    monkeypatch.setattr(prefetch, "async_session_factory", SessionFactory(session))
    monkeypatch.setattr(prefetch, "prepare_call_context", prepare)
    monkeypatch.setattr(prefetch, "store_call_context", store)

    result = await prefetch._run_prefetch_call_context(call.id)

    assert "skipping context prefetch" in result
    prepare.assert_not_awaited()
    store.assert_not_awaited()
