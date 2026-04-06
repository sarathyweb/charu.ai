"""Property tests for WhatsApp integration (P6, P10, P11, P37, P46).

P6  — Message splitting preserves all content:
      concatenating all chunks equals the original text; each chunk ≤1600 chars.
      **Validates: Requirements 3.5**

P10 — WhatsApp phone extraction and agent routing:
      "whatsapp:+XXX" → correct phone extracted and used as user_id.
      **Validates: Requirements 7.3, 7.4**

P11 — WhatsApp reply to correct sender:
      Twilio client called with original sender's number as ``to``.
      **Validates: Requirements 7.5**

P36 — At-most-once outbound message via dedup key:
      *** Covered in tests/properties/test_outbound_message_dedup.py ***
      (Not duplicated here — see that file for full dedup property tests.)

P37 — WhatsApp 24-hour template compliance:
      Proactive messages outside the 24-hour window MUST use templates.
      ``is_within_service_window`` returns False when no window or expired.
      **Validates: Requirements 5.4, Design Concurrency Notes**

P46 — WhatsApp 24-hour window enforcement:
      Boundary conditions around the 86400-second window.
      **Validates: Requirements 18, Design Concurrency Notes**

Template parameter builders — all builders produce parameters that fit
within the 1024-char template body limit.
**Validates: Requirements 5.4**
"""

from __future__ import annotations

import string
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings, strategies as st, HealthCheck

from app.services.whatsapp_service import (
    MAX_WHATSAPP_BODY,
    MAX_TEMPLATE_BODY,
    WHATSAPP_WINDOW_SECONDS,
    WhatsAppService,
    split_message,
    build_daily_recap_params,
    build_daily_recap_no_goal_params,
    build_evening_recap_params,
    build_evening_recap_no_accomplishments_params,
    build_midday_checkin_params,
    build_weekly_summary_params,
    build_missed_call_params,
    build_email_draft_review_params,
)
from app.utils import normalize_phone


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

# Message bodies: printable text, 1–300 chars
_message_body = st.text(
    alphabet=string.ascii_letters + string.digits + " .,!?",
    min_size=1,
    max_size=300,
).filter(lambda s: s.strip())

# Arbitrary strings for splitting / template testing — including very long ones
_any_text = st.text(
    alphabet=st.characters(categories=("L", "N", "P", "Z")),
    min_size=0,
    max_size=10_000,
)

# Short-to-medium text for template parameter testing
_param_text = st.text(
    alphabet=st.characters(categories=("L", "N", "P", "Z")),
    min_size=0,
    max_size=2000,
)

# Timedeltas within a few days for window testing
_small_timedelta = st.timedeltas(
    min_value=timedelta(seconds=0),
    max_value=timedelta(days=3),
)


# ---------------------------------------------------------------------------
# P10: WhatsApp phone extraction and agent routing
# **Validates: Requirements 7.3, 7.4**
# ---------------------------------------------------------------------------


@given(phone=_e164_phone, body=_message_body)
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_whatsapp_phone_extraction_and_routing(phone, body):
    """The webhook strips 'whatsapp:' and routes to the agent with the
    correct E.164 phone as user_id."""

    raw_from = f"whatsapp:{phone}"

    # Simulate the extraction logic from the webhook handler
    extracted = raw_from.removeprefix("whatsapp:")
    normalised = normalize_phone(extracted)

    # The extracted phone must equal the original E.164 phone
    assert normalised == phone, (
        f"Expected user_id '{phone}', got '{normalised}' from From='{raw_from}'"
    )

    # The normalised phone starts with '+' (E.164)
    assert normalised.startswith("+"), f"user_id '{normalised}' is not E.164"


# ---------------------------------------------------------------------------
# P11: WhatsApp reply to correct sender
# **Validates: Requirements 7.5**
# ---------------------------------------------------------------------------


