"""Unit tests for app.voice.context — pre-call context injection.

Tests the system instruction builders and formatting helpers without
requiring a database. DB-dependent integration is tested separately.
"""

from __future__ import annotations

import asyncio

# ---------------------------------------------------------------------------
# Helpers to build test objects
# ---------------------------------------------------------------------------
from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.anti_habituation import Approach
from app.voice.context import (
    _format_approach_guidance,
    _format_completed_tasks_section,
    _format_morning_outcome_section,
    _format_tasks_section,
    _format_yesterday_section,
    _safe_format_opener,
    _SafeDict,
    build_morning_context,
    build_system_instruction,
)


@dataclass
class _FakeTask:
    """Lightweight stand-in for Task in unit tests (no SQLAlchemy state)."""
    title: str
    priority: int = 50


@dataclass
class _FakeCallLog:
    """Lightweight stand-in for CallLog in unit tests."""
    goal: str | None = None
    next_action: str | None = None
    call_outcome_confidence: str | None = None
    commitments: list[str] | None = None


def _make_task(title: str, priority: int = 50) -> _FakeTask:
    """Create a Task-like object for testing without SQLAlchemy state."""
    return _FakeTask(title=title, priority=priority)


def _make_call_log(
    goal: str | None = None,
    next_action: str | None = None,
    confidence: str | None = None,
    commitments: list[str] | None = None,
) -> _FakeCallLog:
    """Create a CallLog-like object for testing without SQLAlchemy state."""
    return _FakeCallLog(
        goal=goal,
        next_action=next_action,
        call_outcome_confidence=confidence,
        commitments=commitments,
    )


# ---------------------------------------------------------------------------
# _format_tasks_section
# ---------------------------------------------------------------------------


class TestFormatTasksSection:
    def test_empty_list(self):
        assert _format_tasks_section([]) == "No pending tasks."

    def test_single_task(self):
        result = _format_tasks_section([_make_task("File taxes", 90)])
        assert "File taxes" in result
        assert "priority: 90" in result

    def test_multiple_tasks_capped_at_five(self):
        tasks = [_make_task(f"Task {i}") for i in range(8)]
        result = _format_tasks_section(tasks)
        # Should show count of all 8 but only list 5
        assert "8" in result
        assert "Task 0" in result
        assert "Task 4" in result
        assert "Task 5" not in result


# ---------------------------------------------------------------------------
# _format_yesterday_section
# ---------------------------------------------------------------------------


class TestFormatYesterdaySection:
    def test_none_returns_empty(self):
        assert _format_yesterday_section(None) == ""

    def test_with_goal_and_action(self):
        cl = _make_call_log("Finish report", "Write intro", "clear")
        result = _format_yesterday_section(cl)
        assert "Finish report" in result
        assert "Write intro" in result
        assert "clear" in result

    def test_with_goal_only(self):
        cl = _make_call_log("Finish report")
        result = _format_yesterday_section(cl)
        assert "Finish report" in result


# ---------------------------------------------------------------------------
# _format_morning_outcome_section
# ---------------------------------------------------------------------------


class TestFormatMorningOutcomeSection:
    def test_none_returns_no_morning_call(self):
        result = _format_morning_outcome_section(None)
        assert "No morning call" in result

    def test_with_goal(self):
        cl = _make_call_log("Ship feature", "Write tests")
        result = _format_morning_outcome_section(cl)
        assert "Ship feature" in result
        assert "Write tests" in result

    def test_no_goal_set(self):
        cl = _make_call_log()
        result = _format_morning_outcome_section(cl)
        assert "no specific goal" in result


# ---------------------------------------------------------------------------
# _format_completed_tasks_section
# ---------------------------------------------------------------------------


