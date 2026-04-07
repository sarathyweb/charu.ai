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
    _MAX_USES,
    _TTL_SECONDS,
    create_ephemeral_token,
    validate_ephemeral_token,
)

# ---------------------------------------------------------------------------
# Helpers — fakeredis async stub
# ---------------------------------------------------------------------------


class FakePipeline:
    """Minimal async pipeline stub that buffers commands and executes them."""

    def __init__(self, store: dict[str, str], ttls: dict[str, int]):
        self._store = store
        self._ttls = ttls
        self._commands: list[tuple] = []

    def set(self, key: str, value: str, *, ex: int | None = None):
        self._commands.append(("set", key, value, ex))
        return self

    async def execute(self):
        results = []
        for cmd in self._commands:
            if cmd[0] == "set":
                _, key, value, ex = cmd
                self._store[key] = value
                if ex is not None:
                    self._ttls[key] = ex
                results.append(True)
        self._commands.clear()
        return results

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class FakeRedis:
    """Minimal async Redis stub that supports set/get/getdel/decr/delete/pipeline/aclose."""

    def __init__(self):
        self._store: dict[str, str] = {}
        self._ttls: dict[str, int] = {}

    async def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        self._store[key] = value
        if ex is not None:
            self._ttls[key] = ex

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def getdel(self, key: str) -> str | None:
        return self._store.pop(key, None)

    async def decr(self, key: str) -> int:
        if key not in self._store:
            self._store[key] = "-1"
            return -1
        val = int(self._store[key]) - 1
        self._store[key] = str(val)
        return val

    async def delete(self, *keys: str) -> int:
        count = 0
        for key in keys:
            if key in self._store:
                del self._store[key]
                self._ttls.pop(key, None)
                count += 1
        return count

    def pipeline(self, transaction: bool = False):
        return FakePipeline(self._store, self._ttls)

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
def test_limited_use(user_id: int, service: str) -> None:
    """A token allows up to _MAX_USES consumptions; subsequent attempts return None.

    The service allows 3 uses to tolerate link-preview crawlers (WhatsApp,
    Facebook) that prefetch URLs before the real user clicks.
    """
    fr = FakeRedis()

    async def _mock_get_redis():
        return fr

    with patch("app.services.ephemeral_token_service._get_redis", new=_mock_get_redis):
        loop = asyncio.new_event_loop()
        try:
            token = loop.run_until_complete(create_ephemeral_token(user_id, service))
            results = [
                loop.run_until_complete(validate_ephemeral_token(token))
                for _ in range(_MAX_USES + 1)
            ]
        finally:
            loop.close()

    # First _MAX_USES calls succeed
    for i in range(_MAX_USES):
        assert results[i] is not None, f"Consumption {i+1} of {_MAX_USES} must succeed"
    # The call after _MAX_USES must fail
    assert results[_MAX_USES] is None, f"Consumption {_MAX_USES+1} must return None (uses exhausted)"


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
