"""Pre-call context injection — build system instructions for voice calls.

Fetches user data, tasks, calendar events, yesterday's outcome, and
anti-habituation selections to assemble a fully personalised system
instruction before the Pipecat pipeline starts.

Morning/afternoon calls: tasks, calendar events, yesterday's outcome,
opener + approach selection.

Evening calls: morning goal/next_action, tasks completed today,
pending tasks.

Requirements: 4, 10, 12, 20
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from time import perf_counter
from zoneinfo import ZoneInfo
from typing import Any

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.call_log import CallLog
from app.models.enums import CallLogStatus, TaskStatus
from app.models.task import Task
from app.models.user import User
from app.services.anti_habituation import (
    EVENING_OPENER_POOL,
    MORNING_OPENER_POOL,
    Approach,
    get_two_week_variation,
    select_approach,
    select_opener,
    update_streak,
)
from app.services.google_calendar_read_service import (
    fetch_todays_events,
    format_events_for_agent,
)
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)

_VOICE_CONTEXT_CALENDAR_TIMEOUT_SECONDS = 1.5


# ---------------------------------------------------------------------------
# Helper: check if user has calendar connected
# ---------------------------------------------------------------------------


def _has_calendar(user: User) -> bool:
    """Return True if the user has Google Calendar connected."""
    scopes = (user.google_granted_scopes or "")
    return "calendar" in scopes


def _user_today(user: User) -> date:
    """Return today's date in the user's local timezone (falls back to UTC)."""
    if user.timezone:
        try:
            return datetime.now(ZoneInfo(user.timezone)).date()
        except (KeyError, Exception):
            pass
    return datetime.now(timezone.utc).date()


def _is_user_weekend(user: User) -> bool:
    """Return True when it is Saturday/Sunday in the user's timezone."""
    return _user_today(user).weekday() >= 5


# ---------------------------------------------------------------------------
# Helper: fetch yesterday's completed call outcome
# ---------------------------------------------------------------------------


async def _fetch_yesterday_outcome(
    user_id: int,
    session: AsyncSession,
) -> CallLog | None:
    """Return the most recent completed morning/afternoon call from yesterday or today."""
    # Look back up to 2 days to catch yesterday's call
    cutoff = datetime.now(timezone.utc) - timedelta(days=2)
    result = await session.exec(
        select(CallLog)
        .where(
            CallLog.user_id == user_id,
            CallLog.call_type.in_(["morning", "afternoon"]),  # type: ignore[union-attr]
            CallLog.status == CallLogStatus.COMPLETED.value,
            CallLog.actual_start_time >= cutoff,
            CallLog.call_type != "evening",
        )
        .order_by(CallLog.actual_start_time.desc())  # type: ignore[union-attr]
        .limit(1)
    )
    return result.first()


# ---------------------------------------------------------------------------
# Helper: fetch today's morning/afternoon outcome for evening context
# ---------------------------------------------------------------------------


async def _fetch_today_morning_outcome(
    user_id: int,
    today_date: date,
    session: AsyncSession,
) -> CallLog | None:
    """Return today's completed morning/afternoon call (for evening context)."""
    result = await session.exec(
        select(CallLog)
        .where(
            CallLog.user_id == user_id,
            CallLog.call_type.in_(["morning", "afternoon"]),  # type: ignore[union-attr]
            CallLog.status == CallLogStatus.COMPLETED.value,
            CallLog.call_date == today_date,
        )
        .order_by(CallLog.actual_start_time.desc())  # type: ignore[union-attr]
        .limit(1)
    )
    return result.first()


# ---------------------------------------------------------------------------
# Helper: fetch tasks completed today
# ---------------------------------------------------------------------------


