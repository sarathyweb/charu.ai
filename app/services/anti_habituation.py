"""Anti-habituation system — opener pools, approach rotation, streak tracking, and variation.

Provides morning and evening opener template pools with context-aware
selection and no-consecutive-repeat constraints.  Also provides approach
rotation (calendar-led, task-led, open question) with the same
no-consecutive-repeat guarantee.  Streak tracking counts consecutive
active days and triggers novelty injection at the 2-week mark (days
10-14).  Both the opener and approach are selected server-side before
each call and injected into the system instruction.

Requirements: 12.1, 12.3, 12.4
Research: .pm/research/26-anti-habituation-design.md
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# OpenerCategory enum
# ---------------------------------------------------------------------------


class OpenerCategory(str, Enum):
    """Categories for opener templates."""

    DIRECT = "direct"
    REFLECTIVE = "reflective"
    CALENDAR = "calendar"
    TASK = "task"
    YESTERDAY = "yesterday"
    LIGHT = "light"
    ENCOURAGE = "encourage"


# ---------------------------------------------------------------------------
# Approach enum — conversational approach for the call
# ---------------------------------------------------------------------------


class Approach(str, Enum):
    """Conversational approaches the agent can lead with."""

    CALENDAR_LED = "calendar_led"
    TASK_LED = "task_led"
    OPEN_QUESTION = "open_question"


#: All valid approach values, ordered for deterministic iteration.
APPROACHES: list[str] = [a.value for a in Approach]


# ---------------------------------------------------------------------------
# Context-dependent categories — require specific data to be available
# ---------------------------------------------------------------------------

_CONTEXT_REQUIREMENTS: dict[OpenerCategory, str] = {
    OpenerCategory.CALENDAR: "has_calendar",
    OpenerCategory.TASK: "has_tasks",
    OpenerCategory.YESTERDAY: "has_yesterday",
}


# ---------------------------------------------------------------------------
# Morning opener pool (10+ openers across all categories)
# ---------------------------------------------------------------------------

MORNING_OPENER_POOL: list[dict[str, str]] = [
    # Direct / Action-oriented
    {
        "id": "direct_1",
        "category": OpenerCategory.DIRECT,
        "template": "Hey {name}, what's the one thing you want to knock out today?",
    },
    {
        "id": "direct_2",
        "category": OpenerCategory.DIRECT,
        "template": "Morning {name}. Let's pick one thing and get it moving.",
    },
    # Reflective / Check-in
    {
        "id": "reflective_1",
        "category": OpenerCategory.REFLECTIVE,
        "template": "Hey {name}. Before we dive in — how are you feeling about today?",
    },
    {
        "id": "reflective_2",
        "category": OpenerCategory.REFLECTIVE,
        "template": "Morning {name}. How's the energy level today?",
    },
    # Calendar-aware (requires calendar context)
    {
        "id": "calendar_1",
        "category": OpenerCategory.CALENDAR,
        "template": (
            "Good morning {name}. I see you've got {meeting_info}"
            " — want to plan around that?"
        ),
    },
    {
        "id": "calendar_2",
        "category": OpenerCategory.CALENDAR,
        "template": (
            "Hey {name}, looks like you have a clear morning until"
            " {next_event}. Great window to get something done."
        ),
    },
    # Task-forward (requires pending tasks)
    {
        "id": "task_1",
        "category": OpenerCategory.TASK,
        "template": (
            "Hey {name}, you mentioned wanting to tackle"
            " {pending_task} — want to start there?"
        ),
    },
    {
        "id": "task_2",
        "category": OpenerCategory.TASK,
        "template": (
            "Morning {name}. You've got {task_count} things on your plate."
            " Which one matters most today?"
        ),
    },
    # Yesterday-reference (requires previous call data)
    {
        "id": "yesterday_1",
        "category": OpenerCategory.YESTERDAY,
        "template": (
            "Hey {name}. Yesterday you were working on"
            " {yesterday_goal} — how'd that go?"
        ),
    },
    {
        "id": "yesterday_2",
        "category": OpenerCategory.YESTERDAY,
        "template": (
            "Morning {name}. Last time we talked, you said you'd"
            " {yesterday_action}. Want to pick up from there?"
        ),
    },
    # Light / Casual
    {
        "id": "light_1",
        "category": OpenerCategory.LIGHT,
        "template": "Hey {name}, ready to get something done today?",
    },
    {
        "id": "light_2",
        "category": OpenerCategory.LIGHT,
        "template": "Morning {name}. What's on your mind today?",
    },
    # Encouragement-led
    {
        "id": "encourage_1",
        "category": OpenerCategory.ENCOURAGE,
        "template": (
            "Hey {name}. You showed up — that's already a win."
            " What do you want to focus on?"
        ),
    },
    {
        "id": "encourage_2",
        "category": OpenerCategory.ENCOURAGE,
        "template": (
            "Good morning {name}. Another day, another chance."
            " What's the plan?"
        ),
    },
]


# ---------------------------------------------------------------------------
# Evening opener pool (10+ variants — calming, reflective, closure-oriented)
# ---------------------------------------------------------------------------

EVENING_OPENER_POOL: list[dict[str, str]] = [
    {
        "id": "eve_1",
        "category": OpenerCategory.REFLECTIVE,
        "template": "Hey {name}, how's the day been?",
    },
    {
        "id": "eve_2",
        "category": OpenerCategory.REFLECTIVE,
        "template": (
            "Evening, {name}. Let's do a quick check-in"
            " before you wind down."
        ),
    },
    {
        "id": "eve_3",
        "category": OpenerCategory.DIRECT,
        "template": "{name}, good to catch you. How did today go?",
    },
    {
        "id": "eve_4",
        "category": OpenerCategory.LIGHT,
        "template": "Hey {name}, let's wrap up the day together.",
    },
    {
        "id": "eve_5",
        "category": OpenerCategory.REFLECTIVE,
        "template": (
            "Hi {name}. Before you call it a day"
            " — how did things go?"
        ),
    },
    {
        "id": "eve_6",
        "category": OpenerCategory.DIRECT,
        "template": (
            "{name}, evening check-in time."
            " What happened today?"
        ),
    },
    {
        "id": "eve_7",
        "category": OpenerCategory.REFLECTIVE,
        "template": (
            "Hey {name}, let's take a minute to look back"
            " at today."
        ),
    },
    {
        "id": "eve_8",
        "category": OpenerCategory.REFLECTIVE,
        "template": "{name}, how are you feeling about today?",
    },
    {
        "id": "eve_9",
        "category": OpenerCategory.LIGHT,
        "template": (
            "Good evening, {name}. Quick reflection"
            " — what stood out today?"
        ),
    },
    {
        "id": "eve_10",
        "category": OpenerCategory.LIGHT,
        "template": (
            "Hey {name}, day's almost done."
            " What did you get into today?"
        ),
    },
    {
        "id": "eve_11",
        "category": OpenerCategory.ENCOURAGE,
        "template": (
            "Hey {name}. You made it through another day."
            " How did it go?"
        ),
    },
]


# ---------------------------------------------------------------------------
# select_opener — context-aware, no-consecutive-repeat selection
# ---------------------------------------------------------------------------


def select_opener(
    opener_pool: list[dict[str, str]],
    last_opener_id: str | None,
    available_context: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Select a random opener that differs from the last one used.

    Algorithm:
      1. Filter openers by context availability (calendar openers only
         if ``has_calendar`` is truthy, etc.).
      2. Exclude the ``last_opener_id`` to prevent consecutive repeats.
      3. If no eligible openers remain after filtering, fall back to
         *any* opener except ``last_opener_id``.
      4. Return a random choice from the eligible set.

    Args:
        opener_pool: Full list of opener dicts (``id``, ``category``,
            ``template``).
        last_opener_id: ID of the opener used in the previous call, or
            ``None`` for the very first call.
        available_context: Dict indicating what context is available,
            e.g. ``{"has_calendar": True, "has_tasks": False, ...}``.
            ``None`` is treated as an empty dict (no context available).

    Returns:
        A single opener dict chosen from the eligible pool.

    Raises:
        ValueError: If ``opener_pool`` is empty.
    """
    if not opener_pool:
        raise ValueError("opener_pool must not be empty")

    if available_context is None:
        available_context = {}

    # Step 1 + 2: filter by context and exclude last opener
    eligible: list[dict[str, str]] = []
    for opener in opener_pool:
        cat = opener["category"]
        # Check if this category requires context that is unavailable
        required_key = _CONTEXT_REQUIREMENTS.get(
            cat if isinstance(cat, OpenerCategory) else OpenerCategory(cat)
        )
        if required_key and not available_context.get(required_key):
            continue
        # Exclude last opener
        if opener["id"] == last_opener_id:
            continue
        eligible.append(opener)

    # Step 3: fallback — any opener except last
    if not eligible:
        eligible = [o for o in opener_pool if o["id"] != last_opener_id]

    # If still empty (pool has exactly 1 opener and it was the last one),
    # allow the single opener through.
    if not eligible:
        eligible = list(opener_pool)

    return random.choice(eligible)


