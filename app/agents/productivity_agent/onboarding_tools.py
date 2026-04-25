"""ADK FunctionTool wrappers for onboarding.

Thin wrappers that resolve user identity from ToolContext session state
and delegate to service-layer methods.  Each tool gets its own DB session
via async_session_factory.

These functions are added directly to the relevant onboarding sub-agent's
``tools`` list — ADK auto-wraps them as FunctionTool instances.

Requirements: 1, 2, 8, 8.3, 8.4, 15
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timezone
from typing import TYPE_CHECKING, Any, Dict, Optional

from google.adk.tools import ToolContext
from google.adk.tools.base_tool import BaseTool

from app.db import async_session_factory
from app.services.user_service import UserService

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.models.user import User

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# before_tool_callback guard for save_user_name
# ---------------------------------------------------------------------------

def guard_save_user_name(
    tool: BaseTool, args: Dict[str, Any], tool_context: ToolContext
) -> Optional[Dict]:
    """Prevent save_user_name from executing if name is already set.

    Returning a dict skips the tool function entirely — the returned dict
    is used as the tool result.  This is an optimisation that avoids an
    unnecessary LLM tool call round-trip when the name is already persisted.
    """
    if tool.name == "save_user_name":
        existing = tool_context.state.get("user:name")
        if existing:
            return {"status": "already_saved", "name": existing}
    return None  # Allow all other tools to proceed


# ---------------------------------------------------------------------------
# Tool: save_user_name
# ---------------------------------------------------------------------------

async def save_user_name(name: str, tool_context: ToolContext) -> dict:
    """Save the user's name. Call this when the user tells you their name.

    Args:
        name: The user's display name.
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return {"error": "No phone number in session state."}

    # Idempotency: skip if already set (Property 2)
    existing = tool_context.state.get("user:name")
    if existing:
        return {"status": "already_saved", "name": existing}

    try:
        async with async_session_factory() as session:
            svc = UserService(session)
            user = await svc.get_by_phone(phone)
            if not user:
                return {"error": "User not found."}
            user.name = name
            session.add(user)
            await session.commit()
    except Exception:
        logger.exception("DB error saving user name for phone=%s", phone)
        return {"error": "Failed to save name. Please try again."}

    # Write-through: update session state only after DB success (Property 13)
    tool_context.state["user:name"] = name
    return {"status": "saved", "name": name}


# ---------------------------------------------------------------------------
# Tool: infer_timezone_from_phone
# ---------------------------------------------------------------------------

async def infer_timezone_from_phone(tool_context: ToolContext) -> dict:
    """Infer the user's timezone from their phone number country code.

    Call this at the start of the timezone collection step to get a
    suggested timezone based on the user's phone number. Present the
    suggestion to the user for confirmation.
    """
    import phonenumbers
    from phonenumbers import timezone as pn_timezone

    phone = tool_context.state.get("phone")
    if not phone:
        return {"error": "No phone number in session state."}

    # Common legacy IANA aliases → modern canonical names
    _LEGACY_TZ = {
        "Asia/Calcutta": "Asia/Kolkata",
        "US/Eastern": "America/New_York",
        "US/Central": "America/Chicago",
        "US/Mountain": "America/Denver",
        "US/Pacific": "America/Los_Angeles",
        "Pacific/Samoa": "Pacific/Pago_Pago",
    }

    try:
        parsed = phonenumbers.parse(phone)
        timezones = pn_timezone.time_zones_for_number(parsed)
        if timezones:
            canonicalized = sorted(
                {_LEGACY_TZ.get(tz, tz) for tz in timezones}
            )
            suggested = canonicalized[0]
            return {
                "suggested_timezone": suggested,
                "all_timezones": canonicalized[:5],
                "country_code": f"+{parsed.country_code}",
            }
        return {"suggested_timezone": None, "message": "Could not determine timezone from phone number."}
    except Exception:
        return {"suggested_timezone": None, "message": "Could not parse phone number."}


