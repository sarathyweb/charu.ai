"""ADK FunctionTool wrappers for Google Calendar and Gmail integrations.

Thin wrappers that resolve user identity from ToolContext session state
and delegate to service-layer methods. Each tool gets its own DB session
via async_session_factory.

Calendar tools delegate to google_calendar_read_service and
google_calendar_write_service. Gmail read tools delegate to
gmail_read_service. Gmail draft tools delegate to EmailDraftService.

These functions are added directly to the agent's ``tools`` list —
ADK auto-wraps them as FunctionTool instances.

Validates: Requirements 10, 11, 17, 18
"""

from google.adk.tools import ToolContext

from app.db import async_session_factory
from app.services.user_service import UserService


async def _resolve_user(phone: str):
    """Look up the full User object from phone number."""
    async with async_session_factory() as session:
        svc = UserService(session)
        return await svc.get_by_phone(phone)


# ---------------------------------------------------------------------------
# Google Calendar tools (Requirements 10, 17)
# ---------------------------------------------------------------------------


async def get_todays_calendar(
    tool_context: ToolContext,
) -> dict:
    """Fetch today's calendar events for the user.

    Returns a formatted summary of today's events for use during
    accountability calls. Requires Google Calendar to be connected.
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return {"error": "No phone number in session state."}

    user = await _resolve_user(phone)
    if not user:
        return {"error": "User not found."}

    scopes = (user.google_granted_scopes or "")
    if "calendar" not in scopes:
        return {"error": "Google Calendar is not connected. Please connect it first."}

    from app.services.google_calendar_read_service import (
        fetch_todays_events,
        format_events_for_agent,
    )

    async with async_session_factory() as session:
        result = await fetch_todays_events(user, session)

    if isinstance(result, dict) and "error" in result:
        return result

    summary = format_events_for_agent(result, user.timezone or "UTC")
    return {"events": summary, "count": len(result)}


async def suggest_calendar_time_block(
    task_title: str,
    duration_minutes: int,
    tool_context: ToolContext,
) -> dict:
    """Find available time gaps and suggest a calendar block for a task.

    Call this first to present a suggestion to the user. If the user
    agrees, call create_calendar_time_block to actually create it.

    Args:
        task_title: The task name to block time for.
        duration_minutes: Desired duration in minutes (e.g. 30, 60).
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return {"error": "No phone number in session state."}

    user = await _resolve_user(phone)
    if not user:
        return {"error": "User not found."}

    scopes = (user.google_granted_scopes or "")
    if "calendar" not in scopes:
        return {"error": "Google Calendar is not connected. Please connect it first."}

    from app.services.google_calendar_write_service import find_available_gaps

    async with async_session_factory() as session:
        gaps = await find_available_gaps(
            user, session, min_duration_minutes=duration_minutes,
        )

    if isinstance(gaps, dict) and "error" in gaps:
        return gaps

    if not gaps:
        return {"has_suggestion": False, "message": "No available time gaps found today."}

    # Pick the earliest gap that fits the desired duration.
    for gap in gaps:
        if gap["duration_minutes"] >= duration_minutes:
            from datetime import datetime, timedelta

            start = datetime.fromisoformat(gap["start"])
            end = start + timedelta(minutes=duration_minutes)
            return {
                "has_suggestion": True,
                "suggested_start": start.isoformat(),
                "suggested_end": end.isoformat(),
                "duration_minutes": duration_minutes,
                "task_title": task_title,
            }

    # Fallback: use the largest gap available.
    largest = max(gaps, key=lambda g: g["duration_minutes"])
    return {
        "has_suggestion": True,
        "suggested_start": largest["start"],
        "suggested_end": largest["end"],
        "duration_minutes": largest["duration_minutes"],
        "task_title": task_title,
    }


async def create_calendar_time_block(
    task_title: str,
    start_iso: str,
    end_iso: str,
    task_id: str | None,
    tool_context: ToolContext,
) -> dict:
    """Create a time block on the user's Google Calendar for a task.

    Idempotent — if a block for the same task and date already exists,
    returns the existing event without creating a duplicate.

    Args:
        task_title: The task name used as the event summary.
        start_iso: RFC 3339 start datetime string.
        end_iso: RFC 3339 end datetime string.
        task_id: Optional task identifier for tracking.
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return {"error": "No phone number in session state."}

    user = await _resolve_user(phone)
    if not user:
        return {"error": "User not found."}

    scopes = (user.google_granted_scopes or "")
    if "calendar" not in scopes:
        return {"error": "Google Calendar is not connected. Please connect it first."}

    from app.services.google_calendar_write_service import create_time_block

    async with async_session_factory() as session:
        result = await create_time_block(
            user,
            session,
            task_title=task_title,
            start_iso=start_iso,
            end_iso=end_iso,
            task_id=task_id,
        )

    return result


# ---------------------------------------------------------------------------
# Gmail read tools (Requirement 11)
# ---------------------------------------------------------------------------


async def check_emails_needing_reply(
    tool_context: ToolContext,
) -> dict:
    """Check for emails that need a reply from the user.

    Returns a summary of up to 3 emails needing attention, for use
    during accountability calls. Requires Gmail to be connected.
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return {"error": "No phone number in session state."}

    user = await _resolve_user(phone)
    if not user:
        return {"error": "User not found."}

    scopes = (user.google_granted_scopes or "")
    if "gmail" not in scopes:
        return {"error": "Gmail is not connected. Please connect it first."}

    from app.services.gmail_read_service import (
        get_emails_needing_reply,
        format_emails_for_agent,
    )

    async with async_session_factory() as session:
        result = await get_emails_needing_reply(user, session, max_results=3)

    if isinstance(result, dict) and "error" in result:
        return result

    summary = format_emails_for_agent(result)
    return {"emails": result, "summary": summary, "count": len(result)}


