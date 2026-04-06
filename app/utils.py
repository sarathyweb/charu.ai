"""Shared utilities — phone normalization, HMAC stream tokens, and helpers."""

import hashlib
import hmac
import time as _time

import phonenumbers


def normalize_phone(raw: str, default_region: str | None = None) -> str:
    """Normalize a phone number to E.164 format.

    Args:
        raw: Raw phone number string (e.g. "+971501234567", "0501234567").
        default_region: ISO 3166-1 alpha-2 region code used when *raw*
            lacks an international prefix (e.g. "AE", "US").

    Returns:
        The phone number in E.164 format (e.g. "+971501234567").

    Raises:
        ValueError: If the number cannot be parsed or is not valid.
    """
    try:
        parsed = phonenumbers.parse(raw, default_region)
    except phonenumbers.NumberParseException as exc:
        raise ValueError(f"Invalid phone number: {raw}") from exc

    if not phonenumbers.is_valid_number(parsed):
        raise ValueError(f"Invalid phone number: {raw}")

    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


# ---------------------------------------------------------------------------
# HMAC stream token — signs WebSocket stream URLs for voice calls
# ---------------------------------------------------------------------------

_TOKEN_TTL_SECONDS = 300  # 5 minutes


def generate_stream_token(
    secret: str,
    call_log_id: int,
    user_id: int,
    *,
    ttl: int = _TOKEN_TTL_SECONDS,
) -> str:
    """Create an HMAC-signed token for authenticating a voice WebSocket stream.

    The token encodes ``call_log_id``, ``user_id``, and an expiry timestamp.
    The voice stream endpoint validates the token before accepting the
    WebSocket connection (Property 42).

    Returns:
        A string in the format ``{call_log_id}:{user_id}:{expires}:{signature}``.
    """
    expires = int(_time.time()) + ttl
    payload = f"{call_log_id}:{user_id}:{expires}"
    sig = hmac.new(
        secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return f"{payload}:{sig}"


def verify_stream_token(
    secret: str,
    token: str,
) -> dict | None:
    """Validate an HMAC stream token.

    Returns a dict with ``call_log_id``, ``user_id``, and ``expires`` on
    success, or ``None`` if the token is invalid or expired.
    """
    parts = token.split(":")
    if len(parts) != 4:
        return None

    call_log_id_str, user_id_str, expires_str, sig = parts

    try:
        call_log_id = int(call_log_id_str)
        user_id = int(user_id_str)
        expires = int(expires_str)
    except ValueError:
        return None

    # Check expiry
    if _time.time() > expires:
        return None

    # Verify signature
    payload = f"{call_log_id}:{user_id}:{expires}"
    expected_sig = hmac.new(
        secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(sig, expected_sig):
        return None

    return {
        "call_log_id": call_log_id,
        "user_id": user_id,
        "expires": expires,
    }
