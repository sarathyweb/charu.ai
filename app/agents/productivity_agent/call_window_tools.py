"""ADK FunctionTool wrappers for call window CRUD.

Thin wrappers that resolve user identity from ToolContext session state
and delegate to CallWindowService. Each tool gets its own DB session
via async_session_factory.

Includes overlap detection and max-3-windows-per-user enforcement.

These functions are added directly to the agent's ``tools`` list —
ADK auto-wraps them as FunctionTool instances.

Validates: Requirement 16
"""

import logging
from datetime import time

from google.adk.tools import ToolContext

from app.db import async_session_factory
from app.services.call_window_service import CallWindowService
from app.services.user_service import UserService

logger = logging.getLogger(__name__)

MAX_WINDOWS_PER_USER = 3


async def _resolve_user_id(phone: str) -> int | None:
    """Look up user.id from phone number."""
    async with async_session_factory() as session:
        svc = UserService(session)
        user = await svc.get_by_phone(phone)
        return user.id if user else None


def _windows_overlap(
    a_start: time, a_end: time, b_start: time, b_end: time
) -> bool:
    """Check if two same-day time windows overlap."""
    a_s = a_start.hour * 60 + a_start.minute
    a_e = a_end.hour * 60 + a_end.minute
    b_s = b_start.hour * 60 + b_start.minute
    b_e = b_end.hour * 60 + b_end.minute
    return a_s < b_e and b_s < a_e


async def add_call_window(
    window_type: str,
    start_time: str,
    end_time: str,
    tool_context: ToolContext,
) -> dict:
    """Add a new call window for the user.

    Validates overlap with existing windows and enforces a maximum of 3
    active windows per user.

    Args:
        window_type: One of: morning, afternoon, evening.
        start_time: Window start in HH:MM format (e.g. 07:00).
        end_time: Window end in HH:MM format (e.g. 07:30).
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return {"error": "No phone number in session state."}

    valid_types = {"morning", "afternoon", "evening"}
    if window_type not in valid_types:
        return {"error": f"Invalid window_type. Must be one of: {', '.join(sorted(valid_types))}"}

    try:
        start = time.fromisoformat(start_time)
        end = time.fromisoformat(end_time)
    except ValueError:
        return {"error": "Times must be in HH:MM format (e.g. 07:00)."}

    start_minutes = start.hour * 60 + start.minute
    end_minutes = end.hour * 60 + end.minute
    if end_minutes <= start_minutes:
        return {"error": "End time must be after start time (no cross-midnight windows)."}
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
            existing = await cw_svc.list_windows_for_user(user.id)

            # Check if this window_type already exists (upsert case)
            existing_same_type = [w for w in existing if w.window_type == window_type]
            is_new = len(existing_same_type) == 0

            # Max 3 windows enforcement (only for truly new windows)
            if is_new and len(existing) >= MAX_WINDOWS_PER_USER:
                return {
                    "error": f"You already have {len(existing)} call windows "
                    f"(maximum is {MAX_WINDOWS_PER_USER}). "
                    "Remove one before adding another."
                }

            # Overlap detection against other active windows
            for w in existing:
                if w.window_type == window_type:
                    continue  # Skip self (will be replaced by upsert)
                if _windows_overlap(start, end, w.start_time, w.end_time):
                    w_start_str = w.start_time.strftime("%H:%M")
                    w_end_str = w.end_time.strftime("%H:%M")
                    return {
                        "error": f"This window overlaps with your {w.window_type} "
                        f"window ({w_start_str}–{w_end_str}). "
                        "Please choose a non-overlapping time."
                    }

            try:
                window = await cw_svc.save_call_window(
                    user_id=user.id,
                    window_type=window_type,
                    start_time=start,
                    end_time=end,
                )
            except ValueError as ve:
                return {"error": str(ve)}

            # Rematerialize if onboarding is complete
            if user.onboarding_complete:
                try:
                    from app.agents.productivity_agent.onboarding_tools import (
                        _rematerialize_future_calls,
                    )

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
        logger.exception("DB error adding call window for phone=%s type=%s", phone, window_type)
        return {"error": "Failed to add call window. Please try again."}

    # Write-through to session state
    tool_context.state[f"user:{window_type}_call_start"] = start_time
    tool_context.state[f"user:{window_type}_call_end"] = end_time

    action = "updated" if not is_new else "added"
    return {
        "status": action,
        "window_type": window_type,
        "start": start_time,
        "end": end_time,
    }


async def update_call_window(
    window_type: str,
    start_time: str | None = None,
    end_time: str | None = None,
    tool_context: ToolContext = None,
) -> dict:
    """Update an existing call window's times.

    At least one of start_time or end_time must be provided. This is a
    permanent change to the recurring schedule (unlike reschedule_call
    which is a one-off change for today).

    Args:
        window_type: The window to update. One of: morning, afternoon, evening.
        start_time: New start in HH:MM format, or omit to keep current.
        end_time: New end in HH:MM format, or omit to keep current.
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return {"error": "No phone number in session state."}

    if start_time is None and end_time is None:
        return {"error": "Provide at least one of start_time or end_time to update."}

    valid_types = {"morning", "afternoon", "evening"}
    if window_type not in valid_types:
        return {"error": f"Invalid window_type. Must be one of: {', '.join(sorted(valid_types))}"}

    parsed_start: time | None = None
    parsed_end: time | None = None
    try:
        if start_time is not None:
            parsed_start = time.fromisoformat(start_time)
        if end_time is not None:
            parsed_end = time.fromisoformat(end_time)
    except ValueError:
        return {"error": "Times must be in HH:MM format (e.g. 07:00)."}

    try:
        async with async_session_factory() as session:
            svc = UserService(session)
            user = await svc.get_by_phone(phone)
            if not user:
                return {"error": "User not found."}

            cw_svc = CallWindowService(session)
            existing = await cw_svc.list_windows_for_user(user.id)

            # Find the target window
            target = next((w for w in existing if w.window_type == window_type), None)
            if target is None:
                return {"error": f"No active {window_type} call window found."}

            # Compute effective new times
            eff_start = parsed_start if parsed_start is not None else target.start_time
            eff_end = parsed_end if parsed_end is not None else target.end_time

            # Basic validation
            s_min = eff_start.hour * 60 + eff_start.minute
            e_min = eff_end.hour * 60 + eff_end.minute
            if e_min <= s_min:
                return {"error": "End time must be after start time (no cross-midnight windows)."}
            if (e_min - s_min) < 20:
                return {"error": "Call window must be at least 20 minutes wide."}

            # Overlap detection against other windows
            for w in existing:
                if w.window_type == window_type:
                    continue
                if _windows_overlap(eff_start, eff_end, w.start_time, w.end_time):
                    w_start_str = w.start_time.strftime("%H:%M")
                    w_end_str = w.end_time.strftime("%H:%M")
                    return {
                        "error": f"This window would overlap with your {w.window_type} "
                        f"window ({w_start_str}–{w_end_str}). "
                        "Please choose a non-overlapping time."
                    }

            try:
                window = await cw_svc.update_window(
                    window_id=target.id,
                    start_time=parsed_start,
                    end_time=parsed_end,
                )
            except ValueError as ve:
                return {"error": str(ve)}

            # Rematerialize if onboarding is complete
            if user.onboarding_complete:
                try:
                    from app.agents.productivity_agent.onboarding_tools import (
                        _rematerialize_future_calls,
                    )

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
        logger.exception("DB error updating call window for phone=%s type=%s", phone, window_type)
        return {"error": "Failed to update call window. Please try again."}

    # Write-through to session state
    final_start = window.start_time.strftime("%H:%M")
    final_end = window.end_time.strftime("%H:%M")
    tool_context.state[f"user:{window_type}_call_start"] = final_start
    tool_context.state[f"user:{window_type}_call_end"] = final_end

    return {
        "status": "updated",
        "window_type": window_type,
        "start": final_start,
        "end": final_end,
    }