# ---------------------------------------------------------------------------
# Approach context requirements — which approaches need specific data
# ---------------------------------------------------------------------------

_APPROACH_CONTEXT: dict[str, str] = {
    Approach.CALENDAR_LED: "has_calendar_events",
    Approach.TASK_LED: "has_pending_tasks",
}


# ---------------------------------------------------------------------------
# select_approach — context-aware, no-consecutive-repeat selection
# ---------------------------------------------------------------------------


def select_approach(
    last_approach: str | None,
    has_calendar_events: bool = False,
    has_pending_tasks: bool = False,
) -> str:
    """Select today's conversational approach, avoiding consecutive repeats.

    The three approaches are:

    * ``calendar_led`` — lead with the user's schedule (requires calendar
      events to be available).
    * ``task_led`` — lead with pending tasks from the Task_List (requires
      pending tasks to be available).
    * ``open_question`` — start with an open question like "What's most
      important today?" (always available).

    Algorithm:
      1. Build the eligible set from approaches whose context requirements
         are met.  ``open_question`` is always eligible.
      2. Remove ``last_approach`` from the eligible set (no consecutive
         repeats) — but only if alternatives remain.
      3. Return a random choice from the eligible set.

    Args:
        last_approach: The approach used on the previous call, or ``None``
            for the very first call.
        has_calendar_events: Whether the user has calendar events today.
        has_pending_tasks: Whether the user has pending tasks.

    Returns:
        One of ``"calendar_led"``, ``"task_led"``, or ``"open_question"``.
    """
    # Step 1: build eligible set based on available context
    eligible: list[str] = []
    if has_calendar_events:
        eligible.append(Approach.CALENDAR_LED)
    if has_pending_tasks:
        eligible.append(Approach.TASK_LED)
    eligible.append(Approach.OPEN_QUESTION)  # always available

    # Step 2: remove last_approach if we have alternatives
    if last_approach in eligible and len(eligible) > 1:
        eligible.remove(last_approach)

    return random.choice(eligible)



