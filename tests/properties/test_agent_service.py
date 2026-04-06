"""Property tests for AgentService session resolution (P6, P7, P8).

P6  — Session user_id is phone number: all created sessions have user_id
      matching E.164 format (starts with "+", digits only after).
      **Validates: Requirements 5.2**

P7  — Cross-channel session continuity: web then WhatsApp with same phone
      → same session_id resolved by AgentService.
      **Validates: Requirements 5.3**

P8  — Session persistence across restarts: create session with events,
      re-init DatabaseSessionService, retrieve session, assert events intact.
      **Validates: Requirements 5.4**

These tests use a real PostgreSQL test database and a real ADK
DatabaseSessionService.  The ADK Runner is mocked so we can test session
resolution without calling a live LLM.
"""

import asyncio
import os
import re
import string
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from dotenv import load_dotenv
from google.adk.events import Event
from google.adk.sessions import DatabaseSessionService
from google.genai.types import Content, Part
from hypothesis import given, settings, strategies as st, HealthCheck
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.current_session import CurrentSession
from app.services.agent_service import AgentService, APP_NAME

load_dotenv()

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://charu:CJbJ7PsFrpbb29xsMBm3pkH5@localhost:5432/charu_ai_test",
)

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_e164_phone = st.sampled_from(
    [
        "+14155552671",
        "+447911123456",
        "+971501234567",
        "+919876543210",
        "+61412345678",
        "+4915112345678",
        "+33612345678",
        "+818012345678",
    ]
)

_channel = st.sampled_from(["web", "whatsapp"])

_message_text = st.text(
    alphabet=string.ascii_letters + string.digits + " ",
    min_size=1,
    max_size=100,
).filter(lambda s: s.strip())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

E164_PATTERN = re.compile(r"^\+[1-9]\d{1,14}$")


