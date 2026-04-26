"""Dashboard API endpoints for the customer-facing web dashboard.

GET  /api/progress       — streak, weekly stats, heatmap, weekly summary
GET  /api/tasks          — task list filtered by status
GET  /api/call-windows   — user's call windows
GET  /api/user/profile   — user profile info
GET  /api/integrations   — integration connection statuses
GET  /api/integrations/{service}/connect — start OAuth flow from dashboard
DELETE /api/integrations/{service}/disconnect — revoke OAuth for a service
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlmodel import func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth.firebase import get_firebase_user
from app.config import get_settings
from app.dependencies import (
    get_call_window_service,
    get_db_session,
    get_goal_service,
    get_task_service,
    get_user_service,
)
from app.models.call_log import CallLog
from app.models.call_window import CallWindow
from app.models.enums import CallLogStatus, CallType, GoalStatus, OutcomeConfidence
from app.models.goal import Goal
from app.models.schemas import FirebasePrincipal
from app.models.task import Task
from app.models.user import User
from app.services.call_window_service import CallWindowService
from app.services.ephemeral_token_service import create_ephemeral_token
from app.services.goal_service import GoalService
from app.services.task_service import TaskService
from app.services.user_service import UserService

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CALENDAR_SCOPES = {"https://www.googleapis.com/auth/calendar"}
GMAIL_SCOPES = {"https://www.googleapis.com/auth/gmail.modify"}
SCHEDULED_CALL_TYPES = (
    CallType.MORNING.value,
    CallType.AFTERNOON.value,
    CallType.EVENING.value,
)
GOAL_CALL_TYPES = (
    CallType.MORNING.value,
    CallType.AFTERNOON.value,
)
GOAL_SUCCESS_CONFIDENCES = (
    OutcomeConfidence.CLEAR.value,
    OutcomeConfidence.PARTIAL.value,
)


class TaskUpdateRequest(BaseModel):
    title: str | None = None
    priority: int | None = None


class TaskSnoozeRequest(BaseModel):
    snooze_until: datetime


class GoalCreateRequest(BaseModel):
    title: str
    description: str | None = None
    target_date: date | None = None


class GoalUpdateRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    target_date: date | None = None


class UserProfileUpdateRequest(BaseModel):
    name: str | None = None
    timezone: str | None = None


class CallWindowRequest(BaseModel):
    window_type: str
    start_time: str
    end_time: str


class CallWindowUpdateRequest(BaseModel):
    start_time: str | None = None
    end_time: str | None = None


async def _resolve_user(
    principal: FirebasePrincipal,
    user_service: UserService,
) -> User:
    """Resolve the authenticated user, raising 404 if not found."""
    user = await user_service.get_by_phone(principal.phone_number)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def _serialize_task(task: Task) -> dict:
    """Serialize a task for dashboard responses."""
    return {
        "id": task.id,
        "title": task.title,
        "priority": task.priority,
        "status": task.status,
        "source": task.source,
        "snoozed_until": task.snoozed_until.isoformat()
        if task.snoozed_until
        else None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }


def _serialize_goal(goal: Goal) -> dict:
    """Serialize a goal for dashboard responses."""
    return {
        "id": goal.id,
        "title": goal.title,
        "description": goal.description,
        "status": goal.status,
        "target_date": goal.target_date.isoformat() if goal.target_date else None,
        "completed_at": goal.completed_at.isoformat() if goal.completed_at else None,
        "created_at": goal.created_at.isoformat() if goal.created_at else None,
    }


def _serialize_call(call: CallLog) -> dict:
    """Serialize a call log for dashboard history responses."""
    return {
        "id": call.id,
        "call_type": call.call_type,
        "call_date": call.call_date.isoformat(),
        "scheduled_time": call.scheduled_time.isoformat(),
        "actual_start_time": call.actual_start_time.isoformat()
        if call.actual_start_time
        else None,
        "end_time": call.end_time.isoformat() if call.end_time else None,
        "status": call.status,
        "occurrence_kind": call.occurrence_kind,
        "attempt_number": call.attempt_number,
        "duration_seconds": call.duration_seconds,
        "goal": call.goal,
        "next_action": call.next_action,
        "commitments": call.commitments,
        "call_outcome_confidence": call.call_outcome_confidence,
        "accomplishments": call.accomplishments,
        "tomorrow_intention": call.tomorrow_intention,
        "reflection_confidence": call.reflection_confidence,
        "recap_sent_at": call.recap_sent_at.isoformat()
        if call.recap_sent_at
        else None,
    }


def _parse_hhmm(value: str, field_name: str) -> time:
    """Parse an HH:MM time string for dashboard call-window edits."""
    try:
        return time.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must be in HH:MM format.",
        ) from exc


def _validate_dashboard_window_type(window_type: str) -> None:
    """Validate a recurring call-window type."""
    valid_types = {CallType.MORNING.value, CallType.AFTERNOON.value, CallType.EVENING.value}
    if window_type not in valid_types:
        raise HTTPException(
            status_code=400,
            detail="window_type must be one of: afternoon, evening, morning.",
        )


async def _rematerialize_call_window(
    session: AsyncSession,
    user: User,
    window_type: str,
) -> None:
    """Best-effort rematerialization after dashboard call-window edits."""
    if not user.onboarding_complete:
        return

    try:
        from app.services.call_materialization_service import rematerialize_future_calls

        await rematerialize_future_calls(
            session,
            user,
            window_type_filter=window_type,
        )
        await session.commit()
    except Exception:
        logger.exception(
            "Dashboard call-window rematerialization failed for user_id=%s type=%s",
            user.id,
            window_type,
        )


def _today_for_user(user: User) -> date:
    """Return today's date in the user's timezone, falling back to UTC."""
    timezone_name = user.timezone or "UTC"
    try:
        tzinfo = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        logger.warning(
            "Invalid timezone %r for user_id=%s; falling back to UTC",
            timezone_name,
            user.id,
        )
        tzinfo = timezone.utc
    return datetime.now(tzinfo).date()


async def _completed_dates(
    session: AsyncSession,
    user_id: int,
    today: date,
) -> set[date]:
    """Return all historical dates where the user completed at least one call."""
    rows = await session.exec(
        select(CallLog.call_date)
        .where(
            CallLog.user_id == user_id,
            CallLog.status == CallLogStatus.COMPLETED.value,
            CallLog.call_date <= today,
        )
        .group_by(CallLog.call_date)
        .order_by(CallLog.call_date)
    )
    return set(rows.all())


def _calculate_streaks(completed_dates: set[date], today: date) -> tuple[int, int]:
    """Calculate current and best daily completion streaks."""
    current_streak = 0
    check_date = today
    while check_date in completed_dates:
        current_streak += 1
        check_date -= timedelta(days=1)

    best_streak = 0
    run_length = 0
    previous_date: date | None = None
    for completed_date in sorted(completed_dates):
        if previous_date is not None and completed_date == previous_date + timedelta(
            days=1
        ):
            run_length += 1
        else:
            run_length = 1
        best_streak = max(best_streak, run_length)
        previous_date = completed_date

    return current_streak, best_streak


async def _count_completed_scheduled_calls(
    session: AsyncSession,
    user_id: int,
    start_date: date,
    end_date: date,
) -> int:
    """Count completed scheduled accountability calls in [start_date, end_date)."""
    result = await session.exec(
        select(func.count(CallLog.id)).where(
            CallLog.user_id == user_id,
            CallLog.status == CallLogStatus.COMPLETED.value,
            CallLog.call_type.in_(SCHEDULED_CALL_TYPES),
            CallLog.call_date >= start_date,
            CallLog.call_date < end_date,
        )
    )
    return result.one()


async def _count_active_call_windows(session: AsyncSession, user_id: int) -> int:
    """Count active scheduled call windows for the user."""
    result = await session.exec(
        select(func.count(CallWindow.id)).where(
            CallWindow.user_id == user_id,
            CallWindow.is_active.is_(True),
        )
    )
    return result.one()


async def _goal_completion_stats(
    session: AsyncSession,
    user_id: int,
    start_date: date,
    end_date: date,
) -> tuple[int, int, int]:
    """Return total goal-capable calls, successful calls, and completion pct."""
    base_filters = (
        CallLog.user_id == user_id,
        CallLog.status == CallLogStatus.COMPLETED.value,
        CallLog.call_type.in_(GOAL_CALL_TYPES),
        CallLog.call_date >= start_date,
        CallLog.call_date < end_date,
    )
    total_result = await session.exec(
        select(func.count(CallLog.id)).where(*base_filters)
    )
    success_result = await session.exec(
        select(func.count(CallLog.id)).where(
            *base_filters,
            CallLog.call_outcome_confidence.in_(GOAL_SUCCESS_CONFIDENCES),
        )
    )
    total = total_result.one()
    successes = success_result.one()
    pct = round((successes / total * 100) if total > 0 else 0)
    return total, successes, pct


# ---------------------------------------------------------------------------
# GET /api/progress
# ---------------------------------------------------------------------------


@router.get("/api/progress")
async def get_progress(
    principal: FirebasePrincipal = Depends(get_firebase_user),
    user_service: UserService = Depends(get_user_service),
    session: AsyncSession = Depends(get_db_session),
):
    """Return progress stats: streak, weekly summary, heatmap, goal completion."""
    user = await _resolve_user(principal, user_service)
    today = _today_for_user(user)

    # --- Heatmap: last 84 days (12 weeks) ---
    heatmap_start = today - timedelta(days=83)
    heatmap_rows = await session.exec(
        select(CallLog.call_date, func.count().label("cnt"))
        .where(
            CallLog.user_id == user.id,
            CallLog.status == CallLogStatus.COMPLETED.value,
            CallLog.call_date >= heatmap_start,
        )
        .group_by(CallLog.call_date)
    )
    completed_by_date: dict[date, int] = {}
    for row in heatmap_rows.all():
        completed_by_date[row[0]] = row[1]

    heatmap = []
    for i in range(84):
        d = heatmap_start + timedelta(days=i)
        count = completed_by_date.get(d, 0)
        # Map count to level 0-4
        if count == 0:
            level = 0
        elif count == 1:
            level = 1
        elif count == 2:
            level = 2
        elif count == 3:
            level = 3
        else:
            level = 4
        heatmap.append({"date": d.isoformat(), "level": level})

    # --- Streaks: all history, not just the 84-day heatmap window ---
    all_completed_dates = await _completed_dates(session, user.id, today)
    streak, best_streak = _calculate_streaks(all_completed_dates, today)

    # --- This week stats ---
    week_start = today - timedelta(days=today.weekday())  # Monday
    prev_week_start = week_start - timedelta(days=7)
    tomorrow = today + timedelta(days=1)

    this_week_calls = await _count_completed_scheduled_calls(
        session,
        user.id,
        week_start,
        tomorrow,
    )
    prev_week_calls = await _count_completed_scheduled_calls(
        session,
        user.id,
        prev_week_start,
        week_start,
    )
    active_call_windows = await _count_active_call_windows(session, user.id)
    weekly_call_total = active_call_windows * 7

    # --- Goal completion ---
    _, goal_successes, goal_pct = await _goal_completion_stats(
        session,
        user.id,
        week_start,
        tomorrow,
    )
    _, _, prev_goal_pct = await _goal_completion_stats(
        session,
        user.id,
        prev_week_start,
        week_start,
    )

    # --- Weekly summary text ---
    if this_week_calls > prev_week_calls:
        trend_text = f" - up from {prev_week_calls}"
    elif this_week_calls == prev_week_calls:
        trend_text = " - same as"
    else:
        trend_text = f" - down from {prev_week_calls}"

    summary = (
        f"You completed {this_week_calls} out of {weekly_call_total} calls this week"
        f"{trend_text} last week. "
        f"You met or partially met goals on {goal_successes} call"
        f"{'s' if goal_successes != 1 else ''}."
    )

    return {
        "streak": {"current": streak, "best": best_streak},
        "week": {
            "calls_completed": this_week_calls,
            "calls_total": weekly_call_total,
            "prev_calls_completed": prev_week_calls,
        },
        "goals": {
            "completion_pct": goal_pct,
            "prev_completion_pct": prev_goal_pct,
        },
        "heatmap": heatmap,
        "weekly_summary": summary,
    }


# ---------------------------------------------------------------------------
# GET /api/tasks
# ---------------------------------------------------------------------------


@router.get("/api/tasks")
async def get_tasks(
    status: str = Query("pending", pattern="^(pending|completed|snoozed)$"),
    principal: FirebasePrincipal = Depends(get_firebase_user),
    user_service: UserService = Depends(get_user_service),
    task_service: TaskService = Depends(get_task_service),
):
    """Return user's tasks filtered by status."""
    user = await _resolve_user(principal, user_service)

    if status == "pending":
        tasks = await task_service.list_pending_tasks(user.id, limit=50)
    elif status == "completed":
        tasks = await task_service.list_completed_tasks(user.id, limit=50)
    else:
        tasks = await task_service.list_snoozed_tasks(user.id, limit=50)

    return {
        "tasks": [_serialize_task(t) for t in tasks]
    }


