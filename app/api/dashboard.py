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
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import select, func
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth.firebase import get_firebase_user
from app.dependencies import (
    get_call_window_service,
    get_db_session,
    get_task_service,
    get_user_service,
)
from app.models.call_log import CallLog
from app.models.enums import CallLogStatus, TaskStatus
from app.models.schemas import FirebasePrincipal
from app.models.user import User
from app.services.call_window_service import CallWindowService
from app.services.task_service import TaskService
from app.services.user_service import UserService

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CALENDAR_SCOPES = {"https://www.googleapis.com/auth/calendar"}
GMAIL_SCOPES = {"https://www.googleapis.com/auth/gmail.modify"}


async def _resolve_user(
    principal: FirebasePrincipal,
    user_service: UserService,
) -> User:
    """Resolve the authenticated user, raising 404 if not found."""
    user = await user_service.get_by_phone(principal.phone_number)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


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
    today = date.today()

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

    # --- Current streak ---
    streak = 0
    check_date = today
    while True:
        if completed_by_date.get(check_date, 0) > 0:
            streak += 1
            check_date -= timedelta(days=1)
        else:
            break

    # --- Best streak (from heatmap window) ---
    best_streak = 0
    current_run = 0
    for i in range(84):
        d = heatmap_start + timedelta(days=i)
        if completed_by_date.get(d, 0) > 0:
            current_run += 1
            best_streak = max(best_streak, current_run)
        else:
            current_run = 0

    # --- This week stats ---
    week_start = today - timedelta(days=today.weekday())  # Monday
    prev_week_start = week_start - timedelta(days=7)

    this_week_calls = sum(
        1 for d, c in completed_by_date.items()
        if week_start <= d <= today and c > 0
    )
    prev_week_calls = sum(
        1 for d, c in completed_by_date.items()
        if prev_week_start <= d < week_start and c > 0
    )

    # --- Goal completion ---
    # Count calls with a goal set vs calls completed this week
    this_week_goals_result = await session.exec(
        select(
            func.count().label("total"),
            func.count(CallLog.goal).label("with_goal"),
        )
        .where(
            CallLog.user_id == user.id,
            CallLog.status == CallLogStatus.COMPLETED.value,
            CallLog.call_date >= week_start,
        )
    )
    goals_row = this_week_goals_result.first()
    total_calls_week = goals_row[0] if goals_row else 0
    goals_set = goals_row[1] if goals_row else 0
    goal_pct = round((goals_set / total_calls_week * 100) if total_calls_week > 0 else 0)

    prev_week_goals_result = await session.exec(
        select(
            func.count().label("total"),
            func.count(CallLog.goal).label("with_goal"),
        )
        .where(
            CallLog.user_id == user.id,
            CallLog.status == CallLogStatus.COMPLETED.value,
            CallLog.call_date >= prev_week_start,
            CallLog.call_date < week_start,
        )
    )
    prev_goals_row = prev_week_goals_result.first()
    prev_total = prev_goals_row[0] if prev_goals_row else 0
    prev_goals = prev_goals_row[1] if prev_goals_row else 0
    prev_goal_pct = round((prev_goals / prev_total * 100) if prev_total > 0 else 0)

    # --- Weekly summary text ---
    summary = (
        f"You completed {this_week_calls} out of 7 calls this week"
        f"{f' — up from {prev_week_calls}' if this_week_calls > prev_week_calls else f' — same as'
          if this_week_calls == prev_week_calls else f' — down from {prev_week_calls}'} last week. "
        f"You set goals on {goals_set} day{'s' if goals_set != 1 else ''}."
    )

    return {
        "streak": {"current": streak, "best": best_streak},
        "week": {
            "calls_completed": this_week_calls,
            "calls_total": 7,
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
    status: str = Query("pending", pattern="^(pending|completed)$"),
    principal: FirebasePrincipal = Depends(get_firebase_user),
    user_service: UserService = Depends(get_user_service),
    task_service: TaskService = Depends(get_task_service),
):
    """Return user's tasks filtered by status."""
    user = await _resolve_user(principal, user_service)

    if status == "pending":
        tasks = await task_service.list_pending_tasks(user.id, limit=50)
    else:
        tasks = await task_service.list_completed_tasks(user.id, limit=50)

    return {
        "tasks": [
            {
                "id": t.id,
                "title": t.title,
                "priority": t.priority,
                "status": t.status,
                "source": t.source,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "completed_at": t.completed_at.isoformat() if t.completed_at else None,
            }
            for t in tasks
        ]
    }


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
                "start_time": w.start_time.strftime("%-I:%M %p") if w.start_time else None,
                "end_time": w.end_time.strftime("%-I:%M %p") if w.end_time else None,
                "timezone": user.timezone or "UTC",
                "is_active": w.is_active,
            }
            for w in windows
        ]
    }


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
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


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
            creds = build_google_credentials(
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
        integrations.append({
            "service": "google_calendar",
            "connected": calendar_connected,
            "email": google_email,
        })
        integrations.append({
            "service": "gmail",
            "connected": gmail_connected,
            "email": google_email,
        })
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
    from app.services.ephemeral_token_service import create_ephemeral_token
    from app.config import get_settings
    from fastapi.responses import RedirectResponse

    token = await create_ephemeral_token(
        user_id=user.id,
        service=oauth_service,
    )

    settings = get_settings()
    start_url = f"{settings.WEBHOOK_BASE_URL}/auth/google/start?token={token}"

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