class TestFormatCompletedTasksSection:
    def test_empty(self):
        assert "No tasks completed" in _format_completed_tasks_section([])

    def test_with_tasks(self):
        tasks = [_make_task("Reply to Sarah"), _make_task("File taxes")]
        result = _format_completed_tasks_section(tasks)
        assert "Reply to Sarah" in result
        assert "File taxes" in result


# ---------------------------------------------------------------------------
# _format_approach_guidance
# ---------------------------------------------------------------------------


class TestFormatApproachGuidance:
    def test_calendar_led(self):
        result = _format_approach_guidance(Approach.CALENDAR_LED)
        assert "schedule" in result.lower()

    def test_task_led(self):
        result = _format_approach_guidance(Approach.TASK_LED)
        assert "task" in result.lower()

    def test_open_question(self):
        result = _format_approach_guidance(Approach.OPEN_QUESTION)
        assert "open question" in result.lower()


# ---------------------------------------------------------------------------
# _safe_format_opener
# ---------------------------------------------------------------------------


class TestSafeFormatOpener:
    def test_simple_name_substitution(self):
        result = _safe_format_opener(
            "Hey {name}, what's up?",
            {"user_name": "Alice"},
        )
        assert "Alice" in result
        assert "{name}" not in result

    def test_missing_placeholder_uses_default(self):
        result = _safe_format_opener(
            "Hey {name}, you mentioned {pending_task}",
            {"user_name": "Bob", "pending_tasks": []},
        )
        assert "Bob" in result
        # Should use fallback for pending_task
        assert "{pending_task}" not in result

    def test_calendar_placeholder(self):
        result = _safe_format_opener(
            "I see you've got {meeting_info}",
            {
                "user_name": "Carol",
                "calendar_context": "Today's calendar:\n- 9:00 AM: Standup",
            },
        )
        assert "{meeting_info}" not in result

    def test_yesterday_placeholder(self):
        cl = _make_call_log("Write proposal", "Draft outline")
        result = _safe_format_opener(
            "Yesterday you were working on {yesterday_goal}",
            {"user_name": "Dave", "yesterday_call": cl},
        )
        assert "Write proposal" in result

    def test_task_count_placeholder(self):
        tasks = [_make_task("A"), _make_task("B"), _make_task("C")]
        result = _safe_format_opener(
            "You've got {task_count} things on your plate",
            {"user_name": "Eve", "pending_tasks": tasks},
        )
        assert "3" in result


# ---------------------------------------------------------------------------
# _SafeDict
# ---------------------------------------------------------------------------


class TestSafeDict:
    def test_existing_key(self):
        d = _SafeDict({"a": "hello"})
        assert d["a"] == "hello"

    def test_missing_key_returns_placeholder(self):
        d = _SafeDict({"a": "hello"})
        assert d["b"] == "{b}"


# ---------------------------------------------------------------------------
# build_system_instruction — morning
# ---------------------------------------------------------------------------


