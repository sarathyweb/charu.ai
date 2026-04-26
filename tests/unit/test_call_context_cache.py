"""Unit tests for Redis-backed voice call context cache."""

from __future__ import annotations

import json
from datetime import date

import pytest

from app.services import call_context_cache


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.closed = False

    async def set(self, key, value, ex=None):  # noqa: ARG002
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_store_and_load_json_safe_context(monkeypatch):
    fake = FakeRedis()

    async def get_redis():
        return fake

    monkeypatch.setattr(call_context_cache, "_get_redis", get_redis)

    await call_context_cache.store_call_context(
        42,
        "system instruction",
        {
            "opener": {"id": "direct_1"},
            "approach": "task_led",
            "streak_days": 4,
            "new_last_active": date(2026, 4, 26),
            "pending_tasks": [object()],
        },
    )

    cached = await call_context_cache.get_cached_call_context(42)

    assert cached == (
        "system instruction",
        {
            "opener": {"id": "direct_1"},
            "approach": "task_led",
            "streak_days": 4,
            "new_last_active": date(2026, 4, 26),
            "available_context": None,
            "is_weekend": None,
        },
    )


@pytest.mark.asyncio
async def test_invalid_cache_payload_returns_none(monkeypatch):
    fake = FakeRedis()
    fake.store["voice_context:9"] = json.dumps({"not": "valid"})

    async def get_redis():
        return fake

    monkeypatch.setattr(call_context_cache, "_get_redis", get_redis)

    assert await call_context_cache.get_cached_call_context(9) is None


@pytest.mark.asyncio
async def test_delete_call_context(monkeypatch):
    fake = FakeRedis()
    fake.store["voice_context:7"] = "{}"

    async def get_redis():
        return fake

    monkeypatch.setattr(call_context_cache, "_get_redis", get_redis)

    await call_context_cache.delete_call_context(7)

    assert "voice_context:7" not in fake.store
