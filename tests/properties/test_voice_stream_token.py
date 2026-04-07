"""Property tests for WebSocket stream token validation (P42).

Property 42 — WebSocket stream token validation:
  For any valid (call_log_id, user_id, secret, ttl), generate_stream_token
  followed by verify_stream_token with the same secret returns the original
  payload. Tokens with wrong secrets, expired TTLs, or tampered payloads
  are rejected.

These are pure-function tests — no database or WebSocket required.

**Validates: Design Error Handling section**
"""

from __future__ import annotations

import time as _time
from unittest.mock import patch

from hypothesis import given, settings, strategies as st, HealthCheck, assume

from app.utils import generate_stream_token, verify_stream_token

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Positive integers for IDs (realistic range for DB primary keys)
_call_log_ids = st.integers(min_value=1, max_value=2**31 - 1)
_user_ids = st.integers(min_value=1, max_value=2**31 - 1)

# Secrets: non-empty ASCII strings (HMAC keys)
_secrets = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "S")),
    min_size=8,
    max_size=128,
).filter(lambda s: len(s.strip()) > 0)

# TTL: positive values for valid tokens
_valid_ttls = st.integers(min_value=10, max_value=86400)

# TTL: zero or negative for expired tokens
_expired_ttls = st.integers(min_value=-86400, max_value=0)


# ---------------------------------------------------------------------------
# P42a: Round-trip — generate then verify with same secret returns payload
# ---------------------------------------------------------------------------