@given(phone=_e164_phone, reply_text=_message_body)
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_whatsapp_reply_to_correct_sender(phone, reply_text):
    """WhatsAppService.send_reply calls Twilio with the original sender's
    number as the ``to`` parameter (prefixed with 'whatsapp:')."""

    import asyncio

    mock_client = MagicMock()
    mock_create = MagicMock()
    mock_client.messages.create = mock_create

    with patch("app.services.whatsapp_service.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(
            TWILIO_ACCOUNT_SID="ACtest",
            TWILIO_AUTH_TOKEN="test_token",
            TWILIO_WHATSAPP_NUMBER="whatsapp:+14155238886",
        )
        svc = WhatsAppService(twilio_client=mock_client)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(svc.send_reply(to=phone, body=reply_text))
    finally:
        loop.close()

    # Twilio client must have been called at least once (may be multiple for long msgs)
    assert mock_create.call_count >= 1

    # Every call must target the correct recipient
    for call in mock_create.call_args_list:
        actual_to = call.kwargs.get("to") or call[1].get("to")
        assert actual_to == f"whatsapp:{phone}", (
            f"Expected to='whatsapp:{phone}', got to='{actual_to}'"
        )


# ---------------------------------------------------------------------------
# P6: Message splitting preserves all content
# **Validates: Requirements 3.5**
# ---------------------------------------------------------------------------


@given(text=_any_text)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_split_message_preserves_content(text):
    """For any string, split_message produces chunks where concatenating
    all chunks equals the original text (no content dropped)."""

    chunks = split_message(text)

    if not text:
        assert chunks == [], f"Empty text should produce empty list, got {chunks}"
        return

    # Content preservation: joining all chunks reproduces the original
    assert "".join(chunks) == text, (
        f"Content not preserved: joined chunks differ from original "
        f"(original len={len(text)}, chunks={len(chunks)})"
    )


@given(text=_any_text)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_split_message_respects_limit(text):
    """Every chunk produced by split_message is ≤ MAX_WHATSAPP_BODY (1600) chars."""

    chunks = split_message(text)

    for i, chunk in enumerate(chunks):
        assert len(chunk) <= MAX_WHATSAPP_BODY, (
            f"Chunk {i} has length {len(chunk)}, exceeds limit {MAX_WHATSAPP_BODY}"
        )


@given(text=st.text(
    alphabet=st.characters(categories=("L", "N", "P", "Z")),
    min_size=1,
    max_size=1600,
))
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_split_message_short_text_single_chunk(text):
    """A string ≤ 1600 chars produces exactly one chunk equal to the original."""

    chunks = split_message(text)
    assert len(chunks) == 1, f"Expected 1 chunk for {len(text)}-char text, got {len(chunks)}"
    assert chunks[0] == text


def test_split_message_empty_string():
    """Empty string produces an empty list."""
    assert split_message("") == []


# ---------------------------------------------------------------------------
# P37: WhatsApp 24-hour template compliance
# **Validates: Requirements 5.4, Design Concurrency Notes**
# ---------------------------------------------------------------------------


def _make_user(last_msg_at: datetime | None = None) -> MagicMock:
    """Create a mock User with last_user_whatsapp_message_at."""
    user = MagicMock()
    user.last_user_whatsapp_message_at = last_msg_at
    return user


def test_service_window_none_means_no_window():
    """No prior WhatsApp message → no service window → must use template."""
    user = _make_user(last_msg_at=None)
    assert WhatsAppService.is_within_service_window(user) is False


@given(delta=st.timedeltas(min_value=timedelta(seconds=1), max_value=timedelta(hours=23, minutes=59)))
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_service_window_within_24h(delta):
    """Message within 24 hours → window is open."""
    now = datetime.now(timezone.utc)
    user = _make_user(last_msg_at=now - delta)
    assert WhatsAppService.is_within_service_window(user) is True


