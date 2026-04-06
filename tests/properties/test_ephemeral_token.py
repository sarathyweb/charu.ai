"""Property-based tests for the ephemeral OAuth token system.

These tests validate correctness properties of the Redis-backed ephemeral
token service without requiring a live Redis instance — they use ``fakeredis``
to provide an in-memory Redis implementation.

Correctness properties tested:
  P1 — Single-use: a token can only be consumed once.
  P2 — Payload integrity: the consumed payload matches what was stored.
  P3 — Expiry: tokens are not retrievable after TTL elapses.
  P4 — Unknown tokens: validating a never-created token returns None.
  P5 — Token entropy: generated tokens are unique across many creations.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from app.services.ephemeral_token_service import (
    _KEY_PREFIX,
    _TTL_SECONDS,
    create_ephemeral_token,
    validate_ephemeral_token,
)

# ---------------------------------------------------------------------------
# Helpers — fakeredis async stub
# ---------------------------------------------------------------------------


class FakeRedis:
    """Minimal async Redis stub that supports set/getdel/aclose."""

    def __init__(self):
        self._store: dict[str, str] = {}
        self._ttls: dict[str, int] = {}

    async def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        self._store[key] = value
        if ex is not None:
            self._ttls[key] = ex

    async def getdel(self, key: str) -> str | None:
        return self._store.pop(key, None)

    async def aclose(self) -> None:
        pass

    def expire_all(self) -> None:
        """Simulate TTL expiry by clearing the store."""
        self._store.clear()
        self._ttls.clear()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_redis():
    """Provide a fresh FakeRedis instance and patch _get_redis to return it."""
    fr = FakeRedis()

    async def _mock_get_redis():
        return fr

    with patch("app.services.ephemeral_token_service._get_redis", new=_mock_get_redis):
        yield fr


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

user_ids = st.integers(min_value=1, max_value=2**31 - 1)
services = st.sampled_from(["calendar", "gmail"])


# ---------------------------------------------------------------------------
# P1 — Single-use: consuming a token twice yields None the second time
# ---------------------------------------------------------------------------


@given(user_id=user_ids, service=services)
@settings(max_examples=50)
def test_single_use(user_id: int, service: str) -> None:
    """A token can only be consumed once; the second attempt returns None."""
    fr = FakeRedis()

    async def _mock_get_redis():
        return fr

    with patch("app.services.ephemeral_token_service._get_redis", new=_mock_get_redis):
        loop = asyncio.new_event_loop()
        try:
            token = loop.run_until_complete(create_ephemeral_token(user_id, service))
            first = loop.run_until_complete(validate_ephemeral_token(token))
            second = loop.run_until_complete(validate_ephemeral_token(token))
        finally:
            loop.close()

    assert first is not None, "First consumption must succeed"
    assert second is None, "Second consumption must return None (single-use)"


# ---------------------------------------------------------------------------
# P2 — Payload integrity: consumed data matches what was stored
# ---------------------------------------------------------------------------


@given(user_id=user_ids, service=services)
@settings(max_examples=50)
def test_payload_integrity(user_id: int, service: str) -> None:
    """The payload returned on consumption matches the original arguments."""
    fr = FakeRedis()

    async def _mock_get_redis():
        return fr

    with patch("app.services.ephemeral_token_service._get_redis", new=_mock_get_redis):
        loop = asyncio.new_event_loop()
        try:
            token = loop.run_until_complete(create_ephemeral_token(user_id, service))
            data = loop.run_until_complete(validate_ephemeral_token(token))
        finally:
            loop.close()

    assert data is not None
    assert data["user_id"] == user_id
    assert data["service"] == service


# ---------------------------------------------------------------------------
# P3 — Expiry: after TTL, token is gone
# ---------------------------------------------------------------------------


@given(user_id=user_ids, service=services)
@settings(max_examples=20)
def test_expiry_clears_token(user_id: int, service: str) -> None:
    """After TTL expiry (simulated), the token is no longer retrievable."""
    fr = FakeRedis()

    async def _mock_get_redis():
        return fr

    with patch("app.services.ephemeral_token_service._get_redis", new=_mock_get_redis):
        loop = asyncio.new_event_loop()
        try:
            token = loop.run_until_complete(create_ephemeral_token(user_id, service))
            # Simulate TTL expiry
            fr.expire_all()
            data = loop.run_until_complete(validate_ephemeral_token(token))
        finally:
            loop.close()

    assert data is None, "Token must not be retrievable after expiry"


# ---------------------------------------------------------------------------
# P4 — Unknown tokens return None
# ---------------------------------------------------------------------------


@given(token=st.text(min_size=1, max_size=64))
@settings(max_examples=50)
def test_unknown_token_returns_none(token: str) -> None:
    """Validating a token that was never created returns None."""
    fr = FakeRedis()

    async def _mock_get_redis():
        return fr

    with patch("app.services.ephemeral_token_service._get_redis", new=_mock_get_redis):
        loop = asyncio.new_event_loop()
        try:
            data = loop.run_until_complete(validate_ephemeral_token(token))
        finally:
            loop.close()

    assert data is None


# ---------------------------------------------------------------------------
# P5 — Token uniqueness: many tokens for the same user are all distinct
# ---------------------------------------------------------------------------


@given(user_id=user_ids, service=services)
@settings(max_examples=20)
def test_token_uniqueness(user_id: int, service: str) -> None:
    """Multiple tokens created for the same user/service are all unique."""
    fr = FakeRedis()

    async def _mock_get_redis():
        return fr

    with patch("app.services.ephemeral_token_service._get_redis", new=_mock_get_redis):
        loop = asyncio.new_event_loop()
        try:
            tokens = [
                loop.run_until_complete(create_ephemeral_token(user_id, service))
                for _ in range(20)
            ]
        finally:
            loop.close()

    assert len(set(tokens)) == len(tokens), "All generated tokens must be unique"


# ---------------------------------------------------------------------------
# P6 — TTL is set correctly on creation
# ---------------------------------------------------------------------------


@given(user_id=user_ids, service=services)
@settings(max_examples=20)
def test_ttl_set_on_creation(user_id: int, service: str) -> None:
    """The Redis key is created with the correct TTL (600 seconds)."""
    fr = FakeRedis()

    async def _mock_get_redis():
        return fr

    with patch("app.services.ephemeral_token_service._get_redis", new=_mock_get_redis):
        loop = asyncio.new_event_loop()
        try:
            token = loop.run_until_complete(create_ephemeral_token(user_id, service))
        finally:
            loop.close()

    key = f"{_KEY_PREFIX}{token}"
    assert key in fr._ttls, "TTL must be set on the key"
    assert fr._ttls[key] == _TTL_SECONDS, f"TTL must be {_TTL_SECONDS}s"