@given(
    call_log_id=_call_log_ids,
    user_id=_user_ids,
    secret=_secrets,
    ttl=_valid_ttls,
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_generate_verify_round_trip(
    call_log_id: int, user_id: int, secret: str, ttl: int
):
    """verify_stream_token(secret, generate_stream_token(secret, ...)) returns
    the original call_log_id and user_id."""
    token = generate_stream_token(
        secret=secret, call_log_id=call_log_id, user_id=user_id, ttl=ttl
    )
    result = verify_stream_token(secret=secret, token=token)

    assert result is not None, "Valid token should verify successfully"
    assert result["call_log_id"] == call_log_id
    assert result["user_id"] == user_id
    assert result["expires"] > _time.time()


# ---------------------------------------------------------------------------
# P42b: Wrong secret returns None
# ---------------------------------------------------------------------------


@given(
    call_log_id=_call_log_ids,
    user_id=_user_ids,
    secret=_secrets,
    wrong_secret=_secrets,
    ttl=_valid_ttls,
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_wrong_secret_returns_none(
    call_log_id: int,
    user_id: int,
    secret: str,
    wrong_secret: str,
    ttl: int,
):
    """verify_stream_token with a different secret must return None."""
    assume(secret != wrong_secret)

    token = generate_stream_token(
        secret=secret, call_log_id=call_log_id, user_id=user_id, ttl=ttl
    )
    result = verify_stream_token(secret=wrong_secret, token=token)

    assert result is None, "Token verified with wrong secret should be rejected"


# ---------------------------------------------------------------------------
# P42c: Expired token (ttl <= 0) returns None
# ---------------------------------------------------------------------------


@given(
    call_log_id=_call_log_ids,
    user_id=_user_ids,
    secret=_secrets,
    ttl=_expired_ttls,
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_expired_token_returns_none(
    call_log_id: int, user_id: int, secret: str, ttl: int
):
    """A token generated with ttl <= 0 should be expired immediately."""
    token = generate_stream_token(
        secret=secret, call_log_id=call_log_id, user_id=user_id, ttl=ttl
    )
    result = verify_stream_token(secret=secret, token=token)

    assert result is None, "Expired token should be rejected"


# ---------------------------------------------------------------------------
# P42d: Tampering with any payload part causes rejection
# ---------------------------------------------------------------------------


@given(
    call_log_id=_call_log_ids,
    user_id=_user_ids,
    secret=_secrets,
    ttl=_valid_ttls,
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_tampered_call_log_id_rejected(
    call_log_id: int, user_id: int, secret: str, ttl: int
):
    """Changing the call_log_id in the token invalidates the signature."""
    token = generate_stream_token(
        secret=secret, call_log_id=call_log_id, user_id=user_id, ttl=ttl
    )
    parts = token.split(":")
    # Tamper with call_log_id (first part)
    tampered_id = call_log_id + 1
    parts[0] = str(tampered_id)
    tampered_token = ":".join(parts)

    result = verify_stream_token(secret=secret, token=tampered_token)
    assert result is None, "Token with tampered call_log_id should be rejected"


@given(
    call_log_id=_call_log_ids,
    user_id=_user_ids,
    secret=_secrets,
    ttl=_valid_ttls,
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_tampered_user_id_rejected(
    call_log_id: int, user_id: int, secret: str, ttl: int
):
    """Changing the user_id in the token invalidates the signature."""
    token = generate_stream_token(
        secret=secret, call_log_id=call_log_id, user_id=user_id, ttl=ttl
    )
    parts = token.split(":")
    # Tamper with user_id (second part)
    tampered_uid = user_id + 1
    parts[1] = str(tampered_uid)
    tampered_token = ":".join(parts)

    result = verify_stream_token(secret=secret, token=tampered_token)
    assert result is None, "Token with tampered user_id should be rejected"


@given(
    call_log_id=_call_log_ids,
    user_id=_user_ids,
    secret=_secrets,
    ttl=_valid_ttls,
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_tampered_expires_rejected(
    call_log_id: int, user_id: int, secret: str, ttl: int
):
    """Changing the expires timestamp in the token invalidates the signature."""
    token = generate_stream_token(
        secret=secret, call_log_id=call_log_id, user_id=user_id, ttl=ttl
    )
    parts = token.split(":")
    # Tamper with expires (third part) — add 1 second
    original_expires = int(parts[2])
    parts[2] = str(original_expires + 1)
    tampered_token = ":".join(parts)

    result = verify_stream_token(secret=secret, token=tampered_token)
    assert result is None, "Token with tampered expires should be rejected"


@given(
    call_log_id=_call_log_ids,
    user_id=_user_ids,
    secret=_secrets,
    ttl=_valid_ttls,
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_tampered_signature_rejected(
    call_log_id: int, user_id: int, secret: str, ttl: int
):
    """Changing a character in the HMAC signature invalidates the token."""
    token = generate_stream_token(
        secret=secret, call_log_id=call_log_id, user_id=user_id, ttl=ttl
    )
    parts = token.split(":")
    sig = parts[3]
    # Flip a hex character in the signature
    sig_list = list(sig)
    flip_idx = len(sig_list) // 2
    original_char = sig_list[flip_idx]
    sig_list[flip_idx] = "0" if original_char != "0" else "1"
    parts[3] = "".join(sig_list)
    tampered_token = ":".join(parts)

    result = verify_stream_token(secret=secret, token=tampered_token)
    assert result is None, "Token with tampered signature should be rejected"


# ---------------------------------------------------------------------------
# P42e: Token format is deterministic for same inputs at same time
# ---------------------------------------------------------------------------


@given(
    call_log_id=_call_log_ids,
    user_id=_user_ids,
    secret=_secrets,
    ttl=_valid_ttls,
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_token_deterministic_same_time(
    call_log_id: int, user_id: int, secret: str, ttl: int
):
    """Two tokens generated at the exact same time with the same inputs
    must be identical (HMAC is deterministic, unlike Fernet)."""
    fixed_time = 1700000000.0
    with patch("app.utils._time.time", return_value=fixed_time):
        token1 = generate_stream_token(
            secret=secret, call_log_id=call_log_id, user_id=user_id, ttl=ttl
        )
        token2 = generate_stream_token(
            secret=secret, call_log_id=call_log_id, user_id=user_id, ttl=ttl
        )
    assert token1 == token2, "Same inputs at same time must produce identical tokens"


# ---------------------------------------------------------------------------
# P42f: Malformed tokens are rejected
# ---------------------------------------------------------------------------


@given(secret=_secrets)
@settings(max_examples=50)
def test_malformed_tokens_rejected(secret: str):
    """Tokens with wrong number of parts or non-integer fields are rejected."""
    assert verify_stream_token(secret=secret, token="") is None
    assert verify_stream_token(secret=secret, token="garbage") is None
    assert verify_stream_token(secret=secret, token="a:b:c") is None
    assert verify_stream_token(secret=secret, token="1:2:notint:sig") is None
    assert verify_stream_token(secret=secret, token="a:b:c:d:e") is None


# ---------------------------------------------------------------------------
# P42g: Token has exactly 4 colon-separated parts
# ---------------------------------------------------------------------------


@given(
    call_log_id=_call_log_ids,
    user_id=_user_ids,
    secret=_secrets,
    ttl=_valid_ttls,
)
@settings(max_examples=100)
def test_token_format_four_parts(
    call_log_id: int, user_id: int, secret: str, ttl: int
):
    """Generated tokens must have exactly 4 colon-separated parts:
    call_log_id:user_id:expires:signature."""
    token = generate_stream_token(
        secret=secret, call_log_id=call_log_id, user_id=user_id, ttl=ttl
    )
    parts = token.split(":")
    assert len(parts) == 4, f"Expected 4 parts, got {len(parts)}"
    assert parts[0] == str(call_log_id)
    assert parts[1] == str(user_id)
    assert int(parts[2]) > 0  # expires is a positive timestamp
    assert len(parts[3]) == 64  # SHA-256 hex digest is 64 chars