async def _fetch_tasks_completed_today(
    user_id: int,
    today_date: date,
    session: AsyncSession,
    user_tz: str | None = None,
) -> list[Task]:
    """Return tasks completed today (using the user's local midnight)."""
    tz = timezone.utc
    if user_tz:
        try:
            tz = ZoneInfo(user_tz)  # type: ignore[assignment]
        except (KeyError, Exception):
            pass
    # Midnight in the user's local timezone, converted to UTC for the query
    start_of_day = datetime.combine(today_date, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
    result = await session.exec(
        select(Task)
        .where(
            Task.user_id == user_id,
            Task.status == TaskStatus.COMPLETED.value,
            Task.completed_at >= start_of_day,
        )
        .limit(10)
    )
    return list(result.all())


# ---------------------------------------------------------------------------
# Morning/afternoon context builder
# ---------------------------------------------------------------------------


async def build_morning_context(
    user: User,
    session: AsyncSession,
    current_call: CallLog | None = None,
) -> dict[str, Any]:
    """Gather all context needed for a morning/afternoon call.

    Returns a dict with keys used by ``build_system_instruction``:
    - user_name, pending_tasks, calendar_context, yesterday_outcome
    - opener (dict), approach (str), streak_days (int)
    - two_week_variation (str | None)
    """
    user_id = user.id
    assert user_id is not None

    calendar_task: asyncio.Task[tuple[str, bool]] | None = None

    # 1. Pending tasks
    task_svc = TaskService(session)
    if _has_calendar(user) and user.timezone:
        calendar_task = asyncio.create_task(_fetch_calendar_context(user, session))

    try:
        pending_tasks = await task_svc.list_pending_tasks(user_id, limit=5)

        # 3. Yesterday's outcome
        yesterday_call = await _fetch_yesterday_outcome(user_id, session)

        # 4. Calendar events (best-effort — don't fail the call if calendar errors)
        calendar_text = "Calendar not connected."
        has_calendar_events = False
        if calendar_task is not None:
            calendar_text, has_calendar_events = await calendar_task
    finally:
        if calendar_task is not None and not calendar_task.done():
            calendar_task.cancel()

    # 5. Anti-habituation: available context flags
    has_yesterday = (
        yesterday_call is not None
        and yesterday_call.goal is not None
    )
    available_context: dict[str, Any] = {
        "has_calendar": has_calendar_events,
        "has_tasks": len(pending_tasks) > 0,
        "has_yesterday": has_yesterday,
    }

    # 6. Select opener
    opener = select_opener(
        MORNING_OPENER_POOL,
        user.last_opener_id,
        available_context,
    )

    # 7. Select approach
    approach = select_approach(
        user.last_approach,
        has_calendar_events=has_calendar_events,
        has_pending_tasks=len(pending_tasks) > 0,
    )

    # 8. Streak tracking
    today = _user_today(user)
    new_streak, new_last_active = update_streak(
        user.consecutive_active_days,
        user.last_active_date,
        today,
    )

    # 9. Two-week variation
    variation = get_two_week_variation(new_streak)
    variation_text: str | None = None
    if variation:
        override = variation["instruction_override"]
        if isinstance(override, str) and "{streak_days}" in override:
            override = override.replace("{streak_days}", str(new_streak))
        variation_text = override

    # Build context dict
    return {
        "user_name": user.name or "there",
        "pending_tasks": pending_tasks,
        "calendar_context": calendar_text,
        "yesterday_call": yesterday_call,
        "has_yesterday": has_yesterday,
        "opener": opener,
        "approach": approach,
        "streak_days": new_streak,
        "new_last_active": new_last_active,
        "two_week_variation": variation_text,
        "available_context": available_context,
        "is_weekend": _is_user_weekend(user),
        "current_call": current_call,
    }


async def _fetch_calendar_context(
    user: User,
    session: AsyncSession,
) -> tuple[str, bool]:
    """Fetch calendar context for live voice calls with a small startup budget.

    Calendar enrichment is helpful but optional. The live call should greet the
    user even if Google Calendar is slow or retryable errors occur.
    """
    user_id = user.id
    assert user_id is not None

    started_at = perf_counter()

    try:
        async with asyncio.timeout(_VOICE_CONTEXT_CALENDAR_TIMEOUT_SECONDS):
            events = await fetch_todays_events(user, session, max_retries=0)
    except TimeoutError:
        logger.warning(
            "prepare_call_context: calendar fetch timed out for user %s after %.0fms",
            user_id,
            _VOICE_CONTEXT_CALENDAR_TIMEOUT_SECONDS * 1000,
        )
        return "Could not fetch calendar events.", False
    except Exception:
        logger.exception("Failed to fetch calendar events for user %s", user_id)
        return "Could not fetch calendar events.", False
    finally:
        logger.info(
            "prepare_call_context: calendar fetch finished for user %s in %.1fms",
            user_id,
            (perf_counter() - started_at) * 1000,
        )

    if isinstance(events, list):
        return format_events_for_agent(events, user.timezone or "UTC"), len(events) > 0

    logger.warning(
        "prepare_call_context: calendar fetch returned structured error for user %s: %s",
        user_id,
        events.get("error") if isinstance(events, dict) else "unknown",
    )
    return "Could not fetch calendar events.", False


# ---------------------------------------------------------------------------
# Evening context builder
# ---------------------------------------------------------------------------


async def build_evening_context(
    user: User,
    session: AsyncSession,
) -> dict[str, Any]:
    """Gather all context needed for an evening reflection call.

    Returns a dict with keys used by ``build_system_instruction``:
    - user_name, morning_goal, morning_next_action
    - tasks_completed_today, pending_tasks
    - opener (dict), streak_days (int)
    - two_week_variation (str | None)
    """
    user_id = user.id
    assert user_id is not None

    today = _user_today(user)

    # 1. Today's morning/afternoon outcome
    morning_call = await _fetch_today_morning_outcome(user_id, today, session)

    # 2. Tasks completed today
    tasks_completed = await _fetch_tasks_completed_today(user_id, today, session, user.timezone)

    # 3. Pending tasks
    task_svc = TaskService(session)
    pending_tasks = await task_svc.list_pending_tasks(user_id, limit=5)

    # 4. Select evening opener
    opener = select_opener(
        EVENING_OPENER_POOL,
        user.last_opener_id,
        available_context=None,  # evening openers don't have context requirements
    )

    # 5. Streak tracking
    new_streak, new_last_active = update_streak(
        user.consecutive_active_days,
        user.last_active_date,
        today,
    )

    # 6. Two-week variation
    variation = get_two_week_variation(new_streak)
    variation_text: str | None = None
    if variation:
        override = variation["instruction_override"]
        if isinstance(override, str) and "{streak_days}" in override:
            override = override.replace("{streak_days}", str(new_streak))
        variation_text = override

    return {
        "user_name": user.name or "there",
        "morning_call": morning_call,
        "tasks_completed_today": tasks_completed,
        "pending_tasks": pending_tasks,
        "opener": opener,
        "streak_days": new_streak,
        "new_last_active": new_last_active,
        "two_week_variation": variation_text,
        "is_weekend": _is_user_weekend(user),
    }


# ---------------------------------------------------------------------------
# Format helpers for system instruction sections
# ---------------------------------------------------------------------------


def _format_tasks_section(tasks: list[Task]) -> str:
    """Format pending tasks into a concise context string."""
    if not tasks:
        return "No pending tasks."
    lines = [f"Pending tasks ({len(tasks)}):"]
    for t in tasks[:5]:
        lines.append(f"- {t.title} (priority: {t.priority})")
    return "\n".join(lines)


def _format_yesterday_section(call: CallLog | None) -> str:
    """Format yesterday's call outcome into context."""
    if call is None:
        return ""
    parts: list[str] = []
    if call.goal:
        parts.append(f"Yesterday's goal: {call.goal}")
    if call.next_action:
        parts.append(f"Yesterday's next action: {call.next_action}")
    if call.call_outcome_confidence:
        parts.append(f"Yesterday's outcome confidence: {call.call_outcome_confidence}")
    return "\n".join(parts) if parts else ""


def _format_morning_outcome_section(call: CallLog | None) -> str:
    """Format today's morning outcome for evening context."""
    if call is None:
        return "No morning call today (user may have missed it)."
    parts: list[str] = []
    if call.goal:
        parts.append(f"Morning goal: {call.goal}")
    if call.next_action:
        parts.append(f"Morning next action: {call.next_action}")
    if not parts:
        return "Morning call completed but no specific goal was set."
    return "\n".join(parts)


def _format_completed_tasks_section(tasks: list[Task]) -> str:
    """Format tasks completed today."""
    if not tasks:
        return "No tasks completed today."
    titles = ", ".join(t.title for t in tasks[:5])
    return f"Tasks completed today: {titles}"


def _format_approach_guidance(approach: str) -> str:
    """Return approach-specific guidance for the system instruction."""
    if approach == Approach.CALENDAR_LED:
        return (
            "Lead with the user's schedule today. Help them find gaps "
            "for focused work around their meetings."
        )
    if approach == Approach.TASK_LED:
        return (
            "Lead with their pending tasks. Help them pick the most "
            "important one to focus on today."
        )
    return (
        "Start with an open question about what matters most today. "
        "Let the user guide the direction."
    )


# ---------------------------------------------------------------------------
# System instruction builders
# ---------------------------------------------------------------------------


def build_system_instruction(
    call_type: str,
    context: dict[str, Any],
) -> str:
    """Build the full system instruction for a voice call.

    Args:
        call_type: One of ``morning``, ``afternoon``, ``evening``, ``on_demand``.
        context: Dict returned by ``build_morning_context`` or
            ``build_evening_context``.

    Returns:
        A complete system instruction string ready for
        ``GeminiLiveLLMService``.
    """
    if call_type == "evening":
        return _build_evening_instruction(context)
    return _build_morning_instruction(call_type, context)


def _build_morning_instruction(
    call_type: str,
    ctx: dict[str, Any],
) -> str:
    """Build system instruction for morning/afternoon/on_demand calls."""
    user_name = ctx["user_name"]
    opener = ctx["opener"]
    approach = ctx["approach"]
    streak_days = ctx["streak_days"]
    variation = ctx.get("two_week_variation")

    # Sections
    tasks_section = _format_tasks_section(ctx["pending_tasks"])
    calendar_section = ctx["calendar_context"]
    yesterday_section = _format_yesterday_section(ctx.get("yesterday_call"))
    approach_guidance = _format_approach_guidance(approach)

    # Opener template — fill in available placeholders
    opener_template = opener["template"]
    opener_text = _safe_format_opener(opener_template, ctx)

    duration = "5" if call_type != "evening" else "3"
    wrapup = "4" if call_type != "evening" else "2"

    parts: list[str] = [
        f"You are Charu, a warm and supportive accountability companion for {user_name}.",
        "",
        f"## Call Configuration",
        f"- Call type: {call_type}",
        f"- Max duration: {duration} minutes",
        f"- Streak: {streak_days} consecutive active days",
        "",
        f"## Your Opening",
        f"Start the call with this opener (speak it naturally, don't read it robotically):",
        f'"{opener_text}"',
        "",
        f"## Today's Approach",
        approach_guidance,
        "",
        f"## User Context",
        tasks_section,
        "",
        calendar_section,
    ]

    if yesterday_section:
        parts.extend(["", yesterday_section])

    if variation:
        parts.extend([
            "",
            "## Special Variation",
            variation,
        ])

    if ctx.get("is_weekend"):
        parts.extend([
            "",
            "## Weekend Mode",
            "Keep the call lighter and more optional. Prioritize personal "
            "tasks, recovery, and one small satisfying action over work pressure.",
        ])

    current_call = ctx.get("current_call")
    if current_call and getattr(current_call, "goal", None):
        parts.extend([
            "",
            "## Proactive Call Reason",
            f"This call was scheduled because: {current_call.goal}",
        ])
        if current_call.next_action:
            parts.append(f"Suggested next action: {current_call.next_action}")
        if current_call.commitments:
            parts.append("Reference metadata:")
            parts.extend(f"- {item}" for item in current_call.commitments[:3])
        parts.append(
            "Open by briefly naming the urgent email context, then help the "
            "user decide the response or next task."
        )

    parts.extend([
        "",
        "## Call Flow",
        f"Follow these phases in order:",
        "",
        f"### Phase 1: Greeting (first 30 seconds)",
        f"- Use the opener above",
        f"- Immediately move toward identifying today's goal",
        "",
        f"### Phase 2: Goal Identification (30s – 2min)",
        f"- Help the user identify their most important goal for today",
        f"- If the goal is vague, ask ONE clarifying question",
        f"- Reference pending tasks or calendar events if relevant",
        "",
        f"### Phase 3: Next Action (2min – 3:30)",
        f"- Break the goal into a concrete first step",
        f"- The next action should be startable within 5 minutes of hanging up",
        f"- If still vague: \"Can you make that even smaller?\"",
        "",
        f"### Phase 4: Commitment + Summary (3:30 – {wrapup}:30)",
        f"- Summarize: \"So your goal is [X] and you're starting with [Y]\"",
        f"- Get verbal confirmation",
        f"- Encourage starting immediately",
        "",
        f"### Phase 5: Wrap-Up ({wrapup}:00 – {duration}:00)",
        f"- Brief, warm goodbye",
        f"- You MUST call save_call_outcome before ending the call",
        "",
        "## Rules",
        "- Keep responses SHORT — 1-3 sentences max",
        "- NEVER use shame, guilt, or disappointment language",
        "- If the user drifts, acknowledge briefly then redirect",
        "- If the user has no goal, suggest reviewing pending tasks or picking one small thing",
        "- You MUST call save_call_outcome before ending the call",
        "- Before calling a tool, first say one short sentence telling the user what you're doing",
        "- After a tool returns, immediately tell the user the result in plain language",
        "- Do not go silent before a tool call when a short spoken bridge would help",
        "- When you receive a message starting with [SYSTEM:], treat it as an internal "
        "instruction — do NOT read it aloud or acknowledge it to the user",
        "",
        "## Adaptive Pacing",
        "Read the user's energy from their responses and adapt:",
        "- Tired/low-energy → slow down, simpler questions, lower the bar",
        "- Energized → match energy, move faster, suggest bigger goals",
        "- Frustrated/overwhelmed → acknowledge first, then redirect to something manageable",
        "- Quiet/hesitant → give space, gentle prompts",
        "",
        "## Google Search",
        "Use Google Search when the user asks about recent events, news, "
        "documentation, prices, schedules, or anything that benefits from "
        "up-to-date web results. Summarize conversationally; do not read raw "
        "URLs unless the user asks.",
        "",
        "## Call Window Management",
        "Use add_call_window, update_call_window, remove_call_window, and "
        "list_call_windows when the user wants to permanently manage recurring "
        "call windows. Use reschedule_call only for a one-off change today.",
    ])

    return "\n".join(parts)


def _build_evening_instruction(ctx: dict[str, Any]) -> str:
    """Build system instruction for evening reflection calls."""
    user_name = ctx["user_name"]
    opener = ctx["opener"]
    streak_days = ctx["streak_days"]
    variation = ctx.get("two_week_variation")

    morning_section = _format_morning_outcome_section(ctx.get("morning_call"))
    completed_section = _format_completed_tasks_section(ctx.get("tasks_completed_today", []))
    pending_section = _format_tasks_section(ctx.get("pending_tasks", []))

    opener_template = opener["template"]
    opener_text = _safe_format_opener(opener_template, ctx)

    parts: list[str] = [
        f"You are Charu, a warm and supportive accountability companion for {user_name}.",
        "",
        "## Call Configuration",
        "- Call type: evening reflection",
        "- Max duration: 3 minutes",
        f"- Streak: {streak_days} consecutive active days",
        "",
        "## Your Opening",
        "Start the call with this opener (speak it naturally):",
        f'"{opener_text}"',
        "",
        "## User Context",
        morning_section,
        "",
        completed_section,
        "",
        pending_section,
    ]

    if variation:
        parts.extend([
            "",
            "## Special Variation",
            variation,
        ])

    if ctx.get("is_weekend"):
        parts.extend([
            "",
            "## Weekend Mode",
            "Keep the reflection gentle. Celebrate rest, personal progress, "
            "and small wins without pushing productivity pressure.",
        ])

    parts.extend([
        "",
        "## Call Flow (3 minutes max)",
        "",
        "### Phase 1: Greeting + Accomplishment Check (0:00 – 1:00)",
        "- Use the opener above",
        "- If a morning goal was set, reference it: ask how it went",
        "- If no morning goal, ask openly: \"How was your day? What did you get done?\"",
        "- Listen actively — let the user share without rushing",
        "",
        "### Phase 2: Acknowledgment (1:00 – 1:45)",
        "- Acknowledge whatever the user shares — even partial progress",
        "- NEVER express disappointment or imply the user should have done more",
        "- If the user reports a bad day, normalize it: \"Some days are just like that.\"",
        "",
        "### Phase 3: Tomorrow's Intention (1:45 – 2:30)",
        "- Ask: \"Is there one thing you'd like to prioritize tomorrow?\"",
        "- If provided, save it as a task using save_task",
        "- If the user declines, respect it — don't push",
        "- For bad days, suggest something small: \"What about a 10-minute task?\"",
        "",
        "### Phase 4: Wrap-Up (2:30 – 3:00)",
        "- Summarize accomplishments and tomorrow's intention",
        "- End with warmth: \"Get some rest. You showed up today, and that matters.\"",
        "- You MUST call save_evening_call_outcome before ending the call",
        "",
        "## Rules",
        "- Keep responses SHORT — 1-3 sentences max",
        "- NEVER use shame, guilt, or disappointment language",
        "- The evening call is about closure, not productivity pressure",
        "- You MUST call save_evening_call_outcome before ending the call",
        "- Before calling a tool, first say one short sentence telling the user what you're doing",
        "- After a tool returns, immediately tell the user the result in plain language",
        "- Do not go silent before a tool call when a short spoken bridge would help",
        "- When you receive a message starting with [SYSTEM:], treat it as an internal "
        "instruction — do NOT read it aloud or acknowledge it to the user",
        "",
        "## Google Search",
        "Use Google Search when the user asks about recent events, news, "
        "documentation, prices, schedules, or anything that benefits from "
        "up-to-date web results. Summarize conversationally; do not read raw "
        "URLs unless the user asks.",
        "",
        "## Call Window Management",
        "Use add_call_window, update_call_window, remove_call_window, and "
        "list_call_windows when the user wants to permanently manage recurring "
        "call windows. Use reschedule_call only for a one-off change today.",
        "",
        "## Bad Day Handling",
        "If the user says they accomplished nothing or had a bad day:",
        "- Acknowledge with genuine empathy (not platitudes)",
        "- Do NOT say \"at least you...\" or \"why didn't you...\"",
        "- Gently offer a small win for tomorrow",
        "- If the user declines, respect it: \"No pressure. We'll figure it out in the morning.\"",
        "- End with warmth: \"Get some rest. Tomorrow's a clean slate.\"",
    ])

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Safe opener formatting
# ---------------------------------------------------------------------------


def _safe_format_opener(template: str, ctx: dict[str, Any]) -> str:
    """Format an opener template with available context, using safe defaults.

    Opener templates may contain placeholders like ``{name}``,
    ``{meeting_info}``, ``{pending_task}``, ``{task_count}``,
    ``{yesterday_goal}``, ``{yesterday_action}``, ``{next_event}``.
    Missing values are replaced with sensible defaults so the opener
    never contains raw ``{placeholder}`` text.
    """
    user_name = ctx.get("user_name", "there")
    pending_tasks = ctx.get("pending_tasks", [])

    # Build substitution values
    values: dict[str, str] = {
        "name": user_name,
    }

    # Task-related
    if pending_tasks:
        values["pending_task"] = pending_tasks[0].title
        values["task_count"] = str(len(pending_tasks))
    else:
        values["pending_task"] = "that thing you mentioned"
        values["task_count"] = "a few"

    # Calendar-related
    calendar_text = ctx.get("calendar_context", "")
    if calendar_text and "No events" not in calendar_text and "not connected" not in calendar_text.lower():
        # Extract first event info for openers
        lines = calendar_text.split("\n")
        event_lines = [l.strip("- ") for l in lines if l.startswith("- ")]
        if event_lines:
            values["meeting_info"] = event_lines[0]
            values["next_event"] = event_lines[0].split(":")[0] if ":" in event_lines[0] else event_lines[0]
        else:
            values["meeting_info"] = "some things on your calendar"
            values["next_event"] = "your next event"
    else:
        values["meeting_info"] = "some things on your calendar"
        values["next_event"] = "your next event"

    # Yesterday-related
    yesterday_call = ctx.get("yesterday_call") or ctx.get("morning_call")
    if yesterday_call and hasattr(yesterday_call, "goal") and yesterday_call.goal:
        values["yesterday_goal"] = yesterday_call.goal
        values["yesterday_action"] = yesterday_call.next_action or yesterday_call.goal
    else:
        values["yesterday_goal"] = "your goal"
        values["yesterday_action"] = "work on your plan"

    # Use str.format_map with a defaultdict-like fallback
    try:
        return template.format_map(_SafeDict(values))
    except (KeyError, ValueError):
        # Fallback: return template with {name} replaced at minimum
        return template.replace("{name}", user_name)


class _SafeDict(dict):
    """Dict subclass that returns the key name for missing format keys."""

    def __missing__(self, key: str) -> str:
        return f"{{{key}}}"


# ---------------------------------------------------------------------------
# Main entry point — called from voice.py before pipeline assembly
# ---------------------------------------------------------------------------


async def prepare_call_context(
    user_id: int,
    call_type: str,
    session: AsyncSession,
    call_log_id: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build the system instruction and context for a voice call.

    This is the main entry point called from ``app/api/voice.py``
    before assembling the Pipecat pipeline.

    Args:
        user_id: The user's database ID.
        call_type: One of ``morning``, ``afternoon``, ``evening``,
            ``on_demand``.
        session: Active async DB session.

    Returns:
        A tuple of ``(system_instruction, context_dict)`` where
        ``context_dict`` contains the opener, approach, and streak
        data needed for post-call anti-habituation state updates.
    """
    started_at = perf_counter()
    user = await session.get(User, user_id)
    if user is None:
        logger.error("prepare_call_context: user %d not found", user_id)
        # Return a minimal fallback instruction
        from app.voice.pipeline import _default_instruction
        return _default_instruction(call_type), {}

    current_call = None
    if call_log_id is not None:
        current_call = await session.get(CallLog, call_log_id)

    if call_type == "evening":
        ctx = await build_evening_context(user, session)
    else:
        ctx = await build_morning_context(user, session, current_call=current_call)

    instruction = build_system_instruction(call_type, ctx)
    logger.info(
        "prepare_call_context: built %s context for user %s in %.1fms",
        call_type,
        user_id,
        (perf_counter() - started_at) * 1000,
    )
    return instruction, ctx