# ---------------------------------------------------------------------------
# Tool: save_user_timezone
# ---------------------------------------------------------------------------

async def save_user_timezone(timezone_str: str, tool_context: ToolContext) -> dict:
    """Save the user's timezone. Call this when the user tells you their timezone.

    Args:
        timezone_str: IANA timezone identifier (e.g. America/New_York, Asia/Kolkata).
    """
    from zoneinfo import available_timezones

    phone = tool_context.state.get("phone")
    if not phone:
        return {"error": "No phone number in session state."}

    if timezone_str not in available_timezones():
        return {"error": f"Invalid timezone: {timezone_str}. Use IANA format like America/New_York."}

    try:
        async with async_session_factory() as session:
            svc = UserService(session)
            user = await svc.get_by_phone(phone)
            if not user:
                return {"error": "User not found."}

            old_tz = user.timezone
            user.timezone = timezone_str
            session.add(user)

            # If timezone changed and user has completed onboarding (meaning
            # calls are already scheduled), hard-delete future planned entries
            # and rematerialize with the new timezone — all in a single
            # transaction (Property 44).  During onboarding, calls haven't
            # been scheduled yet, so skip this — complete_onboarding handles
            # the initial materialization.
            if old_tz and old_tz != timezone_str and user.onboarding_complete:
                from app.models.enums import WindowType
                from app.services.call_window_service import CallWindowService

                cw_svc = CallWindowService(session)
                for wt in WindowType:
                    await cw_svc._hard_delete_future_planned(user.id, wt.value)

                await _rematerialize_future_calls(session, user)

            await session.commit()
    except Exception:
        logger.exception("DB error saving timezone for phone=%s", phone)
        return {"error": "Failed to save timezone. Please try again."}

    # Write-through: update session state only after DB success (Property 13)
    tool_context.state["user:timezone"] = timezone_str
    return {"status": "saved", "timezone": timezone_str}


# ---------------------------------------------------------------------------
# Tool: save_call_window
# ---------------------------------------------------------------------------

async def save_call_window(
    window_type: str,
    start_time: str,
    end_time: str,
    tool_context: ToolContext,
) -> dict:
    """Save a call window preference.

    Args:
        window_type: One of: morning, afternoon, evening.
        start_time: Window start in HH:MM format (e.g. 07:00).
        end_time: Window end in HH:MM format (e.g. 08:00).
    """
    from app.services.call_window_service import CallWindowService

    phone = tool_context.state.get("phone")
    if not phone:
        return {"error": "No phone number in session state."}

    # Validate window_type
    valid_types = {"morning", "afternoon", "evening"}
    if window_type not in valid_types:
        return {"error": f"Invalid window_type. Must be one of: {', '.join(valid_types)}"}

    # Parse times
    try:
        start = time.fromisoformat(start_time)
        end = time.fromisoformat(end_time)
    except ValueError:
        return {"error": "Times must be in HH:MM format (e.g. 07:00)."}

    # Validate: no cross-midnight (Property 3)
    start_minutes = start.hour * 60 + start.minute
    end_minutes = end.hour * 60 + end.minute
    if end_minutes <= start_minutes:
        return {"error": "End time must be after start time (no cross-midnight windows)."}

    # Validate: ≥20 min wide (Property 3)
    if (end_minutes - start_minutes) < 20:
        return {"error": "Call window must be at least 20 minutes wide."}

    try:
        async with async_session_factory() as session:
            svc = UserService(session)
            user = await svc.get_by_phone(phone)
            if not user:
                return {"error": "User not found."}
            if not user.timezone:
                return {"error": "Please set your timezone first."}

            cw_svc = CallWindowService(session)
            try:
                # save_call_window validates, upserts the window, hard-deletes
                # future planned entries if times changed, and commits.
                await cw_svc.save_call_window(
                    user_id=user.id,
                    window_type=window_type,
                    start_time=start,
                    end_time=end,
                )
            except ValueError as ve:
                return {"error": str(ve)}

            # Rematerialize replacement calls in the same transaction as
            # the window save. If this fails, we still have the window
            # saved (committed by the service), and the catch-up sweep
            # will backfill within 15 minutes — but we attempt it here
            # for immediate consistency.
            if user.onboarding_complete:
                try:
                    await _rematerialize_future_calls(
                        session, user, window_type_filter=window_type
                    )
                    await session.commit()
                except Exception:
                    logger.exception(
                        "Rematerialization failed for phone=%s type=%s; "
                        "catch-up sweep will backfill",
                        phone,
                        window_type,
                    )
    except Exception:
        logger.exception(
            "DB error saving call window for phone=%s type=%s", phone, window_type
        )
        return {"error": "Failed to save call window. Please try again."}

    # Write-through to session state only after DB success (Property 13)
    tool_context.state[f"user:{window_type}_call_start"] = start_time
    tool_context.state[f"user:{window_type}_call_end"] = end_time
    return {
        "status": "saved",
        "window_type": window_type,
        "start": start_time,
        "end": end_time,
    }