async def remove_call_window(
    window_type: str,
    tool_context: ToolContext,
) -> dict:
    """Remove (deactivate) a call window without affecting other windows.

    The window is soft-deactivated and its future scheduled calls are
    cancelled. Other windows remain unchanged.

    Args:
        window_type: The window to remove. One of: morning, afternoon, evening.
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return {"error": "No phone number in session state."}

    valid_types = {"morning", "afternoon", "evening"}
    if window_type not in valid_types:
        return {"error": f"Invalid window_type. Must be one of: {', '.join(sorted(valid_types))}"}

    try:
        async with async_session_factory() as session:
            svc = UserService(session)
            user = await svc.get_by_phone(phone)
            if not user:
                return {"error": "User not found."}

            cw_svc = CallWindowService(session)
            existing = await cw_svc.list_windows_for_user(user.id)

            target = next((w for w in existing if w.window_type == window_type), None)
            if target is None:
                # Idempotent — already removed or never existed
                return {"status": "already_removed", "window_type": window_type}

            await cw_svc.deactivate_window(target.id)
    except Exception:
        logger.exception("DB error removing call window for phone=%s type=%s", phone, window_type)
        return {"error": "Failed to remove call window. Please try again."}

    # Clear session state
    tool_context.state[f"user:{window_type}_call_start"] = ""
    tool_context.state[f"user:{window_type}_call_end"] = ""

    return {"status": "removed", "window_type": window_type}


async def list_call_windows(
    tool_context: ToolContext,
) -> dict:
    """List all active call windows for the user.

    Returns each window's type, start time, and end time.
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return {"error": "No phone number in session state."}

    user_id = await _resolve_user_id(phone)
    if not user_id:
        return {"error": "User not found."}

    async with async_session_factory() as session:
        cw_svc = CallWindowService(session)
        windows = await cw_svc.list_windows_for_user(user_id)

    return {
        "windows": [
            {
                "window_type": w.window_type,
                "start": w.start_time.strftime("%H:%M"),
                "end": w.end_time.strftime("%H:%M"),
                "is_active": w.is_active,
            }
            for w in windows
        ],
        "count": len(windows),
    }