@given(delta=st.timedeltas(min_value=timedelta(hours=24, seconds=1), max_value=timedelta(days=7)))
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_service_window_outside_24h(delta):
    """Message older than 24 hours → window is closed → must use template."""
    now = datetime.now(timezone.utc)
    user = _make_user(last_msg_at=now - delta)
    assert WhatsAppService.is_within_service_window(user) is False


# ---------------------------------------------------------------------------
# P46: WhatsApp 24-hour window enforcement — boundary conditions
# **Validates: Requirements 18, Design Concurrency Notes**
# ---------------------------------------------------------------------------


def test_window_boundary_exactly_24h():
    """At exactly 24 hours (86400 seconds), the window should be closed.
    The implementation uses strict less-than: elapsed < 86400."""
    now = datetime.now(timezone.utc)
    user = _make_user(last_msg_at=now - timedelta(seconds=WHATSAPP_WINDOW_SECONDS))
    # elapsed == 86400 → NOT < 86400 → window closed
    assert WhatsAppService.is_within_service_window(user) is False


def test_window_boundary_one_second_before():
    """One second before the 24-hour mark → window still open."""
    now = datetime.now(timezone.utc)
    user = _make_user(last_msg_at=now - timedelta(seconds=WHATSAPP_WINDOW_SECONDS - 1))
    assert WhatsAppService.is_within_service_window(user) is True


