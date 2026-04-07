"""Google OAuth 2.0 endpoints for WhatsApp-initiated authorization.

GET /auth/google/start    — validate ephemeral token, redirect to Google consent
GET /auth/google/callback — exchange code for tokens, encrypt and store

The flow:
1. Agent generates an ephemeral token via ``create_ephemeral_token``
2. User clicks the link in WhatsApp → ``/auth/google/start?token=...``
3. Server validates the ephemeral token, stores CSRF state in Redis,
   redirects to Google consent screen
4. Google redirects back to ``/auth/google/callback?code=...&state=...``
5. Server validates CSRF state, exchanges code for tokens, encrypts
   and persists them on the User record

Requirements: 15
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from datetime import datetime, timezone

import redis.asyncio as aioredis
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import get_settings
from app.db import async_session_factory
from app.services.ephemeral_token_service import validate_ephemeral_token
from app.services.google_oauth_service import encrypt_token

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVICE_SCOPES: dict[str, list[str]] = {
    "calendar": ["https://www.googleapis.com/auth/calendar"],
    "gmail": ["https://www.googleapis.com/auth/gmail.modify"],
}

_OAUTH_STATE_PREFIX = "oauth_state:"
_OAUTH_STATE_TTL = 600  # 10 minutes


# ---------------------------------------------------------------------------
# Redis helper
# ---------------------------------------------------------------------------

async def _get_redis() -> aioredis.Redis:
    return aioredis.from_url(get_settings().REDIS_URL, decode_responses=True)


# ---------------------------------------------------------------------------
# CSRF state helpers (Redis-backed, single-use)
# ---------------------------------------------------------------------------

async def _store_oauth_state(state: str, payload: dict) -> None:
    """Store OAuth state in Redis with TTL for CSRF validation."""
    r = await _get_redis()
    try:
        await r.set(
            f"{_OAUTH_STATE_PREFIX}{state}",
            json.dumps(payload),
            ex=_OAUTH_STATE_TTL,
        )
    finally:
        await r.aclose()


async def _consume_oauth_state(state: str) -> dict | None:
    """Atomically consume an OAuth state (single-use via GETDEL)."""
    r = await _get_redis()
    try:
        raw: str | None = await r.getdel(f"{_OAUTH_STATE_PREFIX}{state}")
    finally:
        await r.aclose()
    if raw is None:
        return None
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Flow builder (sync — runs in thread)
# ---------------------------------------------------------------------------

def _build_flow(scopes: list[str], state: str | None = None):
    """Build a google_auth_oauthlib Flow from app settings."""
    import google_auth_oauthlib.flow

    settings = get_settings()
    client_config = {
        "web": {
            "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    flow = google_auth_oauthlib.flow.Flow.from_client_config(
        client_config=client_config,
        scopes=scopes,
        state=state,
    )
    flow.redirect_uri = settings.GOOGLE_OAUTH_REDIRECT_URI
    return flow


# ---------------------------------------------------------------------------
# GET /auth/google/start
# ---------------------------------------------------------------------------

@router.get("/auth/google/start")
async def google_oauth_start(
    token: str = Query(..., description="Ephemeral token from WhatsApp link"),
):
    """Validate ephemeral token and redirect to Google consent screen.

    The ``service`` (calendar / gmail) is encoded inside the ephemeral token
    payload — the agent sets it when generating the link.
    """
    # 1. Validate and consume the ephemeral token (single-use)
    data = await validate_ephemeral_token(token)
    if data is None:
        raise HTTPException(
            status_code=400,
            detail="This link has expired or has already been used. "
            "Please ask Charu for a new link.",
        )

    user_id: int = data["user_id"]
    service: str = data["service"]

    scopes = SERVICE_SCOPES.get(service)
    if not scopes:
        raise HTTPException(status_code=400, detail=f"Unknown service: {service}")

    # 2. Build the OAuth flow and generate the authorization URL
    flow = _build_flow(scopes)

    # Generate a random nonce for CSRF protection
    nonce = secrets.token_urlsafe(16)
    state = f"{user_id}:{service}:{nonce}"

    # Allow OAUTHLIB_INSECURE_TRANSPORT for local dev (http redirect URIs)
    if get_settings().GOOGLE_OAUTH_REDIRECT_URI.startswith("http://"):
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

    # Suppress scope mismatch warnings (Google may return equivalent scopes)
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",  # Always get a refresh token
        state=state,
    )

    # Capture the PKCE code_verifier generated by the flow
    code_verifier = flow.code_verifier

    # 3. Store state in Redis for CSRF validation in the callback
    await _store_oauth_state(state, {
        "user_id": user_id,
        "service": service,
        "code_verifier": code_verifier,
    })

    logger.info(
        "OAuth start: user_id=%s service=%s — redirecting to Google consent",
        user_id,
        service,
    )
    return RedirectResponse(url=authorization_url, status_code=302)


# ---------------------------------------------------------------------------
# GET /auth/google/callback
# ---------------------------------------------------------------------------

_SUCCESS_HTML = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Connected!</title>
<style>
  body {{ font-family: -apple-system, sans-serif; display: flex;
         justify-content: center; align-items: center; height: 100vh;
         margin: 0; background: #f8f9fa; }}
  .card {{ text-align: center; padding: 2rem; background: white;
          border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,.1); }}
  h1 {{ color: #2d7d46; margin-bottom: .5rem; }}
  p {{ color: #555; }}
</style></head>
<body><div class="card">
  <h1>✅ Connected!</h1>
  <p>Your Google {service} is now linked to Charu AI.</p>
  <p>You can close this tab and return to WhatsApp.</p>
</div></body></html>"""

