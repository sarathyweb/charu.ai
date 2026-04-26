"""Redis-backed cache for prebuilt voice call context."""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

import redis.asyncio as aioredis

from app.config import get_settings

logger = logging.getLogger(__name__)

DEFAULT_CALL_CONTEXT_TTL_SECONDS = 15 * 60


def _cache_key(call_log_id: int) -> str:
    return f"voice_context:{call_log_id}"


async def _get_redis() -> aioredis.Redis:
    return aioredis.from_url(get_settings().REDIS_URL, decode_responses=True)


def _serialize_call_ctx(ctx: dict[str, Any]) -> dict[str, Any]:
    """Return the JSON-safe subset needed after a voice call ends."""
    new_last_active = ctx.get("new_last_active")
    if isinstance(new_last_active, date):
        new_last_active = new_last_active.isoformat()

    opener = ctx.get("opener")
    if not isinstance(opener, dict):
        opener = None

    return {
        "opener": opener,
        "approach": ctx.get("approach"),
        "streak_days": ctx.get("streak_days"),
        "new_last_active": new_last_active,
        "available_context": ctx.get("available_context"),
        "is_weekend": ctx.get("is_weekend"),
    }


def _deserialize_call_ctx(ctx: dict[str, Any]) -> dict[str, Any]:
    new_last_active = ctx.get("new_last_active")
    if isinstance(new_last_active, str):
        try:
            ctx["new_last_active"] = date.fromisoformat(new_last_active)
        except ValueError:
            logger.warning("Ignoring invalid cached new_last_active=%r", new_last_active)
            ctx["new_last_active"] = None
    return ctx


async def store_call_context(
    call_log_id: int,
    system_instruction: str,
    call_ctx: dict[str, Any],
    *,
    ttl_seconds: int = DEFAULT_CALL_CONTEXT_TTL_SECONDS,
) -> None:
    """Store a prebuilt voice system instruction and cleanup context."""
    payload = {
        "system_instruction": system_instruction,
        "call_ctx": _serialize_call_ctx(call_ctx),
    }
    r = await _get_redis()
    try:
        await r.set(_cache_key(call_log_id), json.dumps(payload), ex=ttl_seconds)
    finally:
        await r.aclose()


async def get_cached_call_context(call_log_id: int) -> tuple[str, dict[str, Any]] | None:
    """Return cached voice context, or None on cache miss / invalid payload."""
    r = await _get_redis()
    try:
        raw = await r.get(_cache_key(call_log_id))
    finally:
        await r.aclose()

    if raw is None:
        return None

    try:
        payload = json.loads(raw)
        instruction = payload["system_instruction"]
        call_ctx = payload["call_ctx"]
    except (KeyError, TypeError, json.JSONDecodeError):
        logger.warning("Ignoring invalid cached voice context for call_log_id=%d", call_log_id)
        return None

    if not isinstance(instruction, str) or not isinstance(call_ctx, dict):
        logger.warning("Ignoring malformed cached voice context for call_log_id=%d", call_log_id)
        return None

    return instruction, _deserialize_call_ctx(call_ctx)


async def delete_call_context(call_log_id: int) -> None:
    """Delete cached voice context after a call finishes."""
    r = await _get_redis()
    try:
        await r.delete(_cache_key(call_log_id))
    finally:
        await r.aclose()