# ---------------------------------------------------------------------------
# Helper: rematerialize future planned calls
# ---------------------------------------------------------------------------

async def _rematerialize_future_calls(
    session: "AsyncSession",
    user: "User",
    window_type_filter: str | None = None,
) -> int:
    """Rematerialize planned CallLog entries after hard-delete.

    After a timezone change or call window edit, the hard-deleted entries
    need to be replaced with new ones computed from the updated settings.
    This rematerializes both today (if the window is still feasible) and
    tomorrow, so that editing a window mid-day doesn't leave a gap.

    Args:
        session: Active DB session (caller manages commit).
        user: The User whose calls to rematerialize.
        window_type_filter: If set, only rematerialize for this window type.

    Returns:
        Number of CallLog entries created.
    """
    from datetime import timedelta

    from sqlalchemy.exc import IntegrityError
    from sqlmodel import select
    from zoneinfo import ZoneInfo

    from app.models.call_log import CallLog
    from app.models.call_window import CallWindow
    from app.models.enums import CallLogStatus, OccurrenceKind
    from app.services.scheduling_helpers import (
        compute_first_call_date,
        compute_jittered_call_time,
        resolve_local_time,
    )

    if not user.timezone:
        return 0

    tz = ZoneInfo(user.timezone)
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)
    today = now_local.date()
    tomorrow = today + timedelta(days=1)

    # Fetch active windows
    stmt = select(CallWindow).where(
        CallWindow.user_id == user.id,
        CallWindow.is_active == True,  # noqa: E712
    )
    if window_type_filter:
        stmt = stmt.where(CallWindow.window_type == window_type_filter)

    result = await session.exec(stmt)
    windows = result.all()

    created = 0
    for window in windows:
        # Determine which dates to materialize: today (if feasible) + tomorrow
        target_dates = [tomorrow]
        first_date = compute_first_call_date(
            now_utc=now_utc,
            window_start=window.start_time,
            window_end=window.end_time,
            call_type=window.window_type,
            tz_name=user.timezone,
        )
        if first_date == today:
            target_dates.insert(0, today)

        for target_date in target_dates:
            local_time = compute_jittered_call_time(
                window_start=window.start_time,
                window_end=window.end_time,
                call_type=window.window_type,
            )
            resolved = resolve_local_time(
                target_date=target_date,
                local_time=local_time,
                tz_name=user.timezone,
            )
            call_log = CallLog(
                user_id=user.id,
                call_type=window.window_type,
                call_date=target_date,
                scheduled_time=resolved.utc_dt,
                scheduled_timezone=user.timezone,
                status=CallLogStatus.SCHEDULED.value,
                occurrence_kind=OccurrenceKind.PLANNED.value,
                attempt_number=1,
                origin_window_id=window.id,
            )
            try:
                async with session.begin_nested():
                    session.add(call_log)
                    await session.flush()
                created += 1
            except IntegrityError:
                pass  # Already exists — skip (Property 33)

    if created:
        logger.info(
            "Rematerialized %d CallLog entries for user_id=%d (today=%s, tomorrow=%s)",
            created,
            user.id,
            today,
            tomorrow,
        )

    return created


