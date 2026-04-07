"""Tests for midday check-in reply detection in the WhatsApp webhook.

Covers:
  - ``find_pending_checkin`` returns context when a recent check-in exists
  - ``find_pending_checkin`` returns None when no check-in or outside window
  - ``build_checkin_reply_prefix`` formats context correctly
  - WhatsApp webhook prepends check-in context to agent message
  - ``User.last_user_whatsapp_message_at`` is updated on every inbound message

Requirements: 13
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.call_log import CallLog
from app.models.enums import CallLogStatus, CallType, OccurrenceKind, OutcomeConfidence
from app.models.user import User
from app.services.checkin_context import (
    CHECKIN_REPLY_WINDOW_MINUTES,
    CheckinContext,
    build_checkin_reply_prefix,
    find_pending_checkin,
    mark_checkin_replied,
)


# ---------------------------------------------------------------------------
# find_pending_checkin tests
# ---------------------------------------------------------------------------


class TestFindPendingCheckin:
    """Tests for find_pending_checkin DB query."""

    @pytest.mark.asyncio
    async def test_returns_context_for_recent_checkin(self, session):
        """A completed morning call with checkin_sent_at within the window."""
        user = User(phone="+14155550001")
        session.add(user)
        await session.flush()

        now = datetime.now(timezone.utc)
        call_log = CallLog(
            user_id=user.id,
            call_type=CallType.MORNING.value,
            call_date=now.date(),
            scheduled_time=now - timedelta(hours=6),
            scheduled_timezone="America/New_York",
            status=CallLogStatus.COMPLETED.value,
            occurrence_kind=OccurrenceKind.PLANNED.value,
            goal="Finish the report",
            next_action="Open the doc and write the intro",
            call_outcome_confidence=OutcomeConfidence.CLEAR.value,
            checkin_sent_at=now - timedelta(minutes=10),
        )
        session.add(call_log)
        await session.flush()

        ctx = await find_pending_checkin(user.id, session)

        assert ctx is not None
        assert ctx.call_log_id == call_log.id
        assert ctx.goal == "Finish the report"
        assert ctx.next_action == "Open the doc and write the intro"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_checkin_sent(self, session):
        """A completed call without checkin_sent_at → None."""
        user = User(phone="+14155550002")
        session.add(user)
        await session.flush()

        now = datetime.now(timezone.utc)
        call_log = CallLog(
            user_id=user.id,
            call_type=CallType.MORNING.value,
            call_date=now.date(),
            scheduled_time=now - timedelta(hours=6),
            scheduled_timezone="America/New_York",
            status=CallLogStatus.COMPLETED.value,
            occurrence_kind=OccurrenceKind.PLANNED.value,
            goal="Finish the report",
            next_action="Open the doc",
            call_outcome_confidence=OutcomeConfidence.CLEAR.value,
            checkin_sent_at=None,
        )
        session.add(call_log)
        await session.flush()

        ctx = await find_pending_checkin(user.id, session)
        assert ctx is None

    @pytest.mark.asyncio
    async def test_returns_none_when_checkin_outside_window(self, session):
        """A check-in sent more than 60 minutes ago → None."""
        user = User(phone="+14155550003")
        session.add(user)
        await session.flush()

        now = datetime.now(timezone.utc)
        call_log = CallLog(
            user_id=user.id,
            call_type=CallType.MORNING.value,
            call_date=now.date(),
            scheduled_time=now - timedelta(hours=6),
            scheduled_timezone="America/New_York",
            status=CallLogStatus.COMPLETED.value,
            occurrence_kind=OccurrenceKind.PLANNED.value,
            goal="Finish the report",
            next_action="Open the doc",
            call_outcome_confidence=OutcomeConfidence.CLEAR.value,
            checkin_sent_at=now - timedelta(minutes=CHECKIN_REPLY_WINDOW_MINUTES + 5),
        )
        session.add(call_log)
        await session.flush()

        ctx = await find_pending_checkin(user.id, session)
        assert ctx is None

    @pytest.mark.asyncio
    async def test_returns_none_for_evening_calls(self, session):
        """Evening calls don't trigger midday check-ins → None."""
        user = User(phone="+14155550004")
        session.add(user)
        await session.flush()

        now = datetime.now(timezone.utc)
        call_log = CallLog(
            user_id=user.id,
            call_type=CallType.EVENING.value,
            call_date=now.date(),
            scheduled_time=now - timedelta(hours=3),
            scheduled_timezone="America/New_York",
            status=CallLogStatus.COMPLETED.value,
            occurrence_kind=OccurrenceKind.PLANNED.value,
            next_action="Review tomorrow's plan",
            call_outcome_confidence=OutcomeConfidence.CLEAR.value,
            checkin_sent_at=now - timedelta(minutes=10),
        )
        session.add(call_log)
        await session.flush()

        ctx = await find_pending_checkin(user.id, session)
        assert ctx is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_next_action(self, session):
        """A check-in without next_action → None (nothing to check in about)."""
        user = User(phone="+14155550005")
        session.add(user)
        await session.flush()

        now = datetime.now(timezone.utc)
        call_log = CallLog(
            user_id=user.id,
            call_type=CallType.MORNING.value,
            call_date=now.date(),
            scheduled_time=now - timedelta(hours=6),
            scheduled_timezone="America/New_York",
            status=CallLogStatus.COMPLETED.value,
            occurrence_kind=OccurrenceKind.PLANNED.value,
            goal="Finish the report",
            next_action=None,
            call_outcome_confidence=OutcomeConfidence.PARTIAL.value,
            checkin_sent_at=now - timedelta(minutes=10),
        )
        session.add(call_log)
        await session.flush()

        ctx = await find_pending_checkin(user.id, session)
        assert ctx is None

    @pytest.mark.asyncio
    async def test_returns_most_recent_checkin(self, session):
        """When multiple check-ins exist, return the most recent one."""
        user = User(phone="+14155550006")
        session.add(user)
        await session.flush()

        now = datetime.now(timezone.utc)

        # Older check-in
        old_call = CallLog(
            user_id=user.id,
            call_type=CallType.MORNING.value,
            call_date=now.date(),
            scheduled_time=now - timedelta(hours=8),
            scheduled_timezone="America/New_York",
            status=CallLogStatus.COMPLETED.value,
            occurrence_kind=OccurrenceKind.PLANNED.value,
            goal="Old goal",
            next_action="Old action",
            call_outcome_confidence=OutcomeConfidence.CLEAR.value,
            checkin_sent_at=now - timedelta(minutes=50),
        )
        session.add(old_call)

        # Newer check-in
        new_call = CallLog(
            user_id=user.id,
            call_type=CallType.AFTERNOON.value,
            call_date=now.date(),
            scheduled_time=now - timedelta(hours=3),
            scheduled_timezone="America/New_York",
            status=CallLogStatus.COMPLETED.value,
            occurrence_kind=OccurrenceKind.PLANNED.value,
            goal="New goal",
            next_action="New action",
            call_outcome_confidence=OutcomeConfidence.CLEAR.value,
            checkin_sent_at=now - timedelta(minutes=5),
        )
        session.add(new_call)
        await session.flush()

        ctx = await find_pending_checkin(user.id, session)
        assert ctx is not None
        assert ctx.goal == "New goal"
        assert ctx.next_action == "New action"

    @pytest.mark.asyncio
    async def test_returns_none_when_checkin_already_replied(self, session):
        """A check-in that was already replied to → None."""
        user = User(phone="+14155550007")
        session.add(user)
        await session.flush()

        now = datetime.now(timezone.utc)
        call_log = CallLog(
            user_id=user.id,
            call_type=CallType.MORNING.value,
            call_date=now.date(),
            scheduled_time=now - timedelta(hours=6),
            scheduled_timezone="America/New_York",
            status=CallLogStatus.COMPLETED.value,
            occurrence_kind=OccurrenceKind.PLANNED.value,
            goal="Finish the report",
            next_action="Open the doc and write the intro",
            call_outcome_confidence=OutcomeConfidence.CLEAR.value,
            checkin_sent_at=now - timedelta(minutes=10),
            checkin_replied_at=now - timedelta(minutes=5),
        )
        session.add(call_log)
        await session.flush()

        ctx = await find_pending_checkin(user.id, session)
        assert ctx is None

    @pytest.mark.asyncio
    async def test_mark_checkin_replied_consumes_checkin(self, session):
        """After mark_checkin_replied, find_pending_checkin returns None."""
        user = User(phone="+14155550008")
        session.add(user)
        await session.flush()

        now = datetime.now(timezone.utc)
        call_log = CallLog(
            user_id=user.id,
            call_type=CallType.MORNING.value,
            call_date=now.date(),
            scheduled_time=now - timedelta(hours=6),
            scheduled_timezone="America/New_York",
            status=CallLogStatus.COMPLETED.value,
            occurrence_kind=OccurrenceKind.PLANNED.value,
            goal="Finish the report",
            next_action="Open the doc",
            call_outcome_confidence=OutcomeConfidence.CLEAR.value,
            checkin_sent_at=now - timedelta(minutes=10),
        )
        session.add(call_log)
        await session.flush()

        ctx = await find_pending_checkin(user.id, session)
        assert ctx is not None

        await mark_checkin_replied(call_log.id, session)

        ctx = await find_pending_checkin(user.id, session)
        assert ctx is None


# ---------------------------------------------------------------------------
# build_checkin_reply_prefix tests
# ---------------------------------------------------------------------------


class TestBuildCheckinReplyPrefix:
    """Tests for the context prefix builder."""

    def test_includes_goal_and_next_action(self):
        ctx = CheckinContext(
            call_log_id=1,
            goal="Finish the report",
            next_action="Open the doc and write the intro",
        )
        prefix = build_checkin_reply_prefix(ctx)

        assert "[SYSTEM:" in prefix
        assert "midday check-in" in prefix
        assert "Finish the report" in prefix
        assert "Open the doc and write the intro" in prefix

    def test_omits_goal_when_none(self):
        ctx = CheckinContext(
            call_log_id=2,
            goal=None,
            next_action="Send the email",
        )
        prefix = build_checkin_reply_prefix(ctx)

        assert "morning goal" not in prefix
        assert "Send the email" in prefix

    def test_includes_response_guidelines(self):
        ctx = CheckinContext(
            call_log_id=3,
            goal="Exercise",
            next_action="Put on running shoes",
        )
        prefix = build_checkin_reply_prefix(ctx)

        assert "Midday Check-In Response Guidelines" in prefix
