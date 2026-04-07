"""Redis-backed ephemeral OAuth token system.

Provides time-limited tokens for WhatsApp-initiated Google OAuth flows.
Tokens are stored in Redis with a 10-minute TTL and allow a small number
of uses (default 3) to tolerate link-preview crawlers (WhatsApp, Facebook)
that prefetch URLs before the real user clicks.

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
_USES_PREFIX = "oauth_ephemeral_uses:"
_TTL_SECONDS = 600  # 10 minutes
_MAX_USES = 3  # tolerate link-preview crawlers


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

    uses_key = f"{_USES_PREFIX}{token}"

    r = None
    try:
        r = await _get_redis()
        async with r.pipeline(transaction=True) as pipe:
            pipe.set(key, payload, ex=_TTL_SECONDS)
            pipe.set(uses_key, _MAX_USES, ex=_TTL_SECONDS)
            await pipe.execute()
        logger.debug("Ephemeral token created for user_id=%s service=%s", user_id, service)
    finally:
        if r is not None:
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
    uses_key = f"{_USES_PREFIX}{token}"

    r = None
    try:
        r = await _get_redis()
        # Atomically decrement the use counter
        remaining = await r.decr(uses_key)
        if remaining < 0:
            # Counter exhausted or key missing (DECR on missing key gives -1).
            # Delete the junk key that DECR auto-created so it doesn't linger
            # in Redis with no TTL.
            await r.delete(uses_key)
            logger.debug("Ephemeral token exhausted or not found")
            return None

        raw: str | None = await r.get(key)
    finally:
        if r is not None:
            await r.aclose()

    if raw is None:
        logger.debug("Ephemeral token payload expired or not found")
        return None

    data: dict = json.loads(raw)

    # Clean up both keys after last use
    if remaining == 0:
        try:
            r = await _get_redis()
            await r.delete(key, uses_key)
        finally:
            if r is not None:
                await r.aclose()

    logger.debug(
        "Ephemeral token validated for user_id=%s service=%s (uses_remaining=%d)",
        data.get("user_id"), data.get("service"), remaining,
    )
    return data
