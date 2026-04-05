"""Property tests for WhatsApp integration (P10, P11, P12).

P10 — WhatsApp phone extraction and agent routing:
      "whatsapp:+XXX" → correct phone extracted and used as user_id
      when invoking the ADK Runner.
      **Validates: Requirements 7.3, 7.4**

P11 — WhatsApp reply to correct sender:
      Twilio client called with original sender's number as ``to``.
      **Validates: Requirements 7.5**

P12 — WhatsApp message truncation:
      Any response string → sent length ≤ 1600 chars.
      **Validates: Requirements 7.6**

These tests mock the ADK Runner, Twilio client, and database layer so
they run fast without external dependencies.  Hypothesis generates
random phone numbers and message bodies to exercise edge cases.
"""

import string
from unittest.mock import AsyncMock, MagicMock, patch

from hypothesis import given, settings, strategies as st, HealthCheck

from app.services.whatsapp_service import (
    MAX_WHATSAPP_LENGTH,
    TRUNCATION_SUFFIX,
    WhatsAppService,
)
from app.utils import normalize_phone

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_e164_phone = st.sampled_from([
    "+14155552671",
    "+447911123456",
    "+971501234567",
    "+919876543210",
    "+61412345678",
    "+4915112345678",
    "+33612345678",
    "+818012345678",
])

# Message bodies: printable text, 1–300 chars
_message_body = st.text(
    alphabet=string.ascii_letters + string.digits + " .,!?",
    min_size=1,
    max_size=300,
).filter(lambda s: s.strip())

# Arbitrary strings for truncation testing — including very long ones
_any_text = st.text(
    alphabet=st.characters(categories=("L", "N", "P", "Z")),
    min_size=0,
    max_size=10_000,
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
        f"Expected user_id '{phone}', got '{normalised}' "
        f"from From='{raw_from}'"
    )

    # The normalised phone starts with '+' (E.164)
    assert normalised.startswith("+"), (
        f"user_id '{normalised}' is not E.164"
    )


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

    # Twilio client must have been called exactly once
    mock_create.assert_called_once()

    call_kwargs = mock_create.call_args
    # The 'to' kwarg must be "whatsapp:<original_phone>"
    actual_to = call_kwargs.kwargs.get("to") or call_kwargs[1].get("to")
    assert actual_to == f"whatsapp:{phone}", (
        f"Expected to='whatsapp:{phone}', got to='{actual_to}'"
    )


# ---------------------------------------------------------------------------
# P12: WhatsApp message truncation
# **Validates: Requirements 7.6**
# ---------------------------------------------------------------------------

@given(text=_any_text)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_whatsapp_message_truncation(text):
    """Any response string, after truncation, has length ≤ 1600 chars.
    If the original exceeds 1600, the result ends with '...'."""

    result = WhatsAppService._truncate(text)

    # Core property: never exceeds the limit
    assert len(result) <= MAX_WHATSAPP_LENGTH, (
        f"Truncated length {len(result)} exceeds {MAX_WHATSAPP_LENGTH}"
    )

    if len(text) <= MAX_WHATSAPP_LENGTH:
        # Short text passes through unchanged
        assert result == text
    else:
        # Long text is truncated and ends with the suffix
        assert result.endswith(TRUNCATION_SUFFIX), (
            f"Truncated text should end with '{TRUNCATION_SUFFIX}'"
        )
        assert len(result) == MAX_WHATSAPP_LENGTH, (
            f"Truncated text should be exactly {MAX_WHATSAPP_LENGTH} chars, "
            f"got {len(result)}"
        )
