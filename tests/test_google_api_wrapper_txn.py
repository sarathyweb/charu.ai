"""Regression tests for google_api_wrapper transaction boundary isolation.

Verifies that token refresh and token clearing use an independent DB session,
so the caller's session is never committed as a side-effect.

The tests verify this by checking that session.commit() is never called on
the caller's session during google_api_call, and that the internal helpers
use their own session via async_session_factory.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from httplib2 import Response
from googleapiclient.errors import HttpError

from app.models.user import User
from app.services.google_api_wrapper import (
    _clear_google_tokens,
    _persist_refreshed_token,
    google_api_call,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_credentials(token: str = "access_tok_1") -> Credentials:
    return Credentials(
        token=token,
        refresh_token="refresh_tok",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="test_client_id",
        client_secret="test_client_secret",
    )


def _make_user(user_id: int = 42) -> MagicMock:
    """Create a mock User object for testing."""
    user = MagicMock(spec=User)
    user.id = user_id
    user.phone = "+14155550042"
    user.name = "Test User"
    return user


# ---------------------------------------------------------------------------
# Tests: caller session is never committed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_refresh_does_not_commit_caller_session():
    """When google-auth silently refreshes the token, the wrapper must NOT
    call commit() on the caller's session."""
    user = _make_user()
    creds = _make_credentials(token="old_token")

    caller_session = AsyncMock()

    def fake_api_call():
        creds.token = "new_refreshed_token"
        creds.expiry = datetime(2099, 1, 1, tzinfo=timezone.utc)
        return {"events": []}

    async def mock_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    # Mock the internal _persist_refreshed_token to avoid real DB calls
    with (
        patch("app.services.google_api_wrapper.asyncio.to_thread", side_effect=mock_to_thread),
        patch("app.services.google_api_wrapper._persist_refreshed_token", new_callable=AsyncMock),
    ):
        result = await google_api_call(
            user=user,
            credentials=creds,
            api_callable=fake_api_call,
            session=caller_session,
        )

    assert result == {"events": []}
    # The caller's session must never have commit() called
    caller_session.commit.assert_not_awaited()
    caller_session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_token_clear_on_refresh_error_does_not_commit_caller_session():
    """When a RefreshError triggers token clearing, the caller's session
    must not be committed."""
    user = _make_user()
    creds = _make_credentials()
    caller_session = AsyncMock()

    async def mock_to_thread(fn, *args, **kwargs):
        raise RefreshError("token revoked")

    with (
        patch("app.services.google_api_wrapper.asyncio.to_thread", side_effect=mock_to_thread),
        patch("app.services.google_api_wrapper._clear_google_tokens", new_callable=AsyncMock),
    ):
        result = await google_api_call(
            user=user,
            credentials=creds,
            api_callable=lambda: None,
            session=caller_session,
        )

    assert result["error"] == "google_disconnected"
    caller_session.commit.assert_not_awaited()
    caller_session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_token_clear_on_401_does_not_commit_caller_session():
    """When a 401 HttpError triggers token clearing, the caller's session
    must not be committed."""
    user = _make_user()
    creds = _make_credentials()
    caller_session = AsyncMock()

    resp = Response({"status": "401"})
    http_err = HttpError(resp, b"Unauthorized")

    async def mock_to_thread(fn, *args, **kwargs):
        raise http_err

    with (
        patch("app.services.google_api_wrapper.asyncio.to_thread", side_effect=mock_to_thread),
        patch("app.services.google_api_wrapper._clear_google_tokens", new_callable=AsyncMock),
    ):
        result = await google_api_call(
            user=user,
            credentials=creds,
            api_callable=lambda: None,
            session=caller_session,
        )

    assert result["error"] == "google_disconnected"
    caller_session.commit.assert_not_awaited()
    caller_session.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: internal helpers use independent session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_tokens_uses_independent_session():
    """_clear_google_tokens opens its own session via async_session_factory,
    not the caller's session."""
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock(return_value=mock_session)

    with patch("app.services.google_api_wrapper.async_session_factory", new=mock_factory):
        await _clear_google_tokens(user_id=42)

    # The factory was called (independent session created)
    mock_factory.assert_called_once()
    # execute + commit were called on the independent session
    mock_session.execute.assert_awaited_once()
    mock_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_persist_refreshed_token_uses_independent_session():
    """_persist_refreshed_token opens its own session via async_session_factory."""
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock(return_value=mock_session)

    creds = _make_credentials(token="refreshed_token")
    creds.expiry = datetime(2099, 1, 1, tzinfo=timezone.utc)

    with patch("app.services.google_api_wrapper.async_session_factory", new=mock_factory):
        with patch("app.services.google_api_wrapper.encrypt_token", return_value="encrypted"):
            await _persist_refreshed_token(user_id=42, credentials=creds)

    mock_factory.assert_called_once()
    mock_session.execute.assert_awaited_once()
    mock_session.commit.assert_awaited_once()