# ---------------------------------------------------------------------------
# Tool: generate_oauth_url
# ---------------------------------------------------------------------------

async def generate_oauth_url(service: str, tool_context: ToolContext) -> dict:
    """Generate a Google OAuth authorization link for the user.

    Args:
        service: The Google service to connect. One of: calendar, gmail.
    """
    from app.config import get_settings
    from app.services.ephemeral_token_service import create_ephemeral_token

    phone = tool_context.state.get("phone")
    if not phone:
        return {"error": "No phone number in session state."}

    valid_services = {"calendar", "gmail"}
    if service not in valid_services:
        return {"error": f"Invalid service. Must be one of: {', '.join(valid_services)}"}

    try:
        async with async_session_factory() as session:
            svc = UserService(session)
            user = await svc.get_by_phone(phone)
            if not user:
                return {"error": "User not found."}
            user_id = user.id

        token = await create_ephemeral_token(user_id, service)
    except Exception:
        logger.exception("Error generating OAuth URL for phone=%s", phone)
        return {"error": "Failed to generate authorization link. Please try again."}

    settings = get_settings()
    url = f"{settings.WEBHOOK_BASE_URL}/auth/google/start?token={token}&service={service}"

    return {
        "authorization_url": url,
        "message": f"Click this link to connect your Google {service.title()}: {url}",
        "expires_in_minutes": 10,
    }


# ---------------------------------------------------------------------------
# Tool: check_oauth_status
# ---------------------------------------------------------------------------

