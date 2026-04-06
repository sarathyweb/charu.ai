"""Redis-backed ephemeral OAuth token system.

Provides single-use, time-limited tokens for WhatsApp-initiated Google OAuth
flows.  Tokens are stored in Redis with a 10-minute TTL and consumed
atomically via ``GETDEL`` (Redis ≥ 6.2) to guarantee single-winner semantics.

No DB fallback — Redis is a hard dependency (Celery broker), so it is always
available in any environment that runs the application.

Requirements: 15.1
"""

from __future__ import annotations

import json
import logging
import secrets

import redis.asyncio as aioredis

from app.config import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_KEY_PREFIX = "oauth_ephemeral:"
_TTL_SECONDS = 600  # 10 minutes


# ---------------------------------------------------------------------------
# Redis helper
# ---------------------------------------------------------------------------

async def _get_redis() -> aioredis.Redis:
    """Return a short-lived async Redis client from the configured URL."""
    return aioredis.from_url(
        get_settings().REDIS_URL,
        decode_responses=True,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def create_ephemeral_token(user_id: int, service: str) -> str:
    """Generate a single-use ephemeral token and store it in Redis.

    Parameters
    ----------
    user_id:
        The database ``User.id`` that this token authorises.
    service:
        The Google service being connected (e.g. ``"calendar"``, ``"gmail"``).

    Returns
    -------
    str
        A URL-safe random token (43 characters, 256 bits of entropy).
    """
    token = secrets.token_urlsafe(32)
    key = f"{_KEY_PREFIX}{token}"
    payload = json.dumps({"user_id": user_id, "service": service})

    r = await _get_redis()
    try:
        await r.set(key, payload, ex=_TTL_SECONDS)
        logger.debug("Ephemeral token created for user_id=%s service=%s", user_id, service)
    finally:
        await r.aclose()

    return token


async def validate_ephemeral_token(token: str) -> dict | None:
    """Consume an ephemeral token atomically.

    Uses Redis ``GETDEL`` so that only the first caller to validate a given
    token receives the payload — all subsequent callers get ``None``.

    Parameters
    ----------
    token:
        The token string previously returned by :func:`create_ephemeral_token`.

    Returns
    -------
    dict | None
        ``{"user_id": int, "service": str}`` on success, or ``None`` if the
        token is expired, already consumed, or never existed.
    """
    key = f"{_KEY_PREFIX}{token}"

    r = await _get_redis()
    try:
        raw: str | None = await r.getdel(key)
    finally:
        await r.aclose()

    if raw is None:
        logger.debug("Ephemeral token not found or already consumed")
        return None

    data: dict = json.loads(raw)
    logger.debug("Ephemeral token consumed for user_id=%s service=%s", data.get("user_id"), data.get("service"))
    return data