async def get_email_for_reply(
    message_id: str,
    tool_context: ToolContext,
) -> dict:
    """Fetch the full content of a specific email for drafting a reply.

    Returns the email body, subject, sender, and thread info needed
    to compose a properly-threaded reply.

    Args:
        message_id: The Gmail message ID of the email to fetch.
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return {"error": "No phone number in session state."}

    user = await _resolve_user(phone)
    if not user:
        return {"error": "User not found."}

    scopes = (user.google_granted_scopes or "")
    if "gmail" not in scopes:
        return {"error": "Gmail is not connected. Please connect it first."}

    from app.services.gmail_read_service import (
        get_email_for_reply as _get_email_for_reply,
    )

    async with async_session_factory() as session:
        result = await _get_email_for_reply(user, session, message_id=message_id)

    return result


# ---------------------------------------------------------------------------
# Gmail draft tools (Requirement 18) — delegate to EmailDraftService
# ---------------------------------------------------------------------------


async def save_email_draft(
    thread_id: str,
    original_email_id: str,
    original_from: str,
    original_subject: str,
    original_message_id: str,
    draft_text: str,
    tool_context: ToolContext,
) -> dict:
    """Save an email draft for user review via WhatsApp.

    Persists the draft so it can be presented to the user for approval
    and tracked through the review cycle.

    Args:
        thread_id: The Gmail thread ID.
        original_email_id: The Gmail message ID being replied to.
        original_from: The sender's email address.
        original_subject: The subject line.
        original_message_id: The MIME Message-ID for threading.
        draft_text: The generated draft reply text.
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return {"error": "No phone number in session state."}

    user = await _resolve_user(phone)
    if not user:
        return {"error": "User not found."}

    from app.services.email_draft_service import EmailDraftService

    async with async_session_factory() as session:
        svc = EmailDraftService(session)
        draft = await svc.create_draft(
            user_id=user.id,
            thread_id=thread_id,
            original_email_id=original_email_id,
            original_from=original_from,
            original_subject=original_subject,
            original_message_id=original_message_id,
            draft_text=draft_text,
        )

    return {
        "status": "draft_saved",
        "draft_id": draft.id,
        "thread_id": draft.thread_id,
        "subject": draft.original_subject,
    }


async def update_email_draft(
    draft_id: int,
    new_draft_text: str,
    tool_context: ToolContext,
) -> dict:
    """Update an existing email draft after user requests changes.

    Only allowed when the draft is in pending_review or
    revision_requested state. Max 5 revisions.

    Args:
        draft_id: The ID of the draft to update.
        new_draft_text: The revised draft text.
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return {"error": "No phone number in session state."}

    user = await _resolve_user(phone)
    if not user:
        return {"error": "User not found."}

    from app.services.email_draft_service import EmailDraftService

    async with async_session_factory() as session:
        svc = EmailDraftService(session)
        try:
            draft = await svc.update_draft(draft_id, new_draft_text, user.id)
        except ValueError as exc:
            return {"error": str(exc)}

    return {
        "status": "draft_updated",
        "draft_id": draft.id,
        "revision_count": draft.revision_count,
    }


async def send_approved_reply(
    draft_id: int,
    tool_context: ToolContext,
) -> dict:
    """Send an approved email draft as a reply from the user's Gmail.

    IMPORTANT: Only call this AFTER the user has explicitly approved
    the draft content. Never send without user approval.

    Prevents duplicate sends — safe to retry if the first call's
    result was lost.

    Args:
        draft_id: The ID of the approved draft to send.
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return {"error": "No phone number in session state."}

    user = await _resolve_user(phone)
    if not user:
        return {"error": "User not found."}

    scopes = (user.google_granted_scopes or "")
    if "gmail" not in scopes:
        return {"error": "Gmail is not connected. Please connect it first."}

    from app.services.email_draft_service import EmailDraftService

    async with async_session_factory() as session:
        svc = EmailDraftService(session)
        try:
            result = await svc.approve_draft(draft_id, user)
        except ValueError as exc:
            return {"error": str(exc)}

    return result
