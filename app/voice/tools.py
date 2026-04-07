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
from datetime import time as dt_time

from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams

from app.db import async_session_factory
from app.models.call_log import CallLog
from app.services.call_management_service import CallManagementService
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)


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
            await params.result_callback(
                {"success": True, "status": status, "task_id": task.id, "title": task.title}
            )
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
                    {"success": False, "error": f"No pending task matching '{title}' found."}
                )
                return

            logger.info(
                "complete_task_by_title: user_id=%d title=%r task_id=%d",
                user_id,
                title,
                task.id,
            )
            await params.result_callback(
                {"success": True, "status": "completed", "task_id": task.id, "title": task.title}
            )
        except Exception:
            logger.exception("complete_task_by_title failed for user_id=%d", user_id)
            await params.result_callback(
                {"success": False, "error": "Failed to complete task"}
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
                {"success": False, "error": f"Invalid time format: {new_time}. Use HH:MM."}
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
        schedule_callback,
        skip_call,
        reschedule_call,
        get_next_call,
        cancel_all_calls_today,
    ]

    tools = ToolsSchema(standard_tools=all_tools)

    # Register each direct function on the LLM service
    for fn in all_tools:
        llm.register_direct_function(fn)

    logger.info(
        "Registered %d voice tools on LLM for call_log_id=%d, user_id=%d",
        len(all_tools),
        call_log_id,
        user_id,
    )

    return tools
