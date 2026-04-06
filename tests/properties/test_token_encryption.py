"""Property tests for token encryption round-trip (P27).

Property 27 — Token encryption round-trip:
  For any token string, ``decrypt_token(encrypt_token(token))`` should return
  the original token string.

These are pure-function tests — no database required.  The Fernet key is
generated fresh per test session and injected via a monkeypatch on the
module-level ``_fernet`` cache.

Validates: Requirements 15.3
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet, InvalidToken
from hypothesis import given, settings, strategies as st, HealthCheck

from app.services.google_oauth_service import encrypt_token, decrypt_token
import app.services.google_oauth_service as _mod

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# A valid Fernet key generated once for the entire test module.
_TEST_FERNET_KEY = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def _inject_fernet_key():
    """Inject a test Fernet instance so encrypt/decrypt don't need .env."""
    _mod._fernet = Fernet(_TEST_FERNET_KEY.encode())
    yield
    _mod._fernet = None


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Arbitrary unicode strings (the realistic superset of OAuth tokens).
_token_text = st.text(min_size=1, max_size=2048)

# ASCII-only tokens resembling real OAuth access/refresh tokens.
_ascii_token = st.from_regex(r"[A-Za-z0-9_\-\.]{10,512}", fullmatch=True)


# ---------------------------------------------------------------------------
# P27a: Round-trip — decrypt(encrypt(token)) == token for any string
# ---------------------------------------------------------------------------


@given(token=_token_text)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_encrypt_decrypt_round_trip(token: str):
    """decrypt_token(encrypt_token(t)) must return the original string."""
    assert decrypt_token(encrypt_token(token)) == token


# ---------------------------------------------------------------------------
# P27b: Round-trip with ASCII-only tokens (realistic OAuth tokens)
# ---------------------------------------------------------------------------


@given(token=_ascii_token)
@settings(max_examples=200)
def test_encrypt_decrypt_round_trip_ascii(token: str):
    """Round-trip holds for ASCII tokens resembling real OAuth values."""
    assert decrypt_token(encrypt_token(token)) == token


# ---------------------------------------------------------------------------
# P27c: Ciphertext differs from plaintext
# ---------------------------------------------------------------------------


@given(token=_token_text)
@settings(max_examples=100)
def test_ciphertext_differs_from_plaintext(token: str):
    """The encrypted output must never equal the plaintext input."""
    encrypted = encrypt_token(token)
    assert encrypted != token


# ---------------------------------------------------------------------------
# P27d: Ciphertext is non-deterministic (Fernet uses random IV)
# ---------------------------------------------------------------------------


@given(token=_ascii_token)
@settings(max_examples=50)
def test_ciphertext_is_non_deterministic(token: str):
    """Two encryptions of the same plaintext should produce different
    ciphertexts (Fernet includes a random IV per encryption)."""
    a = encrypt_token(token)
    b = encrypt_token(token)
    assert a != b


# ---------------------------------------------------------------------------
# P27e: Wrong key cannot decrypt
# ---------------------------------------------------------------------------


@given(token=_ascii_token)
@settings(max_examples=50)
def test_wrong_key_cannot_decrypt(token: str):
    """Ciphertext encrypted with one key must not decrypt with another."""
    encrypted = encrypt_token(token)

    other_fernet = Fernet(Fernet.generate_key())
    with pytest.raises(InvalidToken):
        other_fernet.decrypt(encrypted.encode("utf-8"))


# ---------------------------------------------------------------------------
# P27f: Tampered ciphertext raises InvalidToken
# ---------------------------------------------------------------------------


@given(token=_ascii_token)
@settings(max_examples=50)
def test_tampered_ciphertext_raises(token: str):
    """Flipping a byte in the ciphertext must cause decryption to fail."""
    encrypted = encrypt_token(token)
    raw = bytearray(encrypted.encode("utf-8"))
    # Flip a byte near the middle (skip the version byte at index 0)
    idx = max(1, len(raw) // 2)
    raw[idx] ^= 0xFF
    tampered = bytes(raw).decode("utf-8", errors="replace")
    with pytest.raises(Exception):
        decrypt_token(tampered)