async def check_oauth_status(service: str, tool_context: ToolContext) -> dict:
    """Check whether the user has completed OAuth for a Google service.

    Args:
        service: The Google service to check. One of: calendar, gmail.
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return {"error": "No phone number in session state."}

    valid_services = {"calendar", "gmail"}
    if service not in valid_services:
        return {"error": f"Invalid service. Must be one of: {', '.join(valid_services)}"}

    try:
        async with async_session_factory() as session:
            svc = UserService(session)
            user = await svc.get_by_phone(phone)
            if not user:
                return {"error": "User not found."}

            scopes = (user.google_granted_scopes or "").split()
            if service == "calendar":
                connected = any("calendar" in s for s in scopes)
            elif service == "gmail":
                connected = any("gmail.modify" in s for s in scopes)
            else:
                connected = False
    except Exception:
        logger.exception("Error checking OAuth status for phone=%s", phone)
        return {"error": "Failed to check connection status. Please try again."}

    # Update session state
    state_key = f"user:google_{service}_connected"
    tool_context.state[state_key] = connected

    return {"connected": connected, "service": service}


# ---------------------------------------------------------------------------
# Tool: complete_onboarding
# ---------------------------------------------------------------------------

async def complete_onboarding(tool_context: ToolContext) -> dict:
    """Mark onboarding as complete and schedule the first calls.

    Verifies all required data is present, then in a single DB transaction:
    finds the earliest feasible next call across all enabled windows using
    compute_first_call_date (feasibility formula: now + 30min ≤ window_end
    - retry_buffer - max_call_duration), materializes CallLog entries for
    feasible-today windows and tomorrow for infeasible ones, sets
    user:onboarding_complete. If either fails, both roll back.

    Returns the earliest materialized entry's time so the agent can tell
    the user when to expect their first call.
    """
    from sqlalchemy.exc import IntegrityError
    from sqlmodel import select
    from zoneinfo import ZoneInfo

    from app.models.call_log import CallLog
    from app.models.call_window import CallWindow
    from app.models.enums import CallLogStatus, OccurrenceKind
    from app.services.scheduling_helpers import (
        compute_first_call_date,
        compute_jittered_call_time,
        resolve_local_time,
    )

    phone = tool_context.state.get("phone")
    if not phone:
        return {"error": "No phone number in session state."}

    # Check ALL required state keys — every onboarding step must be complete
    required = ["user:name", "user:timezone"]
    window_types = ["morning", "afternoon", "evening"]
    for wt in window_types:
        required.append(f"user:{wt}_call_start")
    required.extend(["user:google_calendar_connected", "user:google_gmail_connected"])

    missing = []
    for k in required:
        value = tool_context.state.get(k)
        if not value:
            missing.append(k)

    if missing:
        return {"error": f"Onboarding incomplete. Missing: {missing}"}

    try:
        async with async_session_factory() as session:
            svc = UserService(session)
            user = await svc.get_by_phone(phone)
            if not user:
                return {"error": "User not found."}

            if user.onboarding_complete:
                return {"status": "already_complete"}

            # Fetch all active windows
            result = await session.exec(
                select(CallWindow).where(
                    CallWindow.user_id == user.id,
                    CallWindow.is_active == True,  # noqa: E712
                )
            )
            windows = result.all()
            if not windows:
                return {"error": "No call windows configured."}

            # Materialize first calls for each window in a single transaction.
            # compute_first_call_date uses the feasibility formula (Property 5):
            # now + 30min ≤ window_end - retry_buffer - max_call_duration
            # If feasible today, returns today; otherwise tomorrow.
            tz = ZoneInfo(user.timezone)
            now_utc = datetime.now(timezone.utc)
            earliest_call_utc = None

            for window in windows:
                call_date = compute_first_call_date(
                    now_utc=now_utc,
                    window_start=window.start_time,
                    window_end=window.end_time,
                    call_type=window.window_type,
                    tz_name=user.timezone,
                )
                local_time = compute_jittered_call_time(
                    window_start=window.start_time,
                    window_end=window.end_time,
                    call_type=window.window_type,
                )
                resolved = resolve_local_time(
                    target_date=call_date,
                    local_time=local_time,
                    tz_name=user.timezone,
                )
                call_log = CallLog(
                    user_id=user.id,
                    call_type=window.window_type,
                    call_date=call_date,
                    scheduled_time=resolved.utc_dt,
                    scheduled_timezone=user.timezone,
                    status=CallLogStatus.SCHEDULED.value,
                    occurrence_kind=OccurrenceKind.PLANNED.value,
                    attempt_number=1,
                    origin_window_id=window.id,
                )
                # Use savepoint for idempotency (partial unique index, Property 33)
                try:
                    async with session.begin_nested():
                        session.add(call_log)
                        await session.flush()
                except IntegrityError:
                    pass  # Already exists — skip

                if earliest_call_utc is None or resolved.utc_dt < earliest_call_utc:
                    earliest_call_utc = resolved.utc_dt

            # Mark onboarding complete — same transaction as CallLog inserts.
            # If either fails, both roll back.
            user.onboarding_complete = True
            session.add(user)
            await session.commit()
    except Exception:
        logger.exception("DB error completing onboarding for phone=%s", phone)
        return {"error": "Failed to complete onboarding. Please try again."}

    # Write-through to session state only after DB success (Property 13)
    tool_context.state["user:onboarding_complete"] = True

    # Format earliest call time for user display
    if earliest_call_utc:
        tz = ZoneInfo(tool_context.state.get("user:timezone", "UTC"))
        local_time_display = earliest_call_utc.astimezone(tz)
        first_call_display = local_time_display.strftime("%A, %B %d at %I:%M %p")
    else:
        first_call_display = "soon"

    return {
        "status": "complete",
        "first_call": first_call_display,
    }
