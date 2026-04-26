"""Property tests for OutboundMessage dedup send flow.

# Feature: accountability-call-onboarding, Property 36: At-most-once outbound message via dedup key

Tests verify:
- First send with a dedup key succeeds and creates an OutboundMessage row
- Second send with the same dedup key is silently skipped (no Twilio call)
- Ambiguous failure (exception) marks as failed to prevent duplicate delivery
- Ambiguous failure blocks subsequent retries (at-most-once)
- Stale pending claims from crashed workers are reclaimed after TTL
- Partial sends (WhatsAppPartialSendError) are marked sent to prevent duplication
- Free-form zero-chunks-sent releases claim for retry
- Dedup key builders produce unique, deterministic keys
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from hypothesis import given, settings, strategies as st, HealthCheck
from sqlalchemy import text
from sqlmodel import select

from app.models.enums import OutboundMessageStatus
from app.models.outbound_message import OutboundMessage
from app.models.user import User
from app.services.outbound_message_service import (
    OutboundMessageService,
    checkin_dedup_key,
    draft_review_dedup_key,
    evening_recap_dedup_key,
    missed_call_dedup_key,
    recap_dedup_key,
    weekly_summary_dedup_key,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_wa_service(*, template_sid: str = "SM_test_sid") -> MagicMock:
    """Create a mock WhatsAppService that returns a predictable SID."""
    wa = MagicMock()
    wa.send_template_message = AsyncMock(return_value=template_sid)
    wa.send_reply = AsyncMock(return_value=[template_sid])
    return wa


def _make_failing_wa_service() -> MagicMock:
    """Create a mock WhatsAppService that always fails."""
    wa = MagicMock()
    wa.send_template_message = AsyncMock(return_value=None)
    wa.send_reply = AsyncMock(return_value=[])
    return wa


async def _ensure_user(session, phone: str = "+14155550001") -> User:
    """Create a test user if not exists."""
    result = await session.exec(select(User).where(User.phone == phone))
    user = result.one_or_none()
    if user:
        return user
    user = User(phone=phone, name="Test User")
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Property 36: At-most-once outbound message via dedup key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_template_dedup_first_send_succeeds(session):
    """First send with a dedup key should succeed and create a sent row."""
    user = await _ensure_user(session)
    wa = _make_wa_service(template_sid="SM_abc123")
    svc = OutboundMessageService(session=session, whatsapp_service=wa)

    sid = await svc.send_template_dedup(
        user_id=user.id,
        dedup_key="recap:100",
        to=user.phone,
        content_sid="HX_template",
        content_variables={"1": "hello"},
    )

    assert sid == "SM_abc123"
    wa.send_template_message.assert_awaited_once()

    # Verify DB row
    result = await session.exec(
        select(OutboundMessage).where(OutboundMessage.dedup_key == "recap:100")
    )
    row = result.one()
    assert row.status == OutboundMessageStatus.SENT.value
    assert row.twilio_message_sid == "SM_abc123"
    assert row.sent_at is not None


@pytest.mark.asyncio
async def test_template_dedup_second_send_skipped(session):
    """Second send with the same dedup key should be silently skipped."""
    user = await _ensure_user(session)
    wa = _make_wa_service(template_sid="SM_first")
    svc = OutboundMessageService(session=session, whatsapp_service=wa)

    # First send
    sid1 = await svc.send_template_dedup(
        user_id=user.id,
        dedup_key="recap:200",
        to=user.phone,
        content_sid="HX_template",
    )
    assert sid1 == "SM_first"

    # Second send — should be skipped
    wa.send_template_message.reset_mock()
    sid2 = await svc.send_template_dedup(
        user_id=user.id,
        dedup_key="recap:200",
        to=user.phone,
        content_sid="HX_template",
    )
    assert sid2 is None
    wa.send_template_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_template_dedup_exception_marks_failed(session):
    """When send_template_message raises (ambiguous — Twilio may have
    accepted), the row is marked failed to prevent duplicate delivery.
    This is the real production path: WhatsAppService no longer catches
    exceptions, so they propagate to the dedup layer."""
    user = await _ensure_user(session)
    wa = MagicMock()
    wa.send_template_message = AsyncMock(side_effect=RuntimeError("timeout"))
    svc = OutboundMessageService(session=session, whatsapp_service=wa)

    sid = await svc.send_template_dedup(
        user_id=user.id,
        dedup_key="recap:301",
        to=user.phone,
        content_sid="HX_template",
    )

    assert sid is None

    # Row should exist with failed status — NOT deleted
    result = await session.exec(
        select(OutboundMessage).where(OutboundMessage.dedup_key == "recap:301")
    )
    row = result.one()
    assert row.status == OutboundMessageStatus.FAILED.value


@pytest.mark.asyncio
async def test_template_dedup_exception_blocks_retry(session):
    """After an ambiguous exception marks the row failed, a retry should
    be blocked (dedup hit) to preserve at-most-once semantics."""
    user = await _ensure_user(session)

    # First attempt: exception (ambiguous)
    wa_exc = MagicMock()
    wa_exc.send_template_message = AsyncMock(side_effect=RuntimeError("timeout"))
    svc1 = OutboundMessageService(session=session, whatsapp_service=wa_exc)
    await svc1.send_template_dedup(
        user_id=user.id,
        dedup_key="recap:302",
        to=user.phone,
        content_sid="HX_template",
    )

    # Second attempt: should be blocked
    wa_ok = _make_wa_service(template_sid="SM_should_not_send")
    svc2 = OutboundMessageService(session=session, whatsapp_service=wa_ok)
    sid2 = await svc2.send_template_dedup(
        user_id=user.id,
        dedup_key="recap:302",
        to=user.phone,
        content_sid="HX_template",
    )
    assert sid2 is None
    wa_ok.send_template_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# Definitive rejection (4xx) releases claim for retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_template_dedup_definitive_rejection_releases_claim(session):
    """A definitive Twilio rejection (4xx) should release the claim so a
    retry with corrected config can succeed — not permanently burn the key."""
    from twilio.base.exceptions import TwilioRestException

    user = await _ensure_user(session)
    wa = MagicMock()
    wa.send_template_message = AsyncMock(
        side_effect=TwilioRestException(
            status=400, uri="/Messages", msg="Invalid Content SID"
        )
    )
    svc = OutboundMessageService(session=session, whatsapp_service=wa)

    sid = await svc.send_template_dedup(
        user_id=user.id,
        dedup_key="recap:500",
        to=user.phone,
        content_sid="HX_bad_template",
    )
    assert sid is None

    # Row should be deleted (claim released), NOT marked failed
    result = await session.exec(
        select(OutboundMessage).where(OutboundMessage.dedup_key == "recap:500")
    )
    assert result.one_or_none() is None


@pytest.mark.asyncio
async def test_template_dedup_definitive_rejection_allows_retry(session):
    """After a 4xx releases the claim, a retry with corrected config succeeds."""
    from twilio.base.exceptions import TwilioRestException

    user = await _ensure_user(session)

    # First attempt: 400 (bad template)
    wa_bad = MagicMock()
    wa_bad.send_template_message = AsyncMock(
        side_effect=TwilioRestException(
            status=400, uri="/Messages", msg="Invalid Content SID"
        )
    )
    svc1 = OutboundMessageService(session=session, whatsapp_service=wa_bad)
    await svc1.send_template_dedup(
        user_id=user.id,
        dedup_key="recap:501",
        to=user.phone,
        content_sid="HX_bad",
    )

    # Second attempt with corrected template: should succeed
    wa_ok = _make_wa_service(template_sid="SM_fixed")
    svc2 = OutboundMessageService(session=session, whatsapp_service=wa_ok)
    sid2 = await svc2.send_template_dedup(
        user_id=user.id,
        dedup_key="recap:501",
        to=user.phone,
        content_sid="HX_good",
    )
    assert sid2 == "SM_fixed"
    wa_ok.send_template_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_template_dedup_5xx_still_marks_failed(session):
    """A 5xx (ambiguous) should still mark as failed — not release the claim."""
    from twilio.base.exceptions import TwilioRestException

    user = await _ensure_user(session)
    wa = MagicMock()
    wa.send_template_message = AsyncMock(
        side_effect=TwilioRestException(
            status=500, uri="/Messages", msg="Internal Server Error"
        )
    )
    svc = OutboundMessageService(session=session, whatsapp_service=wa)

    sid = await svc.send_template_dedup(
        user_id=user.id,
        dedup_key="recap:502",
        to=user.phone,
        content_sid="HX_template",
    )
    assert sid is None

    # Row should exist with failed status — NOT deleted
    result = await session.exec(
        select(OutboundMessage).where(OutboundMessage.dedup_key == "recap:502")
    )
    row = result.one()
    assert row.status == OutboundMessageStatus.FAILED.value


# ---------------------------------------------------------------------------
# Stale claim reclaim (Finding 2: crashed workers)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_pending_claim_is_reclaimed(session):
    """A pending row older than CLAIM_TTL_SECONDS is reclaimed by a new worker."""
    from app.services.outbound_message_service import CLAIM_TTL_SECONDS

    user = await _ensure_user(session)

    # Manually insert a stale pending row (created_at in the past)
    stale_time = datetime.now(timezone.utc) - timedelta(seconds=CLAIM_TTL_SECONDS + 60)
    await session.execute(
        text(
            "INSERT INTO outbound_messages (user_id, dedup_key, status, created_at) "
            "VALUES (:uid, :key, :status, :ts)"
        ),
        {
            "uid": user.id,
            "key": "recap:400",
            "status": OutboundMessageStatus.PENDING.value,
            "ts": stale_time,
        },
    )
    await session.commit()

    # New worker should reclaim the stale row and succeed
    wa = _make_wa_service(template_sid="SM_reclaimed")
    svc = OutboundMessageService(session=session, whatsapp_service=wa)
    sid = await svc.send_template_dedup(
        user_id=user.id,
        dedup_key="recap:400",
        to=user.phone,
        content_sid="HX_template",
    )

    assert sid == "SM_reclaimed"
    wa.send_template_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_fresh_pending_claim_is_not_reclaimed(session):
    """A pending row younger than CLAIM_TTL_SECONDS is NOT reclaimed."""
    user = await _ensure_user(session)

    # Manually insert a fresh pending row
    fresh_time = datetime.now(timezone.utc) - timedelta(seconds=10)
    await session.execute(
        text(
            "INSERT INTO outbound_messages (user_id, dedup_key, status, created_at) "
            "VALUES (:uid, :key, :status, :ts)"
        ),
        {
            "uid": user.id,
            "key": "recap:401",
            "status": OutboundMessageStatus.PENDING.value,
            "ts": fresh_time,
        },
    )
    await session.commit()

    # New worker should NOT reclaim — dedup hit
    wa = _make_wa_service(template_sid="SM_should_not_send")
    svc = OutboundMessageService(session=session, whatsapp_service=wa)
    sid = await svc.send_template_dedup(
        user_id=user.id,
        dedup_key="recap:401",
        to=user.phone,
        content_sid="HX_template",
    )

    assert sid is None
    wa.send_template_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_sending_claim_is_reclaimed(session):
    """A sending row older than CLAIM_TTL_SECONDS is reclaimed by a new worker."""
    from app.services.outbound_message_service import CLAIM_TTL_SECONDS

    user = await _ensure_user(session)
    stale_time = datetime.now(timezone.utc) - timedelta(seconds=CLAIM_TTL_SECONDS + 60)
    await session.execute(
        text(
            "INSERT INTO outbound_messages "
            "(user_id, dedup_key, status, created_at, claim_token) "
            "VALUES (:uid, :key, :status, :ts, :token)"
        ),
        {
            "uid": user.id,
            "key": "recap:402",
            "status": OutboundMessageStatus.SENDING.value,
            "ts": stale_time,
            "token": "old-token",
        },
    )
    await session.commit()

    wa = _make_wa_service(template_sid="SM_reclaimed_sending")
    svc = OutboundMessageService(session=session, whatsapp_service=wa)
    sid = await svc.send_template_dedup(
        user_id=user.id,
        dedup_key="recap:402",
        to=user.phone,
        content_sid="HX_template",
    )

    assert sid == "SM_reclaimed_sending"
    wa.send_template_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_fresh_sending_claim_is_not_reclaimed(session):
    """A fresh sending row is protected from duplicate Twilio sends."""
    user = await _ensure_user(session)
    fresh_time = datetime.now(timezone.utc) - timedelta(seconds=10)
    await session.execute(
        text(
            "INSERT INTO outbound_messages "
            "(user_id, dedup_key, status, created_at, claim_token) "
            "VALUES (:uid, :key, :status, :ts, :token)"
        ),
        {
            "uid": user.id,
            "key": "recap:403",
            "status": OutboundMessageStatus.SENDING.value,
            "ts": fresh_time,
            "token": "active-token",
        },
    )
    await session.commit()

    wa = _make_wa_service(template_sid="SM_should_not_send")
    svc = OutboundMessageService(session=session, whatsapp_service=wa)
    sid = await svc.send_template_dedup(
        user_id=user.id,
        dedup_key="recap:403",
        to=user.phone,
        content_sid="HX_template",
    )

    assert sid is None
    wa.send_template_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_token_cannot_mark_sent(session):
    """A worker with an old claim token cannot mutate a reclaimed row."""
    user = await _ensure_user(session)
    await session.execute(
        text(
            "INSERT INTO outbound_messages "
            "(user_id, dedup_key, status, claim_token) "
            "VALUES (:uid, :key, :status, :token)"
        ),
        {
            "uid": user.id,
            "key": "recap:404",
            "status": OutboundMessageStatus.SENDING.value,
            "token": "fresh-token",
        },
    )
    await session.commit()

    svc = OutboundMessageService(session=session, whatsapp_service=_make_wa_service())
    marked = await svc._mark_sent(
        dedup_key="recap:404",
        twilio_message_sid="SM_stale",
        token="stale-token",
    )

    assert marked is False
    result = await session.exec(
        select(OutboundMessage).where(OutboundMessage.dedup_key == "recap:404")
    )
    row = result.one()
    assert row.status == OutboundMessageStatus.SENDING.value
    assert row.twilio_message_sid is None


# ---------------------------------------------------------------------------
# Partial send (WhatsAppPartialSendError)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_freeform_dedup_partial_send_marks_sent(session):
    """When some chunks are delivered but a later chunk fails, the dedup
    record is marked sent (not released) to prevent duplicate delivery
    of the already-sent chunks."""
    from app.services.whatsapp_service import WhatsAppPartialSendError

    user = await _ensure_user(session)
    wa = MagicMock()
    wa.send_reply = AsyncMock(
        side_effect=WhatsAppPartialSendError(
            sent_sids=["SM_chunk1"],
            total_chunks=3,
            cause=RuntimeError("chunk 2 failed"),
        )
    )
    svc = OutboundMessageService(session=session, whatsapp_service=wa)

    sids = await svc.send_freeform_dedup(
        user_id=user.id,
        dedup_key="draft_overflow:70",
        to=user.phone,
        body="Long text that would be split...",
    )

    # Should return the partial SIDs
    assert sids == ["SM_chunk1"]

    # Row should be marked sent — NOT released
    result = await session.exec(
        select(OutboundMessage).where(
            OutboundMessage.dedup_key == "draft_overflow:70"
        )
    )
    row = result.one()
    assert row.status == OutboundMessageStatus.SENT.value
    assert row.twilio_message_sid == "SM_chunk1"


@pytest.mark.asyncio
async def test_freeform_dedup_partial_send_blocks_retry(session):
    """After a partial send is marked sent, a retry is blocked."""
    from app.services.whatsapp_service import WhatsAppPartialSendError

    user = await _ensure_user(session)
    wa = MagicMock()
    wa.send_reply = AsyncMock(
        side_effect=WhatsAppPartialSendError(
            sent_sids=["SM_partial"],
            total_chunks=2,
            cause=RuntimeError("chunk 2 failed"),
        )
    )
    svc = OutboundMessageService(session=session, whatsapp_service=wa)

    await svc.send_freeform_dedup(
        user_id=user.id,
        dedup_key="draft_overflow:71",
        to=user.phone,
        body="Text",
    )

    # Retry should be blocked
    wa2 = _make_wa_service()
    svc2 = OutboundMessageService(session=session, whatsapp_service=wa2)
    result = await svc2.send_freeform_dedup(
        user_id=user.id,
        dedup_key="draft_overflow:71",
        to=user.phone,
        body="Text",
    )
    assert result is None
    wa2.send_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_freeform_dedup_first_send_succeeds(session):
    """Free-form dedup send should work on first attempt."""
    user = await _ensure_user(session)
    wa = _make_wa_service(template_sid="SM_free1")
    svc = OutboundMessageService(session=session, whatsapp_service=wa)

    sids = await svc.send_freeform_dedup(
        user_id=user.id,
        dedup_key="draft_overflow:50",
        to=user.phone,
        body="Full draft text here...",
    )

    assert sids == ["SM_free1"]
    wa.send_reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_freeform_dedup_zero_chunks_releases_claim(session):
    """When send_reply returns an empty list (no exception), the claim is
    released so a retry can re-attempt.  This is the definitive non-delivery
    path — only used for free-form sends."""
    user = await _ensure_user(session)
    wa = MagicMock()
    wa.send_reply = AsyncMock(return_value=[])  # zero chunks sent, no exception
    svc = OutboundMessageService(session=session, whatsapp_service=wa)

    result = await svc.send_freeform_dedup(
        user_id=user.id,
        dedup_key="draft_overflow:80",
        to=user.phone,
        body="Some text",
    )
    assert result is None

    # Claim should be released — no row
    db_result = await session.exec(
        select(OutboundMessage).where(
            OutboundMessage.dedup_key == "draft_overflow:80"
        )
    )
    assert db_result.one_or_none() is None

    # A retry should now succeed
    wa2 = _make_wa_service(template_sid="SM_retry_ok")
    svc2 = OutboundMessageService(session=session, whatsapp_service=wa2)
    sids = await svc2.send_freeform_dedup(
        user_id=user.id,
        dedup_key="draft_overflow:80",
        to=user.phone,
        body="Some text",
    )
    assert sids == ["SM_retry_ok"]


@pytest.mark.asyncio
async def test_freeform_dedup_second_send_skipped(session):
    """Second free-form send with same key should be skipped."""
    user = await _ensure_user(session)
    wa = _make_wa_service()
    svc = OutboundMessageService(session=session, whatsapp_service=wa)

    await svc.send_freeform_dedup(
        user_id=user.id,
        dedup_key="draft_overflow:60",
        to=user.phone,
        body="First send",
    )

    wa.send_reply.reset_mock()
    result = await svc.send_freeform_dedup(
        user_id=user.id,
        dedup_key="draft_overflow:60",
        to=user.phone,
        body="Duplicate send",
    )
    assert result is None
    wa.send_reply.assert_not_awaited()


# ---------------------------------------------------------------------------
# Dedup key builder tests (pure functions, no DB needed)
# ---------------------------------------------------------------------------


@given(call_log_id=st.integers(min_value=1, max_value=10**9))
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_recap_dedup_key_deterministic(call_log_id: int):
    """Same call_log_id always produces the same recap dedup key."""
    assert recap_dedup_key(call_log_id) == recap_dedup_key(call_log_id)
    assert recap_dedup_key(call_log_id) == f"recap:{call_log_id}"


@given(call_log_id=st.integers(min_value=1, max_value=10**9))
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_dedup_keys_are_distinct_across_types(call_log_id: int):
    """Different message types produce different dedup keys for the same ID."""
    keys = {
        recap_dedup_key(call_log_id),
        evening_recap_dedup_key(call_log_id),
        checkin_dedup_key(call_log_id),
        missed_call_dedup_key(call_log_id),
    }
    assert len(keys) == 4, f"Dedup keys collided for call_log_id={call_log_id}"


@given(
    user_id=st.integers(min_value=1, max_value=10**6),
    week=st.integers(min_value=1, max_value=53),
    year=st.integers(min_value=2024, max_value=2030),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_weekly_summary_dedup_key_deterministic(user_id: int, week: int, year: int):
    """Weekly summary dedup key is deterministic and includes user + week."""
    iso_week = f"{year}-W{week:02d}"
    key = weekly_summary_dedup_key(user_id, iso_week)
    assert key == weekly_summary_dedup_key(user_id, iso_week)
    assert str(user_id) in key
    assert iso_week in key


@given(draft_id=st.integers(min_value=1, max_value=10**9))
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_draft_review_dedup_key_deterministic(draft_id: int):
    """Draft review dedup key is deterministic."""
    assert draft_review_dedup_key(draft_id) == f"draft_review:{draft_id}"