_ERROR_HTML = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Connection Failed</title>
<style>
  body {{ font-family: -apple-system, sans-serif; display: flex;
         justify-content: center; align-items: center; height: 100vh;
         margin: 0; background: #f8f9fa; }}
  .card {{ text-align: center; padding: 2rem; background: white;
          border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,.1); }}
  h1 {{ color: #d32f2f; margin-bottom: .5rem; }}
  p {{ color: #555; }}
</style></head>
<body><div class="card">
  <h1>❌ Connection Failed</h1>
  <p>{detail}</p>
  <p>Please return to WhatsApp and ask Charu for a new link.</p>
</div></body></html>"""


@router.get("/auth/google/callback")
async def google_oauth_callback(
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
    scope: str = Query(None),
):
    """Handle Google OAuth callback — exchange code for tokens.

    On success, encrypts tokens and stores them on the User record.
    Returns a user-friendly HTML page (the user is in a browser).
    """
    # Handle user-denied consent
    if error:
        logger.warning("OAuth callback received error: %s", error)
        return HTMLResponse(
            _ERROR_HTML.format(detail="Authorization was denied or cancelled."),
            status_code=200,
        )

    if not code or not state:
        return HTMLResponse(
            _ERROR_HTML.format(detail="Missing authorization code or state."),
            status_code=400,
        )

    # 1. Validate CSRF state (single-use via Redis GETDEL)
    state_data = await _consume_oauth_state(state)
    if state_data is None:
        return HTMLResponse(
            _ERROR_HTML.format(
                detail="This link has expired or has already been used."
            ),
            status_code=400,
        )

    user_id: int = state_data["user_id"]
    service: str = state_data["service"]
    code_verifier: str | None = state_data.get("code_verifier")
    scopes = SERVICE_SCOPES.get(service, [])

    # 2. Exchange authorization code for tokens (sync — run in thread)
    try:
        credentials = await asyncio.to_thread(
            _exchange_code, code, state, scopes, code_verifier
        )
    except Exception:
        logger.exception("OAuth token exchange failed for user_id=%s", user_id)
        return HTMLResponse(
            _ERROR_HTML.format(
                detail="Failed to complete authorization. Please try again."
            ),
            status_code=500,
        )

    # 3. Encrypt tokens
    access_encrypted = encrypt_token(credentials.token) if credentials.token else None
    refresh_encrypted = (
        encrypt_token(credentials.refresh_token)
        if credentials.refresh_token
        else None
    )

    # 4. Merge granted scopes (incremental authorization)
    granted_scopes = set(credentials.granted_scopes or [])
    # Also include the scopes from the scope query param if present
    if scope:
        granted_scopes.update(scope.split())

    # 5. Persist to User record
    from app.models.user import User
    from sqlmodel import select

    async with async_session_factory() as session:
        result = await session.exec(select(User).where(User.id == user_id))
        user = result.first()
        if user is None:
            logger.error("OAuth callback: user_id=%s not found in DB", user_id)
            return HTMLResponse(
                _ERROR_HTML.format(detail="User account not found."),
                status_code=404,
            )

        # Merge with existing granted scopes (incremental auth)
        existing_scopes = set(
            (user.google_granted_scopes or "").split()
        )
        merged_scopes = existing_scopes | granted_scopes

        if access_encrypted:
            user.google_access_token_encrypted = access_encrypted
        if refresh_encrypted:
            user.google_refresh_token_encrypted = refresh_encrypted
        user.google_token_expiry = credentials.expiry
        user.google_granted_scopes = " ".join(sorted(merged_scopes))

        session.add(user)
        await session.commit()

    logger.info(
        "OAuth callback: user_id=%s service=%s — tokens stored, scopes=%s",
        user_id,
        service,
        " ".join(sorted(merged_scopes)),
    )

    service_display = service.title()
    return HTMLResponse(
        _SUCCESS_HTML.format(service=service_display),
        status_code=200,
    )


# ---------------------------------------------------------------------------
# Sync helper for token exchange (runs in asyncio.to_thread)
# ---------------------------------------------------------------------------

def _exchange_code(code: str, state: str, scopes: list[str], code_verifier: str | None = None):
    """Exchange authorization code for credentials. Blocking — call via to_thread."""
    # Allow http redirect URIs in dev
    if get_settings().GOOGLE_OAUTH_REDIRECT_URI.startswith("http://"):
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

    flow = _build_flow(scopes, state=state)
    flow.fetch_token(code=code, code_verifier=code_verifier)
    return flow.credentials
