"""ADK FunctionTool wrappers for task management.

Thin wrappers that resolve user identity from ToolContext session state
and delegate to TaskService. Each tool gets its own DB session via
async_session_factory (Option A from research).

These functions are added directly to the agent's ``tools`` list —
ADK auto-wraps them as FunctionTool instances.
"""

from google.adk.tools import ToolContext

from app.db import async_session_factory
from app.services.task_service import TaskService
from app.services.user_service import UserService


async def _resolve_user_id(phone: str) -> int | None:
    """Look up user.id from phone number."""
    async with async_session_factory() as session:
        svc = UserService(session)
        user = await svc.get_by_phone(phone)
        return user.id if user else None


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
        return {"error": "No phone number in session state."}

    user_id = await _resolve_user_id(phone)
    if not user_id:
        return {"error": "User not found."}

    async with async_session_factory() as session:
        svc = TaskService(session)
        task, created = await svc.save_task(user_id, title, priority, source)

    status = "created" if created else "merged"
    return {"status": status, "task_id": task.id, "title": task.title}


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
        return {"error": "No phone number in session state."}

    user_id = await _resolve_user_id(phone)
    if not user_id:
        return {"error": "User not found."}

    async with async_session_factory() as session:
        svc = TaskService(session)
        task = await svc.complete_task_by_title(user_id, title)

    if not task:
        return {"error": f"No pending task matching '{title}' found."}

    return {"status": "completed", "task_id": task.id, "title": task.title}


async def list_pending_tasks(
    limit: int = 5,
    tool_context: ToolContext = None,
) -> dict:
    """Get the user's top pending tasks sorted by priority.

    Args:
        limit: Maximum number of tasks to return. Defaults to 5.
    """
    if tool_context is None:
        return {"error": "No tool context available."}

    phone = tool_context.state.get("phone")
    if not phone:
        return {"error": "No phone number in session state."}

    user_id = await _resolve_user_id(phone)
    if not user_id:
        return {"error": "User not found."}

    async with async_session_factory() as session:
        svc = TaskService(session)
        tasks = await svc.list_pending_tasks(user_id, limit)

    return {
        "tasks": [
            {"id": t.id, "title": t.title, "priority": t.priority, "source": t.source}
            for t in tasks
        ],
        "count": len(tasks),
    }