# ---------------------------------------------------------------------------
# Two-week variation types
# ---------------------------------------------------------------------------


class TwoWeekVariationType(str, Enum):
    """Types of novelty injection at the 2-week mark."""

    REVERSE_CALL = "reverse_call"
    MICRO_GOAL = "micro_goal"
    CELEBRATION = "celebration"
    PATTERN_INSIGHT = "pattern_insight"


#: Variation configs keyed by type.  Each contains an
#: ``instruction_override`` string that is injected into the agent's
#: system instruction when the variation is active.
_TWO_WEEK_VARIATIONS: list[dict[str, str]] = [
    {
        "type": TwoWeekVariationType.REVERSE_CALL,
        "instruction_override": (
            "Start by asking what they accomplished yesterday"
            " before discussing today."
        ),
    },
    {
        "type": TwoWeekVariationType.MICRO_GOAL,
        "instruction_override": (
            "Skip the big goal. Focus on one tiny 15-minute task."
        ),
    },
    {
        "type": TwoWeekVariationType.CELEBRATION,
        "instruction_override": (
            "Acknowledge their {streak_days}-day streak warmly"
            " before proceeding."
        ),
    },
    {
        "type": TwoWeekVariationType.PATTERN_INSIGHT,
        "instruction_override": (
            "Share an observation about their patterns from"
            " recent calls."
        ),
    },
]


# ---------------------------------------------------------------------------
# update_streak — consecutive active days counter
# ---------------------------------------------------------------------------


def update_streak(
    consecutive_active_days: int,
    last_active_date: date | None,
    today: date,
) -> tuple[int, date]:
    """Update and return the user's consecutive active day count.

    Pure function — takes the current streak state and returns the new
    state without mutating anything.  The caller is responsible for
    persisting the returned values to the ``User`` model.

    Rules:
      * If ``last_active_date`` is ``None`` (first ever call), the
        streak starts at 1.
      * If ``last_active_date`` is yesterday, the streak increments.
      * If ``last_active_date`` is today, the streak is unchanged
        (already counted).
      * Otherwise the streak resets to 1 (gap detected).

    Args:
        consecutive_active_days: Current streak count stored on the user.
        last_active_date: Date of the user's last active day, or ``None``.
        today: The current date (caller provides for testability).

    Returns:
        A ``(new_streak, new_last_active_date)`` tuple.
    """
    if last_active_date is None:
        return 1, today

    if last_active_date == today:
        # Already counted today — no change.
        return consecutive_active_days, today

    if last_active_date == today - timedelta(days=1):
        return consecutive_active_days + 1, today

    # Gap — streak broken.
    return 1, today


# ---------------------------------------------------------------------------
# get_two_week_variation — novelty injection at days 10-14
# ---------------------------------------------------------------------------


def get_two_week_variation(streak_days: int) -> dict[str, str] | None:
    """Return a variation config if the user is in the 10-14 day window.

    When the user's ``consecutive_active_days`` is between 10 and 14
    inclusive, a random variation is selected from the pool.  Outside
    that range, ``None`` is returned (no variation).

    The ``instruction_override`` in the returned dict may contain a
    ``{streak_days}`` placeholder that the caller should format with
    the actual streak count before injecting into the system instruction.

    Args:
        streak_days: The user's current consecutive active day count.

    Returns:
        A variation dict with ``type`` and ``instruction_override``
        keys, or ``None`` if no variation should be applied.
    """
    if streak_days < 10 or streak_days > 14:
        return None

    return random.choice(_TWO_WEEK_VARIATIONS)