def _run_async(coro):
    """Run an async coroutine in a new event loop (safe for Hypothesis @given)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _make_session():
    """Create engine, ensure SQLModel tables exist, return (engine, session)."""
    import app.models  # noqa: F401 — register all table classes

    eng = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    return eng, factory()


async def _cleanup_current_sessions(session: AsyncSession, phone: str):
    """Remove current_sessions mapping for the given phone."""
    await session.exec(
        sa_text("DELETE FROM current_sessions WHERE phone = :p"),
        params={"p": phone},
    )
    await session.commit()


def _make_mock_runner():
    """Create a mock ADK Runner whose run_async yields a single final event."""
    runner = MagicMock()

    async def _fake_run_async(**kwargs):
        event = MagicMock()
        event.is_final_response.return_value = True
        event.content = Content(parts=[Part(text="mock reply")])
        yield event

    runner.run_async = _fake_run_async
    return runner


# ---------------------------------------------------------------------------
# P6: Session user_id is phone number
# **Validates: Requirements 5.2**
# ---------------------------------------------------------------------------


@given(phone=_e164_phone, channel=_channel)
@settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow])
def test_session_user_id_is_e164_phone(phone, channel):
    """All ADK sessions created by AgentService have user_id in E.164 format."""

    async def _test():
        eng, db_session = await _make_session()
        adk_svc = DatabaseSessionService(db_url=TEST_DATABASE_URL)
        try:
            await _cleanup_current_sessions(db_session, phone)

            runner = _make_mock_runner()
            agent_svc = AgentService(runner, adk_svc, db_session)

            result = await agent_svc.run(
                user_id=phone,
                message="hello",
                channel=channel,
            )

            # The session should exist in ADK with user_id == phone
            adk_session = await adk_svc.get_session(
                app_name=APP_NAME,
                user_id=phone,
                session_id=result.session_id,
            )
            assert adk_session is not None, f"ADK session {result.session_id} not found"
            assert adk_session.user_id == phone
            assert E164_PATTERN.match(adk_session.user_id), (
                f"user_id '{adk_session.user_id}' is not valid E.164"
            )

            # Cleanup
            await adk_svc.delete_session(
                app_name=APP_NAME,
                user_id=phone,
                session_id=result.session_id,
            )
            await _cleanup_current_sessions(db_session, phone)
        finally:
            await db_session.close()
            await eng.dispose()

    _run_async(_test())


# ---------------------------------------------------------------------------
# P7: Cross-channel session continuity
# **Validates: Requirements 5.3**
# ---------------------------------------------------------------------------


@given(phone=_e164_phone)
@settings(max_examples=15, suppress_health_check=[HealthCheck.too_slow])
def test_cross_channel_session_continuity(phone):
    """Web then WhatsApp with same phone resolves to the same session_id."""

    async def _test():
        eng, db_session = await _make_session()
        adk_svc = DatabaseSessionService(db_url=TEST_DATABASE_URL)
        try:
            await _cleanup_current_sessions(db_session, phone)

            runner = _make_mock_runner()
            agent_svc = AgentService(runner, adk_svc, db_session)

            # First message via web channel
            result_web = await agent_svc.run(
                user_id=phone,
                message="hello from web",
                channel="web",
            )

            # Second message via whatsapp channel
            result_wa = await agent_svc.run(
                user_id=phone,
                message="hello from whatsapp",
                channel="whatsapp",
            )

            # Both should resolve to the same session
            assert result_web.session_id == result_wa.session_id, (
                f"Web session {result_web.session_id} != "
                f"WhatsApp session {result_wa.session_id}"
            )

            # Cleanup
            await adk_svc.delete_session(
                app_name=APP_NAME,
                user_id=phone,
                session_id=result_web.session_id,
            )
            await _cleanup_current_sessions(db_session, phone)
        finally:
            await db_session.close()
            await eng.dispose()

    _run_async(_test())


# ---------------------------------------------------------------------------
# P8: Session persistence across restarts
# **Validates: Requirements 5.4**
# ---------------------------------------------------------------------------


@given(phone=_e164_phone)
@settings(max_examples=10, suppress_health_check=[HealthCheck.too_slow])
def test_session_persistence_across_restarts(phone):
    """Session created with events survives DatabaseSessionService re-init."""

    async def _test():
        eng, db_session = await _make_session()
        adk_svc_1 = DatabaseSessionService(db_url=TEST_DATABASE_URL)
        try:
            await _cleanup_current_sessions(db_session, phone)

            runner = _make_mock_runner()
            agent_svc = AgentService(runner, adk_svc_1, db_session)

            # Create a session via AgentService
            result = await agent_svc.run(
                user_id=phone,
                message="remember this",
                channel="web",
            )
            session_id = result.session_id

            # Manually append a real event so we have something to verify
            adk_session = await adk_svc_1.get_session(
                app_name=APP_NAME,
                user_id=phone,
                session_id=session_id,
            )
            assert adk_session is not None

            user_event = Event(
                invocation_id=str(uuid.uuid4()),
                author="user",
                content=Content(parts=[Part(text="persisted message")]),
            )
            await adk_svc_1.append_event(session=adk_session, event=user_event)

            # Re-fetch and confirm at least 1 event exists before restart
            session_before = await adk_svc_1.get_session(
                app_name=APP_NAME,
                user_id=phone,
                session_id=session_id,
            )
            events_before_count = len(session_before.events)
            assert events_before_count >= 1, (
                f"Expected ≥1 events before restart, got {events_before_count}"
            )

            # --- Simulate restart: brand-new DatabaseSessionService ---
            adk_svc_2 = DatabaseSessionService(db_url=TEST_DATABASE_URL)

            session_after = await adk_svc_2.get_session(
                app_name=APP_NAME,
                user_id=phone,
                session_id=session_id,
            )

            assert session_after is not None, (
                f"Session {session_id} not found after service re-init"
            )
            assert session_after.id == session_id
            assert session_after.user_id == phone
            assert session_after.app_name == APP_NAME
            assert len(session_after.events) == events_before_count, (
                f"Expected {events_before_count} events, "
                f"got {len(session_after.events)}"
            )

            # Cleanup
            await adk_svc_2.delete_session(
                app_name=APP_NAME,
                user_id=phone,
                session_id=session_id,
            )
            await _cleanup_current_sessions(db_session, phone)
        finally:
            await db_session.close()
            await eng.dispose()

    _run_async(_test())
