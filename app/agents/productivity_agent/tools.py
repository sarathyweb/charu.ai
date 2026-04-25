"""ADK FunctionTool wrappers for task management.

Thin wrappers that resolve user identity from ToolContext session state
and delegate to TaskService. Each tool gets its own DB session via
async_session_factory (Option A from research).

These functions are added directly to the agent's ``tools`` list —
ADK auto-wraps them as FunctionTool instances.
"""

import logging
from datetime import datetime

from google.adk.tools import ToolContext

from app.db import async_session_factory
from app.services.task_service import TaskService
from app.services.user_service import UserService

logger = logging.getLogger(__name__)


async def _resolve_user_id(phone: str) -> int | None:
    """Look up user.id from phone number."""
    async with async_session_factory() as session:
        svc = UserService(session)
        user = await svc.get_by_phone(phone)
        return user.id if user else None


def _error(message: str) -> dict:
    """Return the standard task-tool error payload."""
    return {"success": False, "error": message}


def _task_payload(task, *, status: str) -> dict:
    """Return the standard task-tool success payload for one task."""
    return {
        "success": True,
        "status": status,
        "task_id": task.id,
        "title": task.title,
        "priority": task.priority,
        "source": task.source,
        "snoozed_until": task.snoozed_until.isoformat() if task.snoozed_until else None,
    }


