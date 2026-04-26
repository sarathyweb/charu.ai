"""Unit tests for Redis-backed voice call context cache."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from app.services import call_context_cache


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.expires: dict[str, int | None] = {}
        self.closed = False

    async def set(self, key, value, ex=None):
        self.store[key] = value
        self.expires[key] = ex

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, *keys):
        for key in keys:
            self.store.pop(key, None)

    async def scan_iter(self, match=None):
        for key in list(self.store):
            if match is None or key.startswith(match.removesuffix("*")):
                yield key

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_store_and_load_json_safe_context(monkeypatch):
    fake = FakeRedis()
    fake.expires = {}
    scheduled_time = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)

    async def get_redis():
        return fake

    monkeypatch.setattr(call_context_cache, "_get_redis", get_redis)

    await call_context_cache.store_call_context(
        42,
        scheduled_time,
        "system instruction",
        {
            "opener": {"id": "direct_1"},
            "approach": "task_led",
            "streak_days": 4,
            "new_last_active": date(2026, 4, 26),
            "pending_tasks": [object()],
        },
    )

    cached = await call_context_cache.get_cached_call_context(42, scheduled_time)

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
    assert list(fake.expires.values()) == [30 * 60]


@pytest.mark.asyncio
async def test_invalid_cache_payload_returns_none(monkeypatch):
    fake = FakeRedis()
    fake.expires = {}
    scheduled_time = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    fake.store[f"voice_context:9:{scheduled_time.isoformat()}"] = json.dumps(
        {"not": "valid"}
    )

    async def get_redis():
        return fake

    monkeypatch.setattr(call_context_cache, "_get_redis", get_redis)

    assert await call_context_cache.get_cached_call_context(9, scheduled_time) is None


@pytest.mark.asyncio
async def test_scheduled_time_mismatch_returns_none(monkeypatch):
    fake = FakeRedis()
    fake.expires = {}
    scheduled_time = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    cached_time = datetime(2026, 4, 26, 12, 5, tzinfo=timezone.utc)
    fake.store[f"voice_context:9:{scheduled_time.isoformat()}"] = json.dumps(
        {
            "scheduled_time": cached_time.isoformat(),
            "system_instruction": "old",
            "call_ctx": {},
        }
    )

    async def get_redis():
        return fake

    monkeypatch.setattr(call_context_cache, "_get_redis", get_redis)

    assert await call_context_cache.get_cached_call_context(9, scheduled_time) is None


@pytest.mark.asyncio
async def test_delete_call_context(monkeypatch):
    fake = FakeRedis()
    fake.expires = {}
    scheduled_time = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    fake.store["voice_context:7"] = "{}"
    fake.store[f"voice_context:7:{scheduled_time.isoformat()}"] = "{}"

    async def get_redis():
        return fake

    monkeypatch.setattr(call_context_cache, "_get_redis", get_redis)

    await call_context_cache.delete_call_context(7)

    assert "voice_context:7" not in fake.store
    assert f"voice_context:7:{scheduled_time.isoformat()}" not in fake.store
