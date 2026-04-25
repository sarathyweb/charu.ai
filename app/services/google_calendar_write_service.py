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


def _parse_iso_datetime(value: str, field_name: str) -> datetime:
    """Parse an ISO/RFC3339 datetime string for validation."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a valid ISO 8601 datetime.") from exc


def _validate_time_range(start_iso: str, end_iso: str) -> None:
    """Validate that start and end datetimes parse and end after start."""
    start_dt = _parse_iso_datetime(start_iso, "start_iso")
    end_dt = _parse_iso_datetime(end_iso, "end_iso")
    try:
        if end_dt <= start_dt:
            raise ValueError("end_iso must be after start_iso.")
    except TypeError as exc:
        raise ValueError(
            "start_iso and end_iso must both include timezone offsets or both omit them."
        ) from exc


def _clean_required_text(value: str, field_name: str) -> str:
    """Return stripped text or raise a user-facing validation error."""
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field_name} cannot be empty.")
    return clean


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
    # Truncate to 32 chars: plenty unique and well within the 5-1024 range.
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


async def create_event(
    user: User,
    session: AsyncSession,
    *,
    summary: str,
    start_iso: str,
    end_iso: str,
    description: str | None = None,
) -> dict:
    """Create a general Google Calendar event.

    This is distinct from ``create_time_block``: it lets Google generate the
    event ID and does not use Charu task-block metadata.
    """
    if not user.timezone:
        return {"error": "no_timezone", "message": "User timezone is not set."}

    clean_summary = _clean_required_text(summary, "summary")
    _validate_time_range(start_iso, end_iso)

    credentials = build_google_credentials(
        access_token_encrypted=user.google_access_token_encrypted,
        refresh_token_encrypted=user.google_refresh_token_encrypted,
        token_expiry=user.google_token_expiry,
    )
    service = _build_calendar_service(credentials)

    event_body: dict[str, Any] = {
        "summary": clean_summary,
        "start": {"dateTime": start_iso, "timeZone": user.timezone},
        "end": {"dateTime": end_iso, "timeZone": user.timezone},
        "source": {"title": "Charu AI", "url": "https://charu.ai"},
    }
    clean_description = (description or "").strip()
    if clean_description:
        event_body["description"] = clean_description

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

    if isinstance(result, dict) and "error" in result:
        return result

    return _event_response(result, status="created")


async def update_event(
    user: User,
    session: AsyncSession,
    *,
    event_id: str,
    summary: str | None = None,
    start_iso: str | None = None,
    end_iso: str | None = None,
    description: str | None = None,
) -> dict:
    """Patch an existing Google Calendar event."""
    if not user.timezone:
        return {"error": "no_timezone", "message": "User timezone is not set."}

    clean_event_id = _clean_required_text(event_id, "event_id")
    if (
        summary is None
        and start_iso is None
        and end_iso is None
        and description is None
    ):
        raise ValueError(
            "At least one of summary, start_iso, end_iso, or description "
            "must be provided."
        )

    event_body: dict[str, Any] = {}
    if summary is not None:
        event_body["summary"] = _clean_required_text(summary, "summary")
    if description is not None:
        event_body["description"] = description.strip()
    if start_iso is not None:
        _parse_iso_datetime(start_iso, "start_iso")
        event_body["start"] = {"dateTime": start_iso, "timeZone": user.timezone}
    if end_iso is not None:
        _parse_iso_datetime(end_iso, "end_iso")
        event_body["end"] = {"dateTime": end_iso, "timeZone": user.timezone}
    if start_iso is not None and end_iso is not None:
        _validate_time_range(start_iso, end_iso)

    credentials = build_google_credentials(
        access_token_encrypted=user.google_access_token_encrypted,
        refresh_token_encrypted=user.google_refresh_token_encrypted,
        token_expiry=user.google_token_expiry,
    )
    service = _build_calendar_service(credentials)

    result = await google_api_call(
        user=user,
        credentials=credentials,
        api_callable=lambda: service.events().patch(
            calendarId="primary",
            eventId=clean_event_id,
            body=event_body,
            sendUpdates="none",
        ).execute(),
        session=session,
    )

    if isinstance(result, dict) and "error" in result:
        return result

    return _event_response(result, status="updated")


async def delete_event(
    user: User,
    session: AsyncSession,
    *,
    event_id: str,
) -> dict:
    """Delete an event from the user's primary Google Calendar."""
    if not user.timezone:
        return {"error": "no_timezone", "message": "User timezone is not set."}

    clean_event_id = _clean_required_text(event_id, "event_id")

    credentials = build_google_credentials(
        access_token_encrypted=user.google_access_token_encrypted,
        refresh_token_encrypted=user.google_refresh_token_encrypted,
        token_expiry=user.google_token_expiry,
    )
    service = _build_calendar_service(credentials)

    result = await google_api_call(
        user=user,
        credentials=credentials,
        api_callable=lambda: service.events().delete(
            calendarId="primary",
            eventId=clean_event_id,
            sendUpdates="none",
        ).execute(),
        session=session,
    )

    if isinstance(result, dict) and "error" in result:
        return result

    return {"status": "deleted", "event_id": clean_event_id}


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