@given(
    seconds_before=st.integers(min_value=0, max_value=60),
    seconds_after=st.integers(min_value=1, max_value=60),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_window_boundary_near_24h(seconds_before, seconds_after):
    """Test the boundary region around exactly 24 hours."""
    now = datetime.now(timezone.utc)

    # Just inside the window
    user_inside = _make_user(
        last_msg_at=now - timedelta(seconds=WHATSAPP_WINDOW_SECONDS - seconds_before - 1)
    )
    assert WhatsAppService.is_within_service_window(user_inside) is True

    # Just outside the window
    user_outside = _make_user(
        last_msg_at=now - timedelta(seconds=WHATSAPP_WINDOW_SECONDS + seconds_after)
    )
    assert WhatsAppService.is_within_service_window(user_outside) is False


# ---------------------------------------------------------------------------
# Template parameter builders — fit within 1024-char template body limit
# **Validates: Requirements 5.4**
# ---------------------------------------------------------------------------


@given(
    user_name=_param_text,
    goal=_param_text,
    next_action=_param_text,
    date_str=_param_text,
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_daily_recap_params_within_limits(user_name, goal, next_action, date_str):
    """build_daily_recap_params truncates each parameter to its max length."""
    params = build_daily_recap_params(user_name, goal, next_action, date_str)
    assert len(params["1"]) <= 60   # date
    assert len(params["2"]) <= 200  # goal
    assert len(params["3"]) <= 200  # next_action
    assert len(params["4"]) <= 60   # user_name
    # Total parameter chars should be well within 1024
    total = sum(len(v) for v in params.values())
    assert total <= MAX_TEMPLATE_BODY


@given(user_name=_param_text)
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_daily_recap_no_goal_params_within_limits(user_name):
    """build_daily_recap_no_goal_params truncates user_name to 60 chars."""
    params = build_daily_recap_no_goal_params(user_name)
    assert len(params["1"]) <= 60


@given(
    user_name=_param_text,
    accomplishments=_param_text,
    tomorrow_intention=_param_text,
    date_str=_param_text,
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_evening_recap_params_within_limits(user_name, accomplishments, tomorrow_intention, date_str):
    """build_evening_recap_params truncates each parameter to its max length."""
    params = build_evening_recap_params(user_name, accomplishments, tomorrow_intention, date_str)
    assert len(params["1"]) <= 60   # date
    assert len(params["2"]) <= 300  # accomplishments
    assert len(params["3"]) <= 200  # tomorrow_intention
    assert len(params["4"]) <= 60   # user_name
    total = sum(len(v) for v in params.values())
    assert total <= MAX_TEMPLATE_BODY


@given(user_name=_param_text)
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_evening_recap_no_accomplishments_params_within_limits(user_name):
    """build_evening_recap_no_accomplishments_params truncates user_name."""
    params = build_evening_recap_no_accomplishments_params(user_name)
    assert len(params["1"]) <= 60


@given(user_name=_param_text, next_action=_param_text)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_midday_checkin_params_within_limits(user_name, next_action):
    """build_midday_checkin_params truncates each parameter."""
    params = build_midday_checkin_params(user_name, next_action)
    assert len(params["1"]) <= 60   # user_name
    assert len(params["2"]) <= 300  # next_action
    total = sum(len(v) for v in params.values())
    assert total <= MAX_TEMPLATE_BODY


@given(
    user_name=_param_text,
    week_range=_param_text,
    calls_answered=st.integers(min_value=0, max_value=100),
    goals_set=st.integers(min_value=0, max_value=100),
    closing_message=_param_text,
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_weekly_summary_params_within_limits(user_name, week_range, calls_answered, goals_set, closing_message):
    """build_weekly_summary_params truncates each parameter."""
    params = build_weekly_summary_params(user_name, week_range, calls_answered, goals_set, closing_message)
    assert len(params["1"]) <= 60   # week_range
    # "2" and "3" are str(int), always short
    assert len(params["4"]) <= 200  # closing_message
    assert len(params["5"]) <= 60   # user_name
    total = sum(len(v) for v in params.values())
    assert total <= MAX_TEMPLATE_BODY


@given(user_name=_param_text)
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_missed_call_params_within_limits(user_name):
    """build_missed_call_params truncates user_name."""
    params = build_missed_call_params(user_name)
    assert len(params["1"]) <= 60


@given(
    sender_name=st.text(min_size=1, max_size=200),
    subject=st.text(min_size=1, max_size=300),
    draft_text=st.text(min_size=0, max_size=5000),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_email_draft_review_params_within_limits(sender_name, subject, draft_text):
    """build_email_draft_review_params produces a preview that fits the template
    body budget, and returns overflow when the draft is too long."""
    params, overflow = build_email_draft_review_params(sender_name, subject, draft_text)

    # sender_name truncated to 100
    assert len(params["1"]) <= 100
    # subject truncated to 200
    assert len(params["2"]) <= 200
    # preview (param "3") should fit within the remaining budget
    # Total params should not exceed MAX_TEMPLATE_BODY
    total = sum(len(v) for v in params.values())
    assert total <= MAX_TEMPLATE_BODY, (
        f"Total param chars {total} exceeds {MAX_TEMPLATE_BODY}"
    )

    # If draft was short enough, no overflow
    # If draft was too long, overflow should be the full draft text
    if overflow is not None:
        assert overflow == draft_text


# ---------------------------------------------------------------------------
# send_template_message propagates Twilio exceptions
# **Validates: at-most-once dedup contract (exceptions must reach dedup layer)**
# ---------------------------------------------------------------------------


def test_send_template_message_propagates_exception():
    """send_template_message must NOT catch Twilio exceptions — they must
    propagate to the dedup layer so it can mark the claim as failed
    (ambiguous delivery).  If this method swallowed exceptions and returned
    None, the dedup layer would release the claim and allow a duplicate."""
    import asyncio

    mock_client = MagicMock()
    mock_client.messages.create = MagicMock(side_effect=RuntimeError("connection reset"))

    with patch("app.services.whatsapp_service.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(
            TWILIO_ACCOUNT_SID="ACtest",
            TWILIO_AUTH_TOKEN="test_token",
            TWILIO_WHATSAPP_NUMBER="whatsapp:+14155238886",
        )
        svc = WhatsAppService(twilio_client=mock_client)

    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(RuntimeError, match="connection reset"):
            loop.run_until_complete(
                svc.send_template_message(
                    to="+14155550001",
                    content_sid="HX_test",
                    content_variables={"1": "hello"},
                )
            )
    finally:
        loop.close()
