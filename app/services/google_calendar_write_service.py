"""Google Calendar write service — find gaps and create time blocks.

All API calls delegate through ``google_api_call`` (task 8.6) so that
token refresh, auth errors, and retryable errors are handled in one place.

Requirements: 17
"""

from __future__ import annotations

import base64
import hashlib
import logging
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.user import User
from app.services.google_api_wrapper import google_api_call
from app.services.google_oauth_service import build_google_credentials

logger = logging.getLogger(__name__)


def _build_calendar_service(credentials: Any) -> Any:
    """Build a Google Calendar API v3 service object."""
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def generate_calendar_event_id(
    user_id: int,
    task_title: str,
    date_str: str,
) -> str:
    """Generate a deterministic, base32hex-compatible event ID.

    Google Calendar event IDs must use only base32hex characters
    (lowercase ``a-v`` and digits ``0-9``) and be 5–1024 characters long.

    The ID is derived from ``user_id + task_title + date``, ensuring the
    same task on the same day always maps to the same event ID.
    """
    raw = f"charuai:{user_id}:{task_title}:{date_str}"
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    b32 = base64.b32hexencode(digest).decode().lower().rstrip("=")
    # Truncate to 32 chars — plenty unique and well within the 5–1024 range.
    return b32[:32]


async def find_available_gaps(
    user: User,
    session: AsyncSession,
    *,
    min_duration_minutes: int = 30,
    end_hour: int = 19,
) -> list[dict] | dict:
    """Find available time gaps in the user's calendar for today.

    Uses ``freebusy.query`` to discover busy periods, then computes the
    gaps between them.

    Parameters
    ----------
    user:
        The authenticated user with Google Calendar connected.
    session:
        Active DB session for token-refresh persistence.
    min_duration_minutes:
        Minimum gap length (in minutes) to include in results.
    end_hour:
        Hour (in user's local time, 24h format) at which to stop
        looking for gaps.  Defaults to 19 (7 PM).

    Returns
    -------
    A list of gap dicts ``{start, end, duration_minutes}`` on success,
    or a structured error dict (with an ``"error"`` key) on failure.
    """
    if not user.timezone:
        return {"error": "no_timezone", "message": "User timezone is not set."}

    credentials = build_google_credentials(
        access_token_encrypted=user.google_access_token_encrypted,
        refresh_token_encrypted=user.google_refresh_token_encrypted,
        token_expiry=user.google_token_expiry,
    )

    service = _build_calendar_service(credentials)

    tz = ZoneInfo(user.timezone)
    now_utc = datetime.now(ZoneInfo("UTC"))
    now_local = now_utc.astimezone(tz)

    end_of_window = datetime.combine(now_local.date(), time(end_hour, 0), tzinfo=tz)
    if now_local >= end_of_window:
        return []  # No more working hours today.

    body = {
        "timeMin": now_utc.isoformat(),
        "timeMax": end_of_window.astimezone(ZoneInfo("UTC")).isoformat(),
        "timeZone": user.timezone,
        "items": [{"id": "primary"}],
    }

    result = await google_api_call(
        user=user,
        credentials=credentials,
        api_callable=lambda: service.freebusy().query(body=body).execute(),
        session=session,
    )

    # Propagate structured errors from the wrapper.
    if isinstance(result, dict) and "error" in result:
        return result

    busy_periods: list[dict] = result.get("calendars", {}).get("primary", {}).get("busy", [])

    gaps: list[dict] = []
    current_start = now_utc

    for busy in busy_periods:
        busy_start = datetime.fromisoformat(busy["start"])
        busy_end = datetime.fromisoformat(busy["end"])

        if busy_start > current_start:
            gap_minutes = (busy_start - current_start).total_seconds() / 60
            if gap_minutes >= min_duration_minutes:
                gaps.append({
                    "start": current_start.isoformat(),
                    "end": busy_start.isoformat(),
                    "duration_minutes": int(gap_minutes),
                })

        current_start = max(current_start, busy_end)

    # Gap after the last busy period until end of window.
    end_of_window_utc = end_of_window.astimezone(ZoneInfo("UTC"))
    if current_start < end_of_window_utc:
        gap_minutes = (end_of_window_utc - current_start).total_seconds() / 60
        if gap_minutes >= min_duration_minutes:
            gaps.append({
                "start": current_start.isoformat(),
                "end": end_of_window_utc.isoformat(),
                "duration_minutes": int(gap_minutes),
            })

    return gaps


