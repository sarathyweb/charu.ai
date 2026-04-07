"""Google Calendar read service — fetch and format today's events.

All API calls delegate through ``google_api_call`` (task 8.6) so that
token refresh, auth errors, and retryable errors are handled in one place.

Requirements: 10
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.user import User
from app.services.google_api_wrapper import google_api_call
from app.services.google_oauth_service import build_google_credentials

logger = logging.getLogger(__name__)


def _build_calendar_service(credentials: Any) -> Any:
    """Build a Google Calendar API v3 service object."""
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


async def fetch_todays_events(
    user: User,
    session: AsyncSession,
    *,
    max_retries: int | None = None,
) -> list[dict] | dict:
    """Fetch today's calendar events for *user*.

    Returns a list of raw event dicts on success, or a structured error
    dict (with an ``"error"`` key) on auth / API failure.
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
    now_local = datetime.now(tz)
    start_of_day = datetime.combine(now_local.date(), time.min, tzinfo=tz)
    end_of_day = start_of_day + timedelta(days=1)

    result = await google_api_call(
        user=user,
        credentials=credentials,
        api_callable=lambda: service.events().list(
            calendarId="primary",
            timeMin=start_of_day.isoformat(),
            timeMax=end_of_day.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            timeZone=user.timezone,
        ).execute(),
        session=session,
        max_retries=max_retries,
    )

    # Propagate structured errors from the wrapper.
    if isinstance(result, dict) and "error" in result:
        return result

    events: list[dict] = result.get("items", [])

    # Filter out cancelled events and events the user declined.
    filtered: list[dict] = []
    for event in events:
        if event.get("status") == "cancelled":
            continue
        if _user_declined(event):
            continue
        filtered.append(event)

    return filtered


def _user_declined(event: dict) -> bool:
    """Return True if the authenticated user declined this event."""
    for attendee in event.get("attendees", []):
        if attendee.get("self") and attendee.get("responseStatus") == "declined":
            return True
    return False


def format_events_for_agent(
    events: list[dict],
    user_timezone: str,
) -> str:
    """Format calendar events into a concise context string for the agent.

    Parameters
    ----------
    events:
        Raw event dicts as returned by ``fetch_todays_events``.
    user_timezone:
        IANA timezone identifier (e.g. ``"America/New_York"``).

    Returns
    -------
    A human-readable summary suitable for injection into an agent's
    system instruction or pre-call context.
    """
    if not events:
        return "No events scheduled for today."

    tz = ZoneInfo(user_timezone)
    lines: list[str] = []

    for event in events:
        start_raw = event.get("start", {})
        end_raw = event.get("end", {})
        summary = event.get("summary", "Untitled event")

        start_dt_str = start_raw.get("dateTime")
        end_dt_str = end_raw.get("dateTime")

        if start_dt_str:
            # Timed event
            start_dt = datetime.fromisoformat(start_dt_str).astimezone(tz)
            time_str = start_dt.strftime("%I:%M %p").lstrip("0")
            if end_dt_str:
                end_dt = datetime.fromisoformat(end_dt_str).astimezone(tz)
                end_time_str = end_dt.strftime("%I:%M %p").lstrip("0")
                lines.append(f"- {time_str}–{end_time_str}: {summary}")
            else:
                lines.append(f"- {time_str}: {summary}")
        else:
            # All-day event
            lines.append(f"- All day: {summary}")

    return "Today's calendar:\n" + "\n".join(lines)
