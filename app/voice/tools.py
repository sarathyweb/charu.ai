"""Voice call tool registration for GeminiLiveLLMService.

Registers thin tool wrappers on the LLM service that delegate to the
shared service layer (CallManagementService, TaskService, CallLogService).

Each tool receives ``call_log_id`` and ``user_id`` via closure so that
outcomes are persisted to the correct CallLog row.

Design references:
  - Design §2: Voice Call Pipeline (tools registered on GeminiLiveLLMService)
  - Requirement 4: Core Accountability Call Flow
  - Requirement 5: Structured Call Outcome
  - Requirement 9: Task Management
  - Requirement 20: Evening Reflection Call
  - Requirement 21: Call Management
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from datetime import time as dt_time

from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams

from app.db import async_session_factory
from app.models.call_log import CallLog
from app.services.call_management_service import CallManagementService
from app.services.goal_service import GoalService
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)


def _task_payload(task, *, status: str) -> dict:
    """Return the standard voice task-tool success payload for one task."""
    return {
        "success": True,
        "status": status,
        "task_id": task.id,
        "title": task.title,
        "priority": task.priority,
        "source": task.source,
        "snoozed_until": task.snoozed_until.isoformat() if task.snoozed_until else None,
    }


def _goal_payload(goal, *, status: str) -> dict:
    """Return the standard voice goal-tool success payload for one goal."""
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


def _parse_snooze_until(snooze_until: str) -> datetime:
    """Parse an ISO-8601 datetime, requiring timezone information."""
    parsed = datetime.fromisoformat(snooze_until.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("snooze_until must include a timezone offset.")
    return parsed


def _parse_goal_target_date(target_date: str) -> date | None:
    """Parse an optional ISO date string for goal tools."""
    if not target_date:
        return None
    try:
        return date.fromisoformat(target_date)
    except ValueError as exc:
        raise ValueError("target_date must be in YYYY-MM-DD format.") from exc


def register_voice_tools(
    llm,  # GeminiLiveLLMService
    *,
    call_log_id: int,
    user_id: int,
) -> ToolsSchema:
    """Register all voice call tools on *llm* and return the ToolsSchema.

    Tools capture ``call_log_id`` and ``user_id`` via closure so every
    tool invocation targets the correct user and call.
    """

    # ── Outcome tools ────────────────────────────────────────────────

    async def save_call_outcome(
        params: FunctionCallParams,
        goal: str | None = None,
        next_action: str | None = None,
        commitments: list[str] | None = None,
        confidence: str = "clear",
    ):
        """Save the structured outcome of a morning or afternoon accountability call.

        Invocation Condition: Call this tool at the END of the call, after
        summarising the goal and next action to the user. Call exactly once.

        Args:
            goal: The goal identified during the call, or null if none.
            next_action: The concrete next action the user committed to, or null.
            commitments: Additional commitments made (e.g. calendar blocks).
            confidence: How clearly goal and action were identified.
                Must be "clear", "partial", or "none".
        """
        try:
            async with async_session_factory() as session:
                call_log = await session.get(CallLog, call_log_id)
                if call_log is None:
                    await params.result_callback(
                        {"success": False, "error": "CallLog not found"}
                    )
                    return

                call_log.goal = goal
                call_log.next_action = next_action
                call_log.commitments = commitments
                call_log.call_outcome_confidence = confidence
                session.add(call_log)
                await session.commit()

            logger.info(
                "save_call_outcome: call_log_id=%d goal=%r confidence=%s",
                call_log_id,
                goal,
                confidence,
            )
            await params.result_callback({"success": True, "status": "saved"})
        except Exception:
            logger.exception("save_call_outcome failed for call_log_id=%d", call_log_id)
            await params.result_callback(
                {"success": False, "error": "Failed to save call outcome"}
            )

    async def save_evening_call_outcome(
        params: FunctionCallParams,
        accomplishments: str | None = None,
        tomorrow_intention: str | None = None,
        confidence: str = "clear",
    ):
        """Save the structured outcome of an evening reflection call.

        Invocation Condition: Call this tool at the END of the evening call,
        after summarising accomplishments and tomorrow's intention. Call exactly once.

        Args:
            accomplishments: What the user accomplished or made progress on today, or null.
            tomorrow_intention: The one thing the user wants to prioritise tomorrow, or null.
            confidence: How clearly accomplishments and intention were identified.
                Must be "clear", "partial", or "none".
        """
        try:
            async with async_session_factory() as session:
                call_log = await session.get(CallLog, call_log_id)
                if call_log is None:
                    await params.result_callback(
                        {"success": False, "error": "CallLog not found"}
                    )
                    return

                call_log.accomplishments = accomplishments
                call_log.tomorrow_intention = tomorrow_intention
                call_log.reflection_confidence = confidence
                session.add(call_log)
                await session.commit()

            logger.info(
                "save_evening_call_outcome: call_log_id=%d confidence=%s",
                call_log_id,
                confidence,
            )
            await params.result_callback({"success": True, "status": "saved"})
        except Exception:
            logger.exception(
                "save_evening_call_outcome failed for call_log_id=%d", call_log_id
            )
            await params.result_callback(
                {"success": False, "error": "Failed to save evening outcome"}
            )

    # ── Task tools ───────────────────────────────────────────────────

    async def save_task(
        params: FunctionCallParams,
        title: str,
        priority: int = 50,
        source: str = "user_mention",
    ):
        """Save a task to the user's task list with fuzzy deduplication.

        Invocation Condition: Call when the user mentions a task, commitment,
        or something they need to do. Also call when the user sets a tomorrow
        intention during the evening call.

        Args:
            title: Short description of the task.
            priority: Priority 0-100. Use 90 for urgent, 50 for normal, 20 for low.
            source: How the task was created. Usually "user_mention" during calls.
        """
        try:
            async with async_session_factory() as session:
                svc = TaskService(session)
                task, created = await svc.save_task(
                    user_id=user_id,
                    title=title,
                    priority=priority,
                    source=source,
                )

            status = "created" if created else "merged"
            logger.info(
                "save_task: user_id=%d title=%r status=%s task_id=%d",
                user_id,
                title,
                status,
                task.id,
            )
            await params.result_callback(_task_payload(task, status=status))
        except Exception:
            logger.exception("save_task failed for user_id=%d", user_id)
            await params.result_callback(
                {"success": False, "error": "Failed to save task"}
            )

    async def complete_task_by_title(
        params: FunctionCallParams,
        title: str,
    ):
        """Mark a task as completed by fuzzy-matching its title.

        Invocation Condition: Call when the user says they finished or
        completed something. The title will be fuzzy-matched against
        pending tasks.

        Args:
            title: Description of the completed task. Will fuzzy-match
                against the user's pending tasks.
        """
        try:
            async with async_session_factory() as session:
                svc = TaskService(session)
                task = await svc.complete_task_by_title(
                    user_id=user_id,
                    title=title,
                )

            if task is None:
                await params.result_callback(
                    {
                        "success": False,
                        "error": f"No pending task matching '{title}' found.",
                    }
                )
                return

            logger.info(
                "complete_task_by_title: user_id=%d title=%r task_id=%d",
                user_id,
                title,
                task.id,
            )
            await params.result_callback(_task_payload(task, status="completed"))
        except Exception:
            logger.exception("complete_task_by_title failed for user_id=%d", user_id)
            await params.result_callback(
                {"success": False, "error": "Failed to complete task"}
            )

    async def list_pending_tasks(
        params: FunctionCallParams,
        limit: int = 5,
    ):
        """Get the user's top pending tasks sorted by priority.

        Invocation Condition: Call when the user asks about their tasks,
        to-do list, or what they need to do.

        Args:
            limit: Maximum number of tasks to return. Defaults to 5.
        """
        try:
            async with async_session_factory() as session:
                svc = TaskService(session)
                tasks = await svc.list_pending_tasks(
                    user_id=user_id,
                    limit=5 if limit is None else limit,
                )

            logger.info(
                "list_pending_tasks: user_id=%d count=%d",
                user_id,
                len(tasks),
            )
            await params.result_callback(
                {
                    "success": True,
                    "tasks": [
                        {
                            "id": task.id,
                            "title": task.title,
                            "priority": task.priority,
                            "source": task.source,
                            "status": task.status,
                            "snoozed_until": task.snoozed_until.isoformat()
                            if task.snoozed_until
                            else None,
                        }
                        for task in tasks
                    ],
                    "count": len(tasks),
                }
            )
        except ValueError as exc:
            await params.result_callback({"success": False, "error": str(exc)})
        except Exception:
            logger.exception("list_pending_tasks failed for user_id=%d", user_id)
            await params.result_callback(
                {"success": False, "error": "Failed to list pending tasks"}
            )

    async def update_task(
        params: FunctionCallParams,
        title: str,
        new_title: str = "",
        new_priority: int = -1,
    ):
        """Update a task's title or priority by fuzzy-matching its current title.

        Invocation Condition: Call when the user wants to rename a task or
        change its priority.

        Args:
            title: Description of the task to update. Will fuzzy-match
                against the user's pending tasks.
            new_title: New title for the task. Omit to keep the current title.
            new_priority: New priority 0-100. Omit to keep the current priority.
        """
        try:
            async with async_session_factory() as session:
                svc = TaskService(session)
                task = await svc.update_task(
                    user_id=user_id,
                    title=title,
                    new_title=new_title or None,
                    new_priority=None if new_priority == -1 else new_priority,
                )

            if task is None:
                await params.result_callback(
                    {
                        "success": False,
                        "error": f"No pending task matching '{title}' found.",
                    }
                )
                return

            logger.info(
                "update_task: user_id=%d title=%r task_id=%d",
                user_id,
                title,
                task.id,
            )
            await params.result_callback(_task_payload(task, status="updated"))
        except ValueError as exc:
            await params.result_callback({"success": False, "error": str(exc)})
        except Exception:
            logger.exception("update_task failed for user_id=%d", user_id)
            await params.result_callback(
                {"success": False, "error": "Failed to update task"}
            )

    async def delete_task(
        params: FunctionCallParams,
        title: str,
    ):
        """Permanently delete a task by fuzzy-matching its title.

        Invocation Condition: Call after the user clearly confirms they want
        to remove or delete a task from their list. Because this is permanent,
        ask for confirmation first if there is any ambiguity.

        Args:
            title: Description of the task to delete. Will fuzzy-match
                against the user's pending tasks.
        """
        try:
            async with async_session_factory() as session:
                svc = TaskService(session)
                task = await svc.delete_task(user_id=user_id, title=title)

            if task is None:
                await params.result_callback(
                    {
                        "success": False,
                        "error": f"No pending task matching '{title}' found.",
                    }
                )
                return

            logger.info(
                "delete_task: user_id=%d title=%r task_id=%d",
                user_id,
                title,
                task.id,
            )
            await params.result_callback(_task_payload(task, status="deleted"))
        except Exception:
            logger.exception("delete_task failed for user_id=%d", user_id)
            await params.result_callback(
                {"success": False, "error": "Failed to delete task"}
            )

    async def snooze_task(
        params: FunctionCallParams,
        title: str,
        snooze_until: str,
    ):
        """Snooze a task until a specific date and time.

        Invocation Condition: Call when the user asks to defer, snooze, or
        postpone a task.

        Args:
            title: Description of the task to snooze. Will fuzzy-match
                against the user's pending tasks.
            snooze_until: ISO-8601 datetime with timezone offset for when
                the task should reappear.
        """
        try:
            parsed_snooze_until = _parse_snooze_until(snooze_until)
        except (TypeError, ValueError) as exc:
            await params.result_callback(
                {"success": False, "error": f"Invalid snooze_until: {exc}"}
            )
            return

        try:
            async with async_session_factory() as session:
                svc = TaskService(session)
                task = await svc.snooze_task(
                    user_id=user_id,
                    title=title,
                    snooze_until=parsed_snooze_until,
                )

            if task is None:
                await params.result_callback(
                    {
                        "success": False,
                        "error": f"No pending task matching '{title}' found.",
                    }
                )
                return

            logger.info(
                "snooze_task: user_id=%d title=%r task_id=%d until=%s",
                user_id,
                title,
                task.id,
                task.snoozed_until,
            )
            await params.result_callback(_task_payload(task, status="snoozed"))
        except Exception:
            logger.exception("snooze_task failed for user_id=%d", user_id)
            await params.result_callback(
                {"success": False, "error": "Failed to snooze task"}
            )

    async def unsnooze_task(
        params: FunctionCallParams,
        title: str,
    ):
        """Unsnooze a task by fuzzy-matching its title.

        Invocation Condition: Call when the user wants a snoozed or deferred
        task back on their active list.

        Args:
            title: Description of the snoozed task to reactivate. Will
                fuzzy-match against snoozed tasks.
        """
        try:
            async with async_session_factory() as session:
                svc = TaskService(session)
                task = await svc.unsnooze_task(user_id=user_id, title=title)

            if task is None:
                await params.result_callback(
                    {
                        "success": False,
                        "error": f"No snoozed task matching '{title}' found.",
                    }
                )
                return

            logger.info(
                "unsnooze_task: user_id=%d title=%r task_id=%d",
                user_id,
                title,
                task.id,
            )
            await params.result_callback(_task_payload(task, status="unsnoozed"))
        except Exception:
            logger.exception("unsnooze_task failed for user_id=%d", user_id)
            await params.result_callback(
                {"success": False, "error": "Failed to unsnooze task"}
            )

    # ── Goal tools ───────────────────────────────────────────────────

    async def create_goal(
        params: FunctionCallParams,
        title: str,
        description: str = "",
        target_date: str = "",
    ):
        """Create a higher-level goal for the user.

        Invocation Condition: Call when the user mentions a broader objective
        or something they want to achieve over days or weeks.

        Args:
            title: Short description of the goal.
            description: Optional longer description or context.
            target_date: Optional target completion date in YYYY-MM-DD format.
        """
        try:
            parsed_target_date = _parse_goal_target_date(target_date)
            async with async_session_factory() as session:
                svc = GoalService(session)
                goal = await svc.create_goal(
                    user_id=user_id,
                    title=title,
                    description=description or None,
                    target_date=parsed_target_date,
                )

            logger.info(
                "create_goal: user_id=%d title=%r goal_id=%d",
                user_id,
                title,
                goal.id,
            )
            await params.result_callback(_goal_payload(goal, status="created"))
        except ValueError as exc:
            await params.result_callback({"success": False, "error": str(exc)})
        except Exception:
            logger.exception("create_goal failed for user_id=%d", user_id)
            await params.result_callback(
                {"success": False, "error": "Failed to create goal"}
            )

    async def list_goals(
        params: FunctionCallParams,
        status: str = "",
    ):
        """Get the user's goals, optionally filtered by status.

        Invocation Condition: Call when the user asks about goals,
        objectives, or what they are working toward.

        Args:
            status: Optional filter: active, completed, or abandoned.
        """
        try:
            async with async_session_factory() as session:
                svc = GoalService(session)
                goals = await svc.list_goals(
                    user_id=user_id,
                    status=status or None,
                )

            logger.info(
                "list_goals: user_id=%d status=%s count=%d",
                user_id,
                status or None,
                len(goals),
            )
            await params.result_callback(
                {
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
            )
        except ValueError as exc:
            await params.result_callback({"success": False, "error": str(exc)})
        except Exception:
            logger.exception("list_goals failed for user_id=%d", user_id)
            await params.result_callback(
                {"success": False, "error": "Failed to list goals"}
            )

    async def update_goal(
        params: FunctionCallParams,
        goal_id: int,
        new_title: str = "",
        new_description: str = "",
        new_target_date: str = "",
    ):
        """Update a goal's title, description, or target date.

        Invocation Condition: Call when the user wants to change an existing
        goal. Use goal_id from list_goals results.

        Args:
            goal_id: The ID of the goal to update.
            new_title: New goal title. Omit to keep the current title.
            new_description: New description. Omit to keep the current one.
            new_target_date: New target date in YYYY-MM-DD format.
        """
        try:
            parsed_target_date = _parse_goal_target_date(new_target_date)
            async with async_session_factory() as session:
                svc = GoalService(session)
                goal = await svc.update_goal(
                    goal_id=goal_id,
                    user_id=user_id,
                    new_title=new_title or None,
                    new_description=new_description or None,
                    new_target_date=parsed_target_date,
                )

            if goal is None:
                await params.result_callback(
                    {"success": False, "error": "Goal not found."}
                )
                return

            logger.info(
                "update_goal: user_id=%d goal_id=%d",
                user_id,
                goal_id,
            )
            await params.result_callback(_goal_payload(goal, status="updated"))
        except ValueError as exc:
            await params.result_callback({"success": False, "error": str(exc)})
        except Exception:
            logger.exception(
                "update_goal failed for user_id=%d goal_id=%d", user_id, goal_id
            )
            await params.result_callback(
                {"success": False, "error": "Failed to update goal"}
            )

    async def complete_goal(
        params: FunctionCallParams,
        goal_id: int,
    ):
        """Mark a goal as completed.

        Invocation Condition: Call when the user says they finished or
        achieved a goal. Use goal_id from list_goals results.

        Args:
            goal_id: The ID of the goal to complete.
        """
        try:
            async with async_session_factory() as session:
                svc = GoalService(session)
                goal = await svc.complete_goal(goal_id=goal_id, user_id=user_id)

            if goal is None:
                await params.result_callback(
                    {"success": False, "error": "Goal not found."}
                )
                return

            logger.info(
                "complete_goal: user_id=%d goal_id=%d",
                user_id,
                goal_id,
            )
            await params.result_callback(_goal_payload(goal, status="completed"))
        except Exception:
            logger.exception(
                "complete_goal failed for user_id=%d goal_id=%d", user_id, goal_id
            )
            await params.result_callback(
                {"success": False, "error": "Failed to complete goal"}
            )

    async def abandon_goal(
        params: FunctionCallParams,
        goal_id: int,
    ):
        """Mark a goal as abandoned.

        Invocation Condition: Call when the user decides to drop a goal without
        completing it. Use goal_id from list_goals results.

        Args:
            goal_id: The ID of the goal to abandon.
        """
        try:
            async with async_session_factory() as session:
                svc = GoalService(session)
                goal = await svc.abandon_goal(goal_id=goal_id, user_id=user_id)

            if goal is None:
                await params.result_callback(
                    {"success": False, "error": "Goal not found."}
                )
                return

            logger.info(
                "abandon_goal: user_id=%d goal_id=%d",
                user_id,
                goal_id,
            )
            await params.result_callback(_goal_payload(goal, status="abandoned"))
        except Exception:
            logger.exception(
                "abandon_goal failed for user_id=%d goal_id=%d", user_id, goal_id
            )
            await params.result_callback(
                {"success": False, "error": "Failed to abandon goal"}
            )

    async def delete_goal(
        params: FunctionCallParams,
        goal_id: int,
    ):
        """Permanently delete a goal.

        Invocation Condition: Call only after the user clearly confirms they
        want to permanently remove a goal from their history.

        Args:
            goal_id: The ID of the goal to delete.
        """
        try:
            async with async_session_factory() as session:
                svc = GoalService(session)
                goal = await svc.delete_goal(goal_id=goal_id, user_id=user_id)

            if goal is None:
                await params.result_callback(
                    {"success": False, "error": "Goal not found."}
                )
                return

            logger.info(
                "delete_goal: user_id=%d goal_id=%d",
                user_id,
                goal_id,
            )
            await params.result_callback(_goal_payload(goal, status="deleted"))
        except Exception:
            logger.exception(
                "delete_goal failed for user_id=%d goal_id=%d", user_id, goal_id
            )
            await params.result_callback(
                {"success": False, "error": "Failed to delete goal"}
            )

    # ── Call management tools ────────────────────────────────────────

    async def schedule_callback(
        params: FunctionCallParams,
        minutes_from_now: int,
    ):
        """Schedule a callback call in the specified number of minutes.

        Invocation Condition: Call when the user says "call me back in X
        minutes" or "call me later". This defers the current call and
        schedules a new on-demand call.

        Args:
            minutes_from_now: Number of minutes until the callback.
                Must be between 1 and 120.
        """
        try:
            async with async_session_factory() as session:
                svc = CallManagementService(session)
                result = await svc.schedule_callback(
                    user_id=user_id,
                    minutes_from_now=minutes_from_now,
                    current_call_log_id=call_log_id,
                )

            logger.info(
                "schedule_callback: user_id=%d minutes=%d success=%s",
                user_id,
                minutes_from_now,
                result.success,
            )
            await params.result_callback(
                {"success": result.success, "message": result.message}
            )
        except Exception:
            logger.exception("schedule_callback failed for user_id=%d", user_id)
            await params.result_callback(
                {"success": False, "error": "Failed to schedule callback"}
            )

    async def skip_call(
        params: FunctionCallParams,
        call_type: str,
    ):
        """Skip the next scheduled call of the given type for today.

        Invocation Condition: Call when the user says "skip tonight's call",
        "skip my morning call", etc.

        Args:
            call_type: The type of call to skip.
                Must be "morning", "afternoon", or "evening".
        """
        try:
            async with async_session_factory() as session:
                svc = CallManagementService(session)
                result = await svc.skip_call(
                    user_id=user_id,
                    call_type=call_type,
                )

            logger.info(
                "skip_call: user_id=%d call_type=%s success=%s",
                user_id,
                call_type,
                result.success,
            )
            await params.result_callback(
                {"success": result.success, "message": result.message}
            )
        except Exception:
            logger.exception("skip_call failed for user_id=%d", user_id)
            await params.result_callback(
                {"success": False, "error": "Failed to skip call"}
            )

    async def reschedule_call(
        params: FunctionCallParams,
        call_type: str,
        new_time: str,
    ):
        """Reschedule today's call of the given type to a new time.

        Invocation Condition: Call when the user says "move my morning call
        to 9am" or "reschedule my afternoon call to 3pm".

        Args:
            call_type: The type of call to reschedule.
                Must be "morning", "afternoon", or "evening".
            new_time: The new time in HH:MM format (24-hour, user's local time).
                For example "09:00" or "15:30".
        """
        try:
            parsed_time = dt_time.fromisoformat(new_time)
        except (ValueError, TypeError):
            await params.result_callback(
                {
                    "success": False,
                    "error": f"Invalid time format: {new_time}. Use HH:MM.",
                }
            )
            return

        try:
            async with async_session_factory() as session:
                svc = CallManagementService(session)
                result = await svc.reschedule_call(
                    user_id=user_id,
                    call_type=call_type,
                    new_time=parsed_time,
                )

            logger.info(
                "reschedule_call: user_id=%d call_type=%s new_time=%s success=%s",
                user_id,
                call_type,
                new_time,
                result.success,
            )
            await params.result_callback(
                {"success": result.success, "message": result.message}
            )
        except Exception:
            logger.exception("reschedule_call failed for user_id=%d", user_id)
            await params.result_callback(
                {"success": False, "error": "Failed to reschedule call"}
            )

    async def get_next_call(params: FunctionCallParams):
        """Look up when the user's next scheduled call is.

        Invocation Condition: Call when the user asks "when is my next call?"
        or similar.
        """
        try:
            async with async_session_factory() as session:
                svc = CallManagementService(session)
                result = await svc.get_next_call(user_id=user_id)

            await params.result_callback(
                {"success": result.success, "message": result.message}
            )
        except Exception:
            logger.exception("get_next_call failed for user_id=%d", user_id)
            await params.result_callback(
                {"success": False, "error": "Failed to get next call"}
            )

    async def cancel_all_calls_today(params: FunctionCallParams):
        """Cancel all remaining scheduled calls for today.

        Invocation Condition: Call when the user says "cancel all my calls
        for today" or similar.
        """
        try:
            async with async_session_factory() as session:
                svc = CallManagementService(session)
                result = await svc.cancel_all_calls_today(user_id=user_id)

            logger.info(
                "cancel_all_calls_today: user_id=%d cancelled=%s",
                user_id,
                result.cancelled_count,
            )
            await params.result_callback(
                {
                    "success": result.success,
                    "message": result.message,
                    "cancelled_count": result.cancelled_count,
                }
            )
        except Exception:
            logger.exception("cancel_all_calls_today failed for user_id=%d", user_id)
            await params.result_callback(
                {"success": False, "error": "Failed to cancel calls"}
            )

    # ── Build ToolsSchema and register handlers ──────────────────────

    all_tools = [
        save_call_outcome,
        save_evening_call_outcome,
        save_task,
        complete_task_by_title,
        list_pending_tasks,
        update_task,
        delete_task,
        snooze_task,
        unsnooze_task,
        create_goal,
        list_goals,
        update_goal,
        complete_goal,
        abandon_goal,
        delete_goal,
        schedule_callback,
        skip_call,
        reschedule_call,
        get_next_call,
        cancel_all_calls_today,
    ]

    tools = ToolsSchema(standard_tools=all_tools)

    # Register each direct function on the LLM service
    non_cancellable_tools = {
        "save_call_outcome",
        "save_evening_call_outcome",
        "save_task",
        "complete_task_by_title",
        "update_task",
        "delete_task",
        "snooze_task",
        "unsnooze_task",
        "create_goal",
        "update_goal",
        "complete_goal",
        "abandon_goal",
        "delete_goal",
        "schedule_callback",
        "skip_call",
        "reschedule_call",
        "cancel_all_calls_today",
    }
    for fn in all_tools:
        llm.register_direct_function(
            fn,
            cancel_on_interruption=fn.__name__ not in non_cancellable_tools,
        )

    logger.info(
        "Registered %d voice tools on LLM for call_log_id=%d, user_id=%d",
        len(all_tools),
        call_log_id,
        user_id,
    )

    return tools
