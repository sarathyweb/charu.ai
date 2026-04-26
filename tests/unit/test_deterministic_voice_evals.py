"""Deterministic voice eval contracts for production AI behavior."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from pipecat.adapters.schemas.tools_schema import AdapterType

from app.services.anti_habituation import Approach
from app.voice.context import build_system_instruction
from app.voice.tools import register_voice_tools

ROOT = Path(__file__).resolve().parents[2]
VOICE_EVALS = ROOT / "tests" / "evals" / "voice_productivity_eval_cases.json"

EXPECTED_VOICE_TOOLS = {
    "save_call_outcome",
    "save_evening_call_outcome",
    "save_task",
    "complete_task_by_title",
    "list_pending_tasks",
    "update_task",
    "delete_task",
    "snooze_task",
    "unsnooze_task",
    "create_goal",
    "list_goals",
    "update_goal",
    "complete_goal",
    "abandon_goal",
    "delete_goal",
    "get_todays_calendar",
    "get_events_for_date_range",
    "suggest_calendar_time_block",
    "create_calendar_time_block",
    "create_calendar_event",
    "update_calendar_event",
    "delete_calendar_event",
    "check_emails_needing_reply",
    "get_email_for_reply",
    "search_emails",
    "read_email",
    "save_email_draft",
    "update_email_draft",
    "send_approved_reply",
    "compose_email",
    "archive_email",
    "schedule_callback",
    "skip_call",
    "reschedule_call",
    "get_next_call",
    "cancel_all_calls_today",
    "add_call_window",
    "update_call_window",
    "remove_call_window",
    "list_call_windows",
}

EXPECTED_CUSTOM_VOICE_TOOLS = {
    "google_search",
}


class _FakeLLM:
    def __init__(self) -> None:
        self.functions = {}
        self.registration_options = {}

    def register_direct_function(self, fn, **kwargs):
        self.functions[fn.__name__] = fn
        self.registration_options[fn.__name__] = kwargs


class _FakeTask:
    def __init__(self, title: str, priority: int = 50) -> None:
        self.title = title
        self.priority = priority


def _register_voice() -> tuple[_FakeLLM, object]:
    llm = _FakeLLM()
    tools = register_voice_tools(llm, call_log_id=1, user_id=2)
    return llm, tools


def _voice_eval_data() -> dict:
    return json.loads(VOICE_EVALS.read_text(encoding="utf-8"))


def _morning_context() -> dict:
    return {
        "user_name": "Asha",
        "pending_tasks": [_FakeTask("File taxes", 90)],
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
        "new_last_active": None,
        "two_week_variation": None,
        "available_context": {
            "has_calendar": False,
            "has_tasks": True,
            "has_yesterday": False,
        },
    }


def _evening_context() -> dict:
    return {
        "user_name": "Asha",
        "morning_call": None,
        "tasks_completed_today": [],
        "pending_tasks": [],
        "opener": {
            "id": "eve_1",
            "category": "reflective",
            "template": "Hey {name}, how's the day been?",
        },
        "streak_days": 3,
        "new_last_active": None,
        "two_week_variation": None,
    }


def test_voice_tool_registration_matches_current_eval_contract():
    llm, tools = _register_voice()

    assert set(llm.functions) == EXPECTED_VOICE_TOOLS
    assert tools.custom_tools == {
        AdapterType.GEMINI: [{"google_search": {}}],
    }


def test_voice_eval_cases_reference_registered_tools_and_function_args():
    llm, tools = _register_voice()
    data = _voice_eval_data()

    assert data["schema_version"] == 1
    assert len(data["cases"]) >= 10

    for case in data["cases"]:
        for tool_name in case["expected_tools"]:
            assert tool_name in llm.functions
            parameters = set(inspect.signature(llm.functions[tool_name]).parameters)
            parameters.discard("params")
            assert set(case["required_args"][tool_name]) <= parameters

        custom_tools = {
            name
            for custom_tool in tools.custom_tools.get(AdapterType.GEMINI, [])
            for name in custom_tool
        }
        for tool_name in case.get("expected_custom_tools", []):
            assert tool_name in EXPECTED_CUSTOM_VOICE_TOOLS
            assert tool_name in custom_tools


def test_voice_eval_non_cancellable_expectations_match_registration():
    llm, _ = _register_voice()
    data = _voice_eval_data()

    for case in data["cases"]:
        for tool_name in case["non_cancellable_tools"]:
            assert llm.registration_options[tool_name]["cancel_on_interruption"] is False


def test_voice_prompt_expectations_are_present_in_live_instructions():
    data = _voice_eval_data()

    morning_instruction = build_system_instruction("morning", _morning_context())
    evening_instruction = build_system_instruction("evening", _evening_context())

    for phrase in data["prompt_expectations"]["morning"]:
        assert phrase in morning_instruction
    for phrase in data["prompt_expectations"]["evening"]:
        assert phrase in evening_instruction


def test_known_voice_parity_gaps_are_empty_after_full_tools_parity():
    llm, tools = _register_voice()
    data = _voice_eval_data()
    missing = set(data["known_missing_voice_parity_tools"])

    custom_tools = {
        name
        for custom_tool in tools.custom_tools.get(AdapterType.GEMINI, [])
        for name in custom_tool
    }
    available = set(llm.functions) | custom_tools

    assert missing == set()
    assert EXPECTED_CUSTOM_VOICE_TOOLS <= available