class TestBuildMorningInstruction:
    def _make_ctx(self, **overrides):
        ctx = {
            "user_name": "TestUser",
            "pending_tasks": [_make_task("Task A", 80)],
            "calendar_context": "No events scheduled for today.",
            "yesterday_call": None,
            "has_yesterday": False,
            "opener": {
                "id": "direct_1",
                "category": "direct",
                "template": "Hey {name}, what's the one thing today?",
            },
            "approach": Approach.OPEN_QUESTION,
            "streak_days": 3,
            "new_last_active": date.today(),
            "two_week_variation": None,
            "available_context": {
                "has_calendar": False,
                "has_tasks": True,
                "has_yesterday": False,
            },
        }
        ctx.update(overrides)
        return ctx

    def test_contains_user_name(self):
        instruction = build_system_instruction("morning", self._make_ctx())
        assert "TestUser" in instruction

    def test_contains_opener(self):
        instruction = build_system_instruction("morning", self._make_ctx())
        assert "what's the one thing today" in instruction

    def test_contains_call_flow_phases(self):
        instruction = build_system_instruction("morning", self._make_ctx())
        assert "Phase 1" in instruction
        assert "Phase 2" in instruction
        assert "Phase 3" in instruction
        assert "Phase 4" in instruction
        assert "Phase 5" in instruction

    def test_contains_system_message_rule(self):
        instruction = build_system_instruction("morning", self._make_ctx())
        assert "[SYSTEM:]" in instruction

    def test_contains_save_call_outcome_rule(self):
        instruction = build_system_instruction("morning", self._make_ctx())
        assert "save_call_outcome" in instruction

    def test_contains_tasks(self):
        instruction = build_system_instruction("morning", self._make_ctx())
        assert "Task A" in instruction

    def test_contains_streak(self):
        instruction = build_system_instruction("morning", self._make_ctx(streak_days=7))
        assert "7" in instruction

    def test_two_week_variation_included(self):
        instruction = build_system_instruction(
            "morning",
            self._make_ctx(two_week_variation="Celebrate the streak!"),
        )
        assert "Celebrate the streak!" in instruction
        assert "Special Variation" in instruction

    def test_yesterday_context_included(self):
        cl = _make_call_log("Ship feature", "Write tests", "clear")
        instruction = build_system_instruction(
            "morning",
            self._make_ctx(yesterday_call=cl),
        )
        assert "Ship feature" in instruction

    def test_5_minute_duration_for_morning(self):
        instruction = build_system_instruction("morning", self._make_ctx())
        assert "5 minutes" in instruction

    def test_on_demand_uses_morning_template(self):
        instruction = build_system_instruction("on_demand", self._make_ctx())
        assert "TestUser" in instruction
        assert "Phase 1" in instruction

    def test_on_demand_includes_proactive_email_reason(self):
        current_call = _make_call_log(
            "Urgent email from Mina: Contract needs approval",
            "Reply to Mina with approval or blockers",
            commitments=["gmail_message_id:msg_123", "gmail_thread_id:thr_123"],
        )
        instruction = build_system_instruction(
            "on_demand",
            self._make_ctx(current_call=current_call),
        )
        assert "Proactive Call Reason" in instruction
        assert "Urgent email from Mina" in instruction
        assert "Reply to Mina" in instruction
        assert "gmail_thread_id:thr_123" in instruction

    def test_contains_tool_bridge_rule(self):
        instruction = build_system_instruction("morning", self._make_ctx())
        assert "Before calling a tool" in instruction
        assert "After a tool returns" in instruction

    def test_weekend_mode_included_when_flagged(self):
        instruction = build_system_instruction(
            "morning",
            self._make_ctx(is_weekend=True),
        )
        assert "Weekend Mode" in instruction
        assert "lighter" in instruction


# ---------------------------------------------------------------------------
# build_system_instruction — evening
# ---------------------------------------------------------------------------


