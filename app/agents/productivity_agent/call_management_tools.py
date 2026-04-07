"""ADK FunctionTool wrappers for call management.

Thin wrappers that resolve user identity from ToolContext session state
and delegate to CallManagementService. Each tool gets its own DB session
via async_session_factory.

No business logic here — all state guards, two-layer cancellation, and
idempotency are handled by CallManagementService.

These functions are added directly to the agent's ``tools`` list —
ADK auto-wraps them as FunctionTool instances.

Validates: Requirement 21
"""

from datetime import time

from google.adk.tools import ToolContext

from app.db import async_session_factory
from app.services.call_management_service import CallManagementService
from app.services.user_service import UserService


async def _resolve_user_id(phone: str) -> int | None:
    """Look up user.id from phone number."""
    async with async_session_factory() as session:
        svc = UserService(session)
        user = await svc.get_by_phone(phone)
        return user.id if user else None


async def schedule_callback(
    minutes_from_now: int,
    tool_context: ToolContext,
) -> dict:
    """Schedule an on-demand callback call in the specified number of minutes.

    Args:
        minutes_from_now: How many minutes from now to place the call.
            Must be between 1 and 120.
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return {"error": "No phone number in session state."}

    user_id = await _resolve_user_id(phone)
    if not user_id:
        return {"error": "User not found."}

    async with async_session_factory() as session:
        svc = CallManagementService(session)
        result = await svc.schedule_callback(user_id, minutes_from_now)

    return {
        "success": result.success,
        "message": result.message,
        **({"call_log_id": result.call_log_id} if result.call_log_id else {}),
    }


async def skip_call(
    call_type: str,
    tool_context: ToolContext,
) -> dict:
    """Skip the next scheduled call of the specified type for today.

    Args:
        call_type: The type of call to skip. One of: morning,
            afternoon, evening.
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return {"error": "No phone number in session state."}

    user_id = await _resolve_user_id(phone)
    if not user_id:
        return {"error": "User not found."}

    async with async_session_factory() as session:
        svc = CallManagementService(session)
        result = await svc.skip_call(user_id, call_type)

    return {
        "success": result.success,
        "message": result.message,
        **({"call_log_id": result.call_log_id} if result.call_log_id else {}),
    }


async def reschedule_call(
    call_type: str,
    new_time: str,
    tool_context: ToolContext,
) -> dict:
    """Reschedule today's call of the specified type to a new time.

    This is a one-off change for today only — it does not modify the
    recurring call window.

    Args:
        call_type: The type of call to reschedule. One of: morning,
            afternoon, evening.
        new_time: The new time in HH:MM format (24-hour, user's local
            timezone). For example "09:00" or "14:30".
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return {"error": "No phone number in session state."}

    user_id = await _resolve_user_id(phone)
    if not user_id:
        return {"error": "User not found."}

    try:
        parsed_time = time.fromisoformat(new_time)
    except ValueError:
        return {"error": f"Invalid time format '{new_time}'. Use HH:MM (e.g. 09:00)."}

    async with async_session_factory() as session:
        svc = CallManagementService(session)
        result = await svc.reschedule_call(user_id, call_type, parsed_time)

    return {
        "success": result.success,
        "message": result.message,
        **({"call_log_id": result.call_log_id} if result.call_log_id else {}),
    }


async def get_next_call(
    tool_context: ToolContext,
) -> dict:
    """Look up when the user's next scheduled call is.

    Returns the call type, date, time, and timezone.
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return {"error": "No phone number in session state."}

    user_id = await _resolve_user_id(phone)
    if not user_id:
        return {"error": "User not found."}

    async with async_session_factory() as session:
        svc = CallManagementService(session)
        result = await svc.get_next_call(user_id)

    resp: dict = {
        "success": result.success,
        "message": result.message,
    }
    if result.next_call:
        resp["next_call"] = {
            "call_type": result.next_call.call_type,
            "date": result.next_call.date,
            "time": result.next_call.time,
            "timezone": result.next_call.timezone,
        }
    return resp


async def cancel_all_calls_today(
    tool_context: ToolContext,
) -> dict:
    """Cancel all remaining scheduled calls for today.

    Returns the number of calls that were cancelled.
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return {"error": "No phone number in session state."}

    user_id = await _resolve_user_id(phone)
    if not user_id:
        return {"error": "User not found."}

    async with async_session_factory() as session:
        svc = CallManagementService(session)
        result = await svc.cancel_all_calls_today(user_id)

    return {
        "success": result.success,
        "message": result.message,
        "cancelled_count": result.cancelled_count or 0,
    }