async def create_time_block(
    user: User,
    session: AsyncSession,
    *,
    task_title: str,
    start_iso: str,
    end_iso: str,
    task_id: str | None = None,
) -> dict:
    """Create a calendar time block for a task.  Idempotent via deterministic event ID.

    If an event with the same deterministic ID already exists, the API
    returns ``409 Conflict`` which is treated as idempotent success — the
    existing event details are returned.

    Parameters
    ----------
    user:
        The authenticated user with Google Calendar connected.
    session:
        Active DB session for token-refresh persistence.
    task_title:
        The task name used as the event summary.
    start_iso:
        RFC 3339 start datetime string.
    end_iso:
        RFC 3339 end datetime string.
    task_id:
        Optional external task identifier stored in extended properties.

    Returns
    -------
    A dict with ``status`` (``"created"`` or ``"already_exists"``),
    ``event_id``, ``summary``, ``start``, ``end``, and ``html_link``;
    or a structured error dict on failure.
    """
    if not user.timezone:
        return {"error": "no_timezone", "message": "User timezone is not set."}

    credentials = build_google_credentials(
        access_token_encrypted=user.google_access_token_encrypted,
        refresh_token_encrypted=user.google_refresh_token_encrypted,
        token_expiry=user.google_token_expiry,
    )

    service = _build_calendar_service(credentials)

    date_str = datetime.fromisoformat(start_iso).strftime("%Y-%m-%d")
    event_id = generate_calendar_event_id(user.id, task_title, date_str)

    extended_private: dict[str, str] = {
        "charuai": "time_block",
        "charuai_user_id": str(user.id),
    }
    if task_id is not None:
        extended_private["charuai_task_id"] = str(task_id)

    event_body = {
        "id": event_id,
        "summary": f"🎯 {task_title}",
        "description": "Time block created by Charu AI during your accountability call.",
        "start": {
            "dateTime": start_iso,
            "timeZone": user.timezone,
        },
        "end": {
            "dateTime": end_iso,
            "timeZone": user.timezone,
        },
        "transparency": "opaque",
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": 5}],
        },
        "extendedProperties": {"private": extended_private},
        "source": {"title": "Charu AI", "url": "https://charu.ai"},
    }

    # --- Attempt insert via the shared wrapper ---
    # The wrapper re-raises non-retryable, non-auth HttpErrors (including 409).
    # We catch 409 here and treat it as idempotent success.
    try:
        result = await google_api_call(
            user=user,
            credentials=credentials,
            api_callable=lambda: service.events().insert(
                calendarId="primary",
                body=event_body,
                sendUpdates="none",
            ).execute(),
            session=session,
        )
    except HttpError as exc:
        if exc.resp.status == 409:
            # Event already exists — idempotent success.
            return await _handle_conflict(user, credentials, service, event_id, session)
        raise

    # Propagate structured errors from the wrapper (auth, rate-limit, etc.).
    if isinstance(result, dict) and "error" in result:
        return result

    # Success — event was created.
    return _event_response(result, status="created")


async def _handle_conflict(
    user: User,
    credentials: Any,
    service: Any,
    event_id: str,
    session: AsyncSession,
) -> dict:
    """Fetch the existing event after a 409 Conflict."""
    existing = await google_api_call(
        user=user,
        credentials=credentials,
        api_callable=lambda: service.events().get(
            calendarId="primary",
            eventId=event_id,
        ).execute(),
        session=session,
    )
    if isinstance(existing, dict) and "error" in existing:
        return existing
    return _event_response(existing, status="already_exists")


def _event_response(event: dict, *, status: str) -> dict:
    """Normalise a Calendar API event resource into our return format."""
    start = event.get("start", {}).get("dateTime", event.get("start", {}).get("date", ""))
    end = event.get("end", {}).get("dateTime", event.get("end", {}).get("date", ""))
    return {
        "status": status,
        "event_id": event.get("id", ""),
        "html_link": event.get("htmlLink", ""),
        "summary": event.get("summary", ""),
        "start": start,
        "end": end,
    }