@router.patch("/api/tasks/{task_id}")
async def update_task(
    task_id: int,
    request: TaskUpdateRequest,
    principal: FirebasePrincipal = Depends(get_firebase_user),
    user_service: UserService = Depends(get_user_service),
    task_service: TaskService = Depends(get_task_service),
):
    """Update a dashboard task's title and/or priority by ID."""
    user = await _resolve_user(principal, user_service)
    try:
        task = await task_service.update_task_by_id(
            user_id=user.id,
            task_id=task_id,
            new_title=request.title,
            new_priority=request.priority,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task": _serialize_task(task)}


@router.post("/api/tasks/{task_id}/complete")
async def complete_task(
    task_id: int,
    principal: FirebasePrincipal = Depends(get_firebase_user),
    user_service: UserService = Depends(get_user_service),
    task_service: TaskService = Depends(get_task_service),
):
    """Mark a dashboard task completed by ID."""
    user = await _resolve_user(principal, user_service)
    task = await task_service.complete_task_by_id(user_id=user.id, task_id=task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task": _serialize_task(task)}


@router.post("/api/tasks/{task_id}/snooze")
async def snooze_task(
    task_id: int,
    request: TaskSnoozeRequest,
    principal: FirebasePrincipal = Depends(get_firebase_user),
    user_service: UserService = Depends(get_user_service),
    task_service: TaskService = Depends(get_task_service),
):
    """Snooze a dashboard task by ID."""
    user = await _resolve_user(principal, user_service)
    try:
        task = await task_service.snooze_task_by_id(
            user_id=user.id,
            task_id=task_id,
            snooze_until=request.snooze_until,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task": _serialize_task(task)}


@router.post("/api/tasks/{task_id}/unsnooze")
async def unsnooze_task(
    task_id: int,
    principal: FirebasePrincipal = Depends(get_firebase_user),
    user_service: UserService = Depends(get_user_service),
    task_service: TaskService = Depends(get_task_service),
):
    """Reactivate a snoozed dashboard task by ID."""
    user = await _resolve_user(principal, user_service)
    task = await task_service.unsnooze_task_by_id(user_id=user.id, task_id=task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task": _serialize_task(task)}


@router.delete("/api/tasks/{task_id}")
async def delete_task(
    task_id: int,
    principal: FirebasePrincipal = Depends(get_firebase_user),
    user_service: UserService = Depends(get_user_service),
    task_service: TaskService = Depends(get_task_service),
):
    """Permanently delete a dashboard task by ID."""
    user = await _resolve_user(principal, user_service)
    task = await task_service.delete_task_by_id(user_id=user.id, task_id=task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task": _serialize_task(task), "status": "deleted"}


# ---------------------------------------------------------------------------
# GET /api/call-windows
# ---------------------------------------------------------------------------


@router.get("/api/call-windows")
async def get_call_windows(
    principal: FirebasePrincipal = Depends(get_firebase_user),
    user_service: UserService = Depends(get_user_service),
    cw_service: CallWindowService = Depends(get_call_window_service),
):
    """Return user's call windows."""
    user = await _resolve_user(principal, user_service)
    windows = await cw_service.list_windows_for_user(user.id)

    return {
        "windows": [
            {
                "type": w.window_type,
                "start_time": w.start_time.strftime("%-I:%M %p")
                if w.start_time
                else None,
                "end_time": w.end_time.strftime("%-I:%M %p") if w.end_time else None,
                "timezone": user.timezone or "UTC",
                "is_active": w.is_active,
            }
            for w in windows
        ]
    }


@router.post("/api/call-windows")
async def create_call_window(
    request: CallWindowRequest,
    principal: FirebasePrincipal = Depends(get_firebase_user),
    user_service: UserService = Depends(get_user_service),
    cw_service: CallWindowService = Depends(get_call_window_service),
    session: AsyncSession = Depends(get_db_session),
):
    """Create or replace one recurring call window from dashboard settings."""
    user = await _resolve_user(principal, user_service)
    _validate_dashboard_window_type(request.window_type)
    try:
        window = await cw_service.save_call_window(
            user_id=user.id,
            window_type=request.window_type,
            start_time=_parse_hhmm(request.start_time, "start_time"),
            end_time=_parse_hhmm(request.end_time, "end_time"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await _rematerialize_call_window(session, user, request.window_type)
    return {
        "window": {
            "type": window.window_type,
            "start_time": window.start_time.strftime("%H:%M"),
            "end_time": window.end_time.strftime("%H:%M"),
            "timezone": user.timezone or "UTC",
            "is_active": window.is_active,
        }
    }


@router.patch("/api/call-windows/{window_type}")
async def update_call_window(
    window_type: str,
    request: CallWindowUpdateRequest,
    principal: FirebasePrincipal = Depends(get_firebase_user),
    user_service: UserService = Depends(get_user_service),
    cw_service: CallWindowService = Depends(get_call_window_service),
    session: AsyncSession = Depends(get_db_session),
):
    """Update one recurring call window from dashboard settings."""
    user = await _resolve_user(principal, user_service)
    _validate_dashboard_window_type(window_type)
    if request.start_time is None and request.end_time is None:
        raise HTTPException(
            status_code=400,
            detail="Provide at least one of start_time or end_time.",
        )

    windows = await cw_service.list_windows_for_user(user.id)
    target = next((window for window in windows if window.window_type == window_type), None)
    if target is None:
        raise HTTPException(status_code=404, detail="Call window not found")

    try:
        window = await cw_service.update_window(
            window_id=target.id,
            start_time=_parse_hhmm(request.start_time, "start_time")
            if request.start_time
            else None,
            end_time=_parse_hhmm(request.end_time, "end_time")
            if request.end_time
            else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await _rematerialize_call_window(session, user, window_type)
    return {
        "window": {
            "type": window.window_type,
            "start_time": window.start_time.strftime("%H:%M"),
            "end_time": window.end_time.strftime("%H:%M"),
            "timezone": user.timezone or "UTC",
            "is_active": window.is_active,
        }
    }


@router.delete("/api/call-windows/{window_type}")
async def delete_call_window(
    window_type: str,
    principal: FirebasePrincipal = Depends(get_firebase_user),
    user_service: UserService = Depends(get_user_service),
    cw_service: CallWindowService = Depends(get_call_window_service),
    session: AsyncSession = Depends(get_db_session),
):
    """Deactivate one recurring call window from dashboard settings."""
    user = await _resolve_user(principal, user_service)
    _validate_dashboard_window_type(window_type)
    windows = await cw_service.list_windows_for_user(user.id)
    target = next((window for window in windows if window.window_type == window_type), None)
    if target is None:
        return {"status": "already_removed", "type": window_type}

    window = await cw_service.deactivate_window(target.id)
    await _rematerialize_call_window(session, user, window_type)
    return {
        "status": "removed",
        "window": {
            "type": window.window_type,
            "start_time": window.start_time.strftime("%H:%M"),
            "end_time": window.end_time.strftime("%H:%M"),
            "timezone": user.timezone or "UTC",
            "is_active": window.is_active,
        },
    }


# ---------------------------------------------------------------------------
# GET /api/user/profile
# ---------------------------------------------------------------------------


@router.get("/api/call-history")
async def get_call_history(
    status: str | None = Query(None),
    call_type: str | None = Query(None),
    limit: int = Query(25, ge=1, le=100),
    principal: FirebasePrincipal = Depends(get_firebase_user),
    user_service: UserService = Depends(get_user_service),
    session: AsyncSession = Depends(get_db_session),
):
    """Return recent call history for the dashboard."""
    user = await _resolve_user(principal, user_service)

    query = select(CallLog).where(CallLog.user_id == user.id)
    if status is not None:
        valid_statuses = {item.value for item in CallLogStatus}
        if status not in valid_statuses:
            raise HTTPException(status_code=400, detail="Invalid call status.")
        query = query.where(CallLog.status == status)
    if call_type is not None:
        valid_call_types = {item.value for item in CallType}
        if call_type not in valid_call_types:
            raise HTTPException(status_code=400, detail="Invalid call type.")
        query = query.where(CallLog.call_type == call_type)

    result = await session.exec(
        query.order_by(
            CallLog.scheduled_time.desc(),  # type: ignore[union-attr]
            CallLog.id.desc(),  # type: ignore[union-attr]
        ).limit(limit)
    )
    calls = list(result.all())
    return {"calls": [_serialize_call(call) for call in calls], "count": len(calls)}


# ---------------------------------------------------------------------------
# GET /api/user/profile
# ---------------------------------------------------------------------------


@router.get("/api/user/profile")
async def get_user_profile(
    principal: FirebasePrincipal = Depends(get_firebase_user),
    user_service: UserService = Depends(get_user_service),
):
    """Return user profile info."""
    user = await _resolve_user(principal, user_service)

    return {
        "name": user.name,
        "phone": user.phone,
        "timezone": user.timezone,
        "onboarding_complete": user.onboarding_complete,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


@router.patch("/api/user/profile")
async def update_user_profile(
    request: UserProfileUpdateRequest,
    principal: FirebasePrincipal = Depends(get_firebase_user),
    user_service: UserService = Depends(get_user_service),
):
    """Update dashboard-editable user profile and preferences."""
    if request.name is None and request.timezone is None:
        raise HTTPException(
            status_code=400,
            detail="Provide at least one of name or timezone.",
        )

    updates: dict[str, object] = {}
    if request.name is not None:
        updates["name"] = request.name.strip() or None
    if request.timezone is not None:
        updates["timezone"] = request.timezone

    try:
        user = await user_service.update_preferences(principal.phone_number, **updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "name": user.name,
        "phone": user.phone,
        "timezone": user.timezone,
        "onboarding_complete": user.onboarding_complete,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------


@router.get("/api/goals")
async def get_goals(
    status: str | None = Query(
        GoalStatus.ACTIVE.value,
        pattern="^(active|completed|abandoned)$",
    ),
    principal: FirebasePrincipal = Depends(get_firebase_user),
    user_service: UserService = Depends(get_user_service),
    goal_service: GoalService = Depends(get_goal_service),
):
    """Return dashboard goals, optionally filtered by status."""
    user = await _resolve_user(principal, user_service)
    goals = await goal_service.list_goals(user_id=user.id, status=status)
    return {"goals": [_serialize_goal(goal) for goal in goals]}


@router.post("/api/goals")
async def create_goal(
    request: GoalCreateRequest,
    principal: FirebasePrincipal = Depends(get_firebase_user),
    user_service: UserService = Depends(get_user_service),
    goal_service: GoalService = Depends(get_goal_service),
):
    """Create a dashboard goal."""
    user = await _resolve_user(principal, user_service)
    try:
        goal = await goal_service.create_goal(
            user_id=user.id,
            title=request.title,
            description=request.description,
            target_date=request.target_date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"goal": _serialize_goal(goal)}


@router.patch("/api/goals/{goal_id}")
async def update_goal(
    goal_id: int,
    request: GoalUpdateRequest,
    principal: FirebasePrincipal = Depends(get_firebase_user),
    user_service: UserService = Depends(get_user_service),
    goal_service: GoalService = Depends(get_goal_service),
):
    """Update a dashboard goal by ID."""
    user = await _resolve_user(principal, user_service)
    try:
        goal = await goal_service.update_goal(
            goal_id=goal_id,
            user_id=user.id,
            new_title=request.title,
            new_description=request.description,
            new_target_date=request.target_date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    return {"goal": _serialize_goal(goal)}


@router.post("/api/goals/{goal_id}/complete")
async def complete_goal(
    goal_id: int,
    principal: FirebasePrincipal = Depends(get_firebase_user),
    user_service: UserService = Depends(get_user_service),
    goal_service: GoalService = Depends(get_goal_service),
):
    """Mark a dashboard goal completed."""
    user = await _resolve_user(principal, user_service)
    goal = await goal_service.complete_goal(goal_id=goal_id, user_id=user.id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    return {"goal": _serialize_goal(goal)}


@router.post("/api/goals/{goal_id}/abandon")
async def abandon_goal(
    goal_id: int,
    principal: FirebasePrincipal = Depends(get_firebase_user),
    user_service: UserService = Depends(get_user_service),
    goal_service: GoalService = Depends(get_goal_service),
):
    """Mark a dashboard goal abandoned."""
    user = await _resolve_user(principal, user_service)
    goal = await goal_service.abandon_goal(goal_id=goal_id, user_id=user.id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    return {"goal": _serialize_goal(goal)}


@router.delete("/api/goals/{goal_id}")
async def delete_goal(
    goal_id: int,
    principal: FirebasePrincipal = Depends(get_firebase_user),
    user_service: UserService = Depends(get_user_service),
    goal_service: GoalService = Depends(get_goal_service),
):
    """Permanently delete a dashboard goal."""
    user = await _resolve_user(principal, user_service)
    goal = await goal_service.delete_goal(goal_id=goal_id, user_id=user.id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    return {"goal": _serialize_goal(goal), "status": "deleted"}


# ---------------------------------------------------------------------------
# GET /api/integrations
# ---------------------------------------------------------------------------


@router.get("/api/integrations")
async def get_integrations(
    principal: FirebasePrincipal = Depends(get_firebase_user),
    user_service: UserService = Depends(get_user_service),
):
    """Return integration connection statuses."""
    user = await _resolve_user(principal, user_service)

    granted = set((user.google_granted_scopes or "").split())
    has_refresh = bool(user.google_refresh_token_encrypted)

    # Determine per-service connection status
    calendar_connected = has_refresh and bool(granted & CALENDAR_SCOPES)
    gmail_connected = has_refresh and bool(granted & GMAIL_SCOPES)

    # Try to extract email from Google tokens (if available)
    google_email = None
    if has_refresh:
        try:
            from app.services.google_oauth_service import build_google_credentials

            build_google_credentials(
                access_token_encrypted=user.google_access_token_encrypted,
                refresh_token_encrypted=user.google_refresh_token_encrypted,
                token_expiry=user.google_token_expiry,
            )
            # The email isn't stored on the User model yet, so we skip it for now
            google_email = None
        except Exception:
            pass

    integrations = []
    if calendar_connected or gmail_connected or has_refresh:
        integrations.append(
            {
                "service": "google_calendar",
                "connected": calendar_connected,
                "email": google_email,
            }
        )
        integrations.append(
            {
                "service": "gmail",
                "connected": gmail_connected,
                "email": google_email,
            }
        )
    else:
        integrations.append({"service": "google_calendar", "connected": False})
        integrations.append({"service": "gmail", "connected": False})

    return {"integrations": integrations}


# ---------------------------------------------------------------------------
# GET /api/integrations/{service}/connect — start OAuth from dashboard
# ---------------------------------------------------------------------------


@router.get("/api/integrations/{service}/connect")
async def connect_integration(
    service: str,
    redirect: bool = Query(
        True,
        description="Return a 302 redirect when true, otherwise return the OAuth URL as JSON.",
    ),
    principal: FirebasePrincipal = Depends(get_firebase_user),
    user_service: UserService = Depends(get_user_service),
):
    """Start Google OAuth flow from the dashboard.

    Unlike the WhatsApp ephemeral-token flow, this uses the Firebase JWT
    directly. Creates an ephemeral token and redirects to /auth/google/start.
    """
    if service not in ("google_calendar", "gmail"):
        raise HTTPException(status_code=400, detail=f"Unknown service: {service}")

    user = await _resolve_user(principal, user_service)

    # Map dashboard service name to OAuth service name
    oauth_service = "calendar" if service == "google_calendar" else "gmail"

    # Create ephemeral token for the existing OAuth flow
    token = await create_ephemeral_token(
        user_id=user.id,
        service=oauth_service,
    )

    settings = get_settings()
    start_url = f"{settings.WEBHOOK_BASE_URL}/auth/google/start?token={token}"

    if not redirect:
        return {"url": start_url}

    return RedirectResponse(url=start_url, status_code=302)


# ---------------------------------------------------------------------------
# DELETE /api/integrations/{service}/disconnect
# ---------------------------------------------------------------------------


@router.delete("/api/integrations/{service}/disconnect")
async def disconnect_integration(
    service: str,
    principal: FirebasePrincipal = Depends(get_firebase_user),
    user_service: UserService = Depends(get_user_service),
    session: AsyncSession = Depends(get_db_session),
):
    """Revoke OAuth tokens for a specific service."""
    user = await _resolve_user(principal, user_service)

    if service not in ("google_calendar", "gmail"):
        raise HTTPException(status_code=400, detail=f"Unknown service: {service}")

    # Map service to scope
    scope_map = {
        "google_calendar": CALENDAR_SCOPES,
        "gmail": GMAIL_SCOPES,
    }
    scopes_to_remove = scope_map[service]

    # Remove the scopes
    granted = set((user.google_granted_scopes or "").split())
    remaining = granted - scopes_to_remove
    user.google_granted_scopes = " ".join(sorted(remaining)) if remaining else None

    # If no scopes remain, clear all OAuth tokens
    if not remaining:
        user.google_access_token_encrypted = None
        user.google_refresh_token_encrypted = None
        user.google_token_expiry = None

    session.add(user)
    await session.commit()

    logger.info(
        "Disconnected %s for user_id=%s, remaining scopes: %s",
        service,
        user.id,
        remaining or "none",
    )
    return {"status": "disconnected", "service": service}