def _parse_snooze_until(snooze_until: str) -> datetime:
    """Parse an ISO-8601 datetime, requiring timezone information."""
    parsed = datetime.fromisoformat(snooze_until.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("snooze_until must include a timezone offset.")
    return parsed


async def save_task(
    title: str,
    priority: int,
    source: str,
    tool_context: ToolContext,
) -> dict:
    """Save a task to the user's task list.

    Args:
        title: Short description of the task.
        priority: Priority level 0-100 (higher = more important).
            Use 90 for explicitly urgent, 70 for email/calendar,
            50 for normal mentions, 20 for low priority.
        source: How the task was created. One of: user_mention, gmail,
            calendar, import.
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return _error("No phone number in session state.")

    user_id = await _resolve_user_id(phone)
    if not user_id:
        return _error("User not found.")

    try:
        async with async_session_factory() as session:
            svc = TaskService(session)
            task, created = await svc.save_task(user_id, title, priority, source)
    except Exception:
        logger.exception("save_task failed for user_id=%s", user_id)
        return _error("Failed to save task.")

    status = "created" if created else "merged"
    return _task_payload(task, status=status)


async def complete_task_by_title(
    title: str,
    tool_context: ToolContext,
) -> dict:
    """Mark a task as completed by matching its title.

    Args:
        title: Description of the task to complete. Will fuzzy-match
            against pending tasks.
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return _error("No phone number in session state.")

    user_id = await _resolve_user_id(phone)
    if not user_id:
        return _error("User not found.")

    try:
        async with async_session_factory() as session:
            svc = TaskService(session)
            task = await svc.complete_task_by_title(user_id, title)
    except Exception:
        logger.exception("complete_task_by_title failed for user_id=%s", user_id)
        return _error("Failed to complete task.")

    if not task:
        return _error(f"No pending task matching '{title}' found.")

    return _task_payload(task, status="completed")


async def update_task(
    title: str,
    tool_context: ToolContext,
    *,
    new_title: str = "",
    new_priority: int = -1,
) -> dict:
    """Update a task's title or priority by matching its current title.

    Args:
        title: Description of the task to update. Will fuzzy-match
            against pending tasks.
        new_title: New title for the task. Omit to keep the current title.
        new_priority: New priority for the task, from 0 to 100. Omit to
            keep the current priority.
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return _error("No phone number in session state.")

    user_id = await _resolve_user_id(phone)
    if not user_id:
        return _error("User not found.")

    try:
        async with async_session_factory() as session:
            svc = TaskService(session)
            task = await svc.update_task(
                user_id=user_id,
                title=title,
                new_title=new_title or None,
                new_priority=None if new_priority == -1 else new_priority,
            )
    except ValueError as exc:
        return _error(str(exc))
    except Exception:
        logger.exception("update_task failed for user_id=%s", user_id)
        return _error("Failed to update task.")

    if not task:
        return _error(f"No pending task matching '{title}' found.")

    return _task_payload(task, status="updated")


async def delete_task(
    title: str,
    tool_context: ToolContext,
) -> dict:
    """Delete a task permanently by matching its title.

    Args:
        title: Description of the task to delete. Will fuzzy-match
            against pending tasks.
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return _error("No phone number in session state.")

    user_id = await _resolve_user_id(phone)
    if not user_id:
        return _error("User not found.")

    try:
        async with async_session_factory() as session:
            svc = TaskService(session)
            task = await svc.delete_task(user_id=user_id, title=title)
    except Exception:
        logger.exception("delete_task failed for user_id=%s", user_id)
        return _error("Failed to delete task.")

    if not task:
        return _error(f"No pending task matching '{title}' found.")

    return _task_payload(task, status="deleted")


async def snooze_task(
    title: str,
    snooze_until: str,
    tool_context: ToolContext,
) -> dict:
    """Snooze a task until a specific date and time.

    Args:
        title: Description of the task to snooze. Will fuzzy-match
            against pending tasks.
        snooze_until: When the task should reappear, as an ISO-8601
            datetime with timezone offset.
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return _error("No phone number in session state.")

    user_id = await _resolve_user_id(phone)
    if not user_id:
        return _error("User not found.")

    try:
        parsed_snooze_until = _parse_snooze_until(snooze_until)
    except (TypeError, ValueError) as exc:
        return _error(f"Invalid snooze_until: {exc}")

    try:
        async with async_session_factory() as session:
            svc = TaskService(session)
            task = await svc.snooze_task(
                user_id=user_id,
                title=title,
                snooze_until=parsed_snooze_until,
            )
    except Exception:
        logger.exception("snooze_task failed for user_id=%s", user_id)
        return _error("Failed to snooze task.")

    if not task:
        return _error(f"No pending task matching '{title}' found.")

    return _task_payload(task, status="snoozed")


async def unsnooze_task(
    title: str,
    tool_context: ToolContext,
) -> dict:
    """Unsnooze a task by matching its title.

    Args:
        title: Description of the snoozed task to reactivate. Will
            fuzzy-match against snoozed tasks.
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return _error("No phone number in session state.")

    user_id = await _resolve_user_id(phone)
    if not user_id:
        return _error("User not found.")

    try:
        async with async_session_factory() as session:
            svc = TaskService(session)
            task = await svc.unsnooze_task(user_id=user_id, title=title)
    except Exception:
        logger.exception("unsnooze_task failed for user_id=%s", user_id)
        return _error("Failed to unsnooze task.")

    if not task:
        return _error(f"No snoozed task matching '{title}' found.")

    return _task_payload(task, status="unsnoozed")


async def list_pending_tasks(
    tool_context: ToolContext,
    limit: int = 5,
) -> dict:
    """Get the user's top pending tasks sorted by priority.

    Args:
        limit: Maximum number of tasks to return. Defaults to 5.
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return _error("No phone number in session state.")

    user_id = await _resolve_user_id(phone)
    if not user_id:
        return _error("User not found.")

    try:
        async with async_session_factory() as session:
            svc = TaskService(session)
            tasks = await svc.list_pending_tasks(user_id, 5 if limit is None else limit)
    except ValueError as exc:
        return _error(str(exc))
    except Exception:
        logger.exception("list_pending_tasks failed for user_id=%s", user_id)
        return _error("Failed to list pending tasks.")

    return {
        "success": True,
        "tasks": [
            {
                "id": t.id,
                "title": t.title,
                "priority": t.priority,
                "source": t.source,
                "status": t.status,
                "snoozed_until": t.snoozed_until.isoformat()
                if t.snoozed_until
                else None,
            }
            for t in tasks
        ],
        "count": len(tasks),
    }