class TestBuildEveningInstruction:
    def _make_ctx(self, **overrides):
        ctx = {
            "user_name": "EveUser",
            "morning_call": None,
            "tasks_completed_today": [],
            "pending_tasks": [],
            "opener": {
                "id": "eve_1",
                "category": "reflective",
                "template": "Hey {name}, how's the day been?",
            },
            "streak_days": 5,
            "new_last_active": date.today(),
            "two_week_variation": None,
        }
        ctx.update(overrides)
        return ctx

    def test_contains_user_name(self):
        instruction = build_system_instruction("evening", self._make_ctx())
        assert "EveUser" in instruction

    def test_contains_evening_phases(self):
        instruction = build_system_instruction("evening", self._make_ctx())
        assert "Accomplishment Check" in instruction
        assert "Acknowledgment" in instruction
        assert "Tomorrow's Intention" in instruction
        assert "Wrap-Up" in instruction

    def test_contains_tool_bridge_rule(self):
        instruction = build_system_instruction("evening", self._make_ctx())
        assert "Before calling a tool" in instruction
        assert "After a tool returns" in instruction

    def test_contains_3_minute_duration(self):
        instruction = build_system_instruction("evening", self._make_ctx())
        assert "3 minutes" in instruction

    def test_contains_save_evening_call_outcome(self):
        instruction = build_system_instruction("evening", self._make_ctx())
        assert "save_evening_call_outcome" in instruction

    def test_contains_bad_day_handling(self):
        instruction = build_system_instruction("evening", self._make_ctx())
        assert "Bad Day" in instruction

    def test_morning_goal_referenced(self):
        cl = _make_call_log("Finish report", "Write intro")
        instruction = build_system_instruction(
            "evening",
            self._make_ctx(morning_call=cl),
        )
        assert "Finish report" in instruction

    def test_completed_tasks_shown(self):
        tasks = [_make_task("Reply to Sarah")]
        instruction = build_system_instruction(
            "evening",
            self._make_ctx(tasks_completed_today=tasks),
        )
        assert "Reply to Sarah" in instruction

    def test_system_message_rule(self):
        instruction = build_system_instruction("evening", self._make_ctx())
        assert "[SYSTEM:]" in instruction

    def test_two_week_variation_included(self):
        instruction = build_system_instruction(
            "evening",
            self._make_ctx(two_week_variation="Try a reverse call."),
        )
        assert "Try a reverse call." in instruction

    def test_no_shame_language_rule(self):
        instruction = build_system_instruction("evening", self._make_ctx())
        assert "NEVER use shame" in instruction

    def test_weekend_mode_included_when_flagged(self):
        instruction = build_system_instruction(
            "evening",
            self._make_ctx(is_weekend=True),
        )
        assert "Weekend Mode" in instruction
        assert "gentle" in instruction


@pytest.mark.asyncio
async def test_build_morning_context_times_out_calendar_fetch(monkeypatch):
    user = SimpleNamespace(
        id=42,
        name="TestUser",
        timezone="UTC",
        google_granted_scopes="calendar.readonly",
        last_opener_id=None,
        last_approach=None,
        consecutive_active_days=2,
        last_active_date=date.today(),
    )
    session = AsyncMock()

    async def slow_calendar_fetch(*args, **kwargs):
        await asyncio.sleep(0.05)
        return []

    monkeypatch.setattr(
        "app.voice.context._VOICE_CONTEXT_CALENDAR_TIMEOUT_SECONDS",
        0.01,
    )

    with (
        patch("app.voice.context.TaskService") as task_service_cls,
        patch("app.voice.context._fetch_yesterday_outcome", AsyncMock(return_value=None)),
        patch("app.voice.context.fetch_todays_events", AsyncMock(side_effect=slow_calendar_fetch)),
    ):
        task_service = task_service_cls.return_value
        task_service.list_pending_tasks = AsyncMock(return_value=[])

        ctx = await build_morning_context(user, session)

    assert ctx["calendar_context"] == "Could not fetch calendar events."
    assert ctx["available_context"]["has_calendar"] is False


@pytest.mark.asyncio
async def test_build_morning_context_uses_fast_fail_calendar_retries():
    user = SimpleNamespace(
        id=42,
        name="TestUser",
        timezone="UTC",
        google_granted_scopes="calendar.readonly",
        last_opener_id=None,
        last_approach=None,
        consecutive_active_days=2,
        last_active_date=date.today(),
    )
    session = AsyncMock()

    with (
        patch("app.voice.context.TaskService") as task_service_cls,
        patch("app.voice.context._fetch_yesterday_outcome", AsyncMock(return_value=None)),
        patch("app.voice.context.fetch_todays_events", AsyncMock(return_value=[])) as fetch_events,
    ):
        task_service = task_service_cls.return_value
        task_service.list_pending_tasks = AsyncMock(return_value=[])

        await build_morning_context(user, session)

    fetch_events.assert_awaited_once_with(user, session, max_retries=0)
