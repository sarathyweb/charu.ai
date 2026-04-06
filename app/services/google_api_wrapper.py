"""Shared Google API call wrapper for Calendar and Gmail services.

All Google API calls (Calendar read/write, Gmail read/write) delegate through
``google_api_call`` so that token refresh persistence, auth error handling, and
retryable-error backoff live in exactly one place.

Token persistence (refresh and clear) uses an independent DB session so that
commits never leak into the caller's transaction boundary.

Requirements: 10, 11, 15, 17, 18 — Design Error Handling section.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
from sqlalchemy import update
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import async_session_factory
from app.models.user import User
from app.services.google_oauth_service import encrypt_token

logger = logging.getLogger(__name__)

# Retry configuration for 429 / 5xx errors.
_MAX_RETRIES = 3
_BACKOFF_SECONDS = (1, 2, 4)

# HTTP status codes that trigger token clearing (auth failure).
_AUTH_ERROR_CODES = frozenset({401, 403})

# HTTP status codes that are retryable.
_RETRYABLE_CODES = frozenset({429, 500, 502, 503, 504})


async def _clear_google_tokens(user_id: int) -> None:
    """Clear all Google OAuth fields for *user_id* in an independent session.

    Uses a dedicated session so the commit does not affect any pending
    mutations on the caller's session.
    """
    async with async_session_factory() as session:
        stmt = (
            update(User)
            .where(User.id == user_id)
            .values(
                google_access_token_encrypted=None,
                google_refresh_token_encrypted=None,
                google_token_expiry=None,
                google_granted_scopes=None,
                updated_at=datetime.now(timezone.utc),
            )
        )
        await session.execute(stmt)
        await session.commit()
    logger.warning("Cleared Google tokens for user %s", user_id)


async def _persist_refreshed_token(
    user_id: int,
    credentials: Credentials,
) -> None:
    """Persist a newly refreshed access token + expiry in an independent session.

    Uses a dedicated session so the commit does not affect any pending
    mutations on the caller's session.
    """
    expiry = (
        credentials.expiry.replace(tzinfo=timezone.utc)
        if credentials.expiry and credentials.expiry.tzinfo is None
        else credentials.expiry
    )
    async with async_session_factory() as session:
        stmt = (
            update(User)
            .where(User.id == user_id)
            .values(
                google_access_token_encrypted=encrypt_token(credentials.token),
                google_token_expiry=expiry,
                updated_at=datetime.now(timezone.utc),
            )
        )
        await session.execute(stmt)
        await session.commit()
    logger.info("Persisted refreshed Google token for user %s", user_id)


async def google_api_call(
    user: User,
    credentials: Credentials,
    api_callable: Callable[[], Any],
    session: AsyncSession,
) -> Any:
    """Execute a Google API call with automatic token-refresh persistence and error handling.

    Parameters
    ----------
    user:
        The ``User`` row whose tokens back *credentials*.  Must have a
        valid ``id``.  The user object on the caller's session is never
        mutated by this function.
    credentials:
        A ``google.oauth2.credentials.Credentials`` built from the user's
        stored encrypted tokens (via ``build_google_credentials``).
    api_callable:
        A **zero-argument callable** that performs the actual Google API
        request.  Typically a lambda wrapping an ``execute()`` call, e.g.
        ``lambda: service.events().list(...).execute()``.
        This callable is **synchronous** — it will be run in a thread via
        ``asyncio.to_thread``.
    session:
        The caller's ``AsyncSession``.  Kept in the signature for backward
        compatibility but **no longer used for token persistence** — token
        updates use an independent session to avoid committing the caller's
        pending mutations.

    Returns
    -------
    The raw result from the Google API on success, or a structured error
    ``dict`` with an ``"error"`` key on auth / quota / server failures.
    """
    token_before = credentials.token

    for attempt in range(_MAX_RETRIES + 1):
        try:
            result = await asyncio.to_thread(api_callable)

            # Check if google-auth silently refreshed the access token.
            if credentials.token != token_before:
                await _persist_refreshed_token(user.id, credentials)

            return result

        except RefreshError as exc:
            logger.warning(
                "Google RefreshError for user %s: %s", user.id, exc,
            )
            await _clear_google_tokens(user.id)
            return {
                "error": "google_disconnected",
                "message": (
                    "Your Google account connection has expired. "
                    "Please reconnect to continue using Calendar and Gmail features."
                ),
            }

        except HttpError as exc:
            status = exc.resp.status

            if status in _AUTH_ERROR_CODES:
                logger.warning(
                    "Google HttpError %s for user %s: %s",
                    status, user.id, exc,
                )
                await _clear_google_tokens(user.id)
                return {
                    "error": "google_disconnected",
                    "message": (
                        "Google returned an authorization error. "
                        "Please reconnect your Google account."
                    ),
                }

            if status in _RETRYABLE_CODES:
                if attempt < _MAX_RETRIES:
                    delay = _BACKOFF_SECONDS[attempt]
                    logger.info(
                        "Google HttpError %s for user %s, retrying in %ss "
                        "(attempt %d/%d)",
                        status, user.id, delay, attempt + 1, _MAX_RETRIES,
                    )
                    await asyncio.sleep(delay)
                    continue

                # Exhausted retries.
                error_type = (
                    "google_rate_limited" if status == 429
                    else "google_server_error"
                )
                logger.error(
                    "Google HttpError %s for user %s after %d retries",
                    status, user.id, _MAX_RETRIES,
                )
                return {
                    "error": error_type,
                    "message": (
                        "Google API is temporarily unavailable. "
                        "Please try again in a few minutes."
                    ),
                }

            # Non-retryable, non-auth HttpError — propagate.
            raise

    # Should be unreachable, but satisfy type checkers.
    return {  # pragma: no cover
        "error": "google_server_error",
        "message": "Unexpected retry exhaustion.",
    }
