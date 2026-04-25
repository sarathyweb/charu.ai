"""ADK FunctionTool wrappers for goal management."""

import logging
from datetime import date

from google.adk.tools import ToolContext

from app.db import async_session_factory
from app.services.goal_service import GoalService

from .tools import _resolve_user_id

logger = logging.getLogger(__name__)


def _error(message: str) -> dict:
    """Return the standard goal-tool error payload."""
    return {"success": False, "error": message}


def _goal_payload(goal, *, status: str) -> dict:
    """Return the standard goal-tool success payload for one goal."""
    return {
        "success": True,
        "status": status,
        "goal_id": goal.id,
        "title": goal.title,
        "description": goal.description,
        "goal_status": goal.status,
        "target_date": goal.target_date.isoformat() if goal.target_date else None,
        "completed_at": goal.completed_at.isoformat() if goal.completed_at else None,
    }


def _parse_target_date(target_date: str) -> date | None:
    """Parse an optional ISO date string."""
    if not target_date:
        return None
    try:
        return date.fromisoformat(target_date)
    except ValueError as exc:
        raise ValueError("target_date must be in YYYY-MM-DD format.") from exc


async def create_goal(
    title: str,
    tool_context: ToolContext,
    description: str = "",
    target_date: str = "",
) -> dict:
    """Create a higher-level goal for the user.

    Args:
        title: Short description of the goal.
        description: Optional longer description or context.
        target_date: Optional target date in YYYY-MM-DD format.
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return _error("No phone number in session state.")

    user_id = await _resolve_user_id(phone)
    if not user_id:
        return _error("User not found.")

    try:
        parsed_target_date = _parse_target_date(target_date)
        async with async_session_factory() as session:
            svc = GoalService(session)
            goal = await svc.create_goal(
                user_id=user_id,
                title=title,
                description=description or None,
                target_date=parsed_target_date,
            )
    except ValueError as exc:
        return _error(str(exc))
    except Exception:
        logger.exception("create_goal failed for user_id=%s", user_id)
        return _error("Failed to create goal.")

    return _goal_payload(goal, status="created")


async def update_goal(
    goal_id: int,
    tool_context: ToolContext,
    *,
    new_title: str = "",
    new_description: str = "",
    new_target_date: str = "",
) -> dict:
    """Update a goal's title, description, or target date.

    Args:
        goal_id: The goal ID from list_goals results.
        new_title: New goal title. Omit to keep the current title.
        new_description: New goal description. Omit to keep the current description.
        new_target_date: New target date in YYYY-MM-DD format. Omit to keep it.
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return _error("No phone number in session state.")

    user_id = await _resolve_user_id(phone)
    if not user_id:
        return _error("User not found.")

    try:
        parsed_target_date = _parse_target_date(new_target_date)
        async with async_session_factory() as session:
            svc = GoalService(session)
            goal = await svc.update_goal(
                goal_id=goal_id,
                user_id=user_id,
                new_title=new_title or None,
                new_description=new_description or None,
                new_target_date=parsed_target_date,
            )
    except ValueError as exc:
        return _error(str(exc))
    except Exception:
        logger.exception(
            "update_goal failed for user_id=%s goal_id=%s", user_id, goal_id
        )
        return _error("Failed to update goal.")

    if not goal:
        return _error("Goal not found.")

    return _goal_payload(goal, status="updated")


async def complete_goal(
    goal_id: int,
    tool_context: ToolContext,
) -> dict:
    """Mark a goal as completed.

    Args:
        goal_id: The goal ID from list_goals results.
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return _error("No phone number in session state.")

    user_id = await _resolve_user_id(phone)
    if not user_id:
        return _error("User not found.")

    try:
        async with async_session_factory() as session:
            svc = GoalService(session)
            goal = await svc.complete_goal(goal_id=goal_id, user_id=user_id)
    except Exception:
        logger.exception(
            "complete_goal failed for user_id=%s goal_id=%s", user_id, goal_id
        )
        return _error("Failed to complete goal.")

    if not goal:
        return _error("Goal not found.")

    return _goal_payload(goal, status="completed")


async def abandon_goal(
    goal_id: int,
    tool_context: ToolContext,
) -> dict:
    """Mark a goal as abandoned.

    Args:
        goal_id: The goal ID from list_goals results.
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return _error("No phone number in session state.")

    user_id = await _resolve_user_id(phone)
    if not user_id:
        return _error("User not found.")

    try:
        async with async_session_factory() as session:
            svc = GoalService(session)
            goal = await svc.abandon_goal(goal_id=goal_id, user_id=user_id)
    except Exception:
        logger.exception(
            "abandon_goal failed for user_id=%s goal_id=%s", user_id, goal_id
        )
        return _error("Failed to abandon goal.")

    if not goal:
        return _error("Goal not found.")

    return _goal_payload(goal, status="abandoned")


async def list_goals(
    tool_context: ToolContext,
    status: str = "",
) -> dict:
    """List the user's goals, optionally filtered by status.

    Args:
        status: Optional filter: active, completed, or abandoned.
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return _error("No phone number in session state.")

    user_id = await _resolve_user_id(phone)
    if not user_id:
        return _error("User not found.")

    try:
        async with async_session_factory() as session:
            svc = GoalService(session)
            goals = await svc.list_goals(user_id=user_id, status=status or None)
    except ValueError as exc:
        return _error(str(exc))
    except Exception:
        logger.exception("list_goals failed for user_id=%s", user_id)
        return _error("Failed to list goals.")

    return {
        "success": True,
        "goals": [
            {
                "id": goal.id,
                "title": goal.title,
                "description": goal.description,
                "status": goal.status,
                "target_date": goal.target_date.isoformat()
                if goal.target_date
                else None,
                "completed_at": goal.completed_at.isoformat()
                if goal.completed_at
                else None,
            }
            for goal in goals
        ],
        "count": len(goals),
    }


async def delete_goal(
    goal_id: int,
    tool_context: ToolContext,
) -> dict:
    """Permanently delete a goal.

    Args:
        goal_id: The goal ID from list_goals results.
    """
    phone = tool_context.state.get("phone")
    if not phone:
        return _error("No phone number in session state.")

    user_id = await _resolve_user_id(phone)
    if not user_id:
        return _error("User not found.")

    try:
        async with async_session_factory() as session:
            svc = GoalService(session)
            goal = await svc.delete_goal(goal_id=goal_id, user_id=user_id)
    except Exception:
        logger.exception(
            "delete_goal failed for user_id=%s goal_id=%s", user_id, goal_id
        )
        return _error("Failed to delete goal.")

    if not goal:
        return _error("Goal not found.")

    return _goal_payload(goal, status="deleted")
