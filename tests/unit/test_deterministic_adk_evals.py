"""Deterministic ADK eval contracts for production AI behavior."""

from __future__ import annotations

import json
from pathlib import Path

from google.adk.evaluation.local_eval_sets_manager import load_eval_set_from_file
from google.adk.tools import FunctionTool

from app.agents.productivity_agent.agent import root_agent

ROOT = Path(__file__).resolve().parents[2]
ADK_EVALSET = ROOT / "tests" / "evals" / "productivity_assistant.evalset.json"
BACKLOG_REVIEW = ROOT / "tests" / "evals" / "deferred_product_backlog_review.json"

EXPECTED_ROOT_TOOLS = {
    "google_search",
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
    "schedule_callback",
    "skip_call",
    "reschedule_call",
    "get_next_call",
    "cancel_all_calls_today",
    "add_call_window",
    "update_call_window",
    "remove_call_window",
    "list_call_windows",
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
}

REQUIRED_SCENARIO_MARKERS = {
    "onboarding_completion": "I'm new here",
    "task_capture": "Remember that I need to file taxes",
    "task_completion": "I finished filing taxes",
    "goal_management": "Make preparing for Monday's client presentation",
    "calendar_scheduling": "Find me a 45 minute opening",
    "calendar_create": "Schedule a planning event",
    "gmail_reply": "Check whether I have any emails",
    "call_management": "Call me back in 15 minutes",
    "refusal_error_handling": "Send that draft right now",
}

CONFIRMATION_REQUIRED_TOOLS = {
    "delete_task",
    "delete_goal",
    "delete_calendar_event",
    "compose_email",
    "archive_email",
}

DEFERRED_BACKLOG_ITEMS = {
    "weekend_mode",
    "urgent_email_calls",
    "auto_task_from_emails",
    "notion",
    "google_keep",
    "google_tasks",
    "todoist",
}


def _tool_name(tool) -> str:
    return getattr(tool, "__name__", None) or tool.name


def _tool_declaration(tool):
    if hasattr(tool, "_get_declaration"):
        return tool._get_declaration()
    return FunctionTool(tool)._get_declaration()


def _registered_tools() -> dict[str, object]:
    return {_tool_name(tool): tool for tool in root_agent.tools}


def _schema_arg_names(tool) -> set[str] | None:
    declaration = _tool_declaration(tool)
    if declaration is None or declaration.parameters is None:
        return None
    return set(declaration.parameters.properties or {})


def _schema_required_args(tool) -> set[str]:
    declaration = _tool_declaration(tool)
    if declaration is None or declaration.parameters is None:
        return set()
    return set(declaration.parameters.required or [])


def test_root_agent_tool_registration_matches_full_adk_spec():
    assert set(_registered_tools()) == EXPECTED_ROOT_TOOLS


def test_risky_root_agent_tools_still_require_confirmation():
    tools = _registered_tools()

    for name in CONFIRMATION_REQUIRED_TOOLS:
        assert getattr(tools[name], "_require_confirmation", False) is True


def test_adk_evalset_loads_through_official_local_loader():
    eval_set = load_eval_set_from_file(str(ADK_EVALSET), "productivity_assistant")

    assert eval_set.eval_set_id == "productivity_assistant"
    assert [case.eval_id for case in eval_set.eval_cases] == [
        "core_accountability_tool_trajectory"
    ]
    assert len(eval_set.eval_cases[0].conversation) == len(REQUIRED_SCENARIO_MARKERS)


def test_adk_evalset_covers_core_accountability_scenarios():
    raw = json.loads(ADK_EVALSET.read_text(encoding="utf-8"))
    queries = "\n".join(invocation["query"] for case in raw for invocation in case["data"])

    for marker in REQUIRED_SCENARIO_MARKERS.values():
        assert marker in queries


def test_adk_eval_tool_trajectories_reference_registered_tool_schemas():
    raw = json.loads(ADK_EVALSET.read_text(encoding="utf-8"))
    registered = _registered_tools()

    for case in raw:
        for invocation in case["data"]:
            for expected in invocation.get("expected_tool_use", []):
                tool_name = expected["tool_name"]
                assert tool_name in registered

                allowed_args = _schema_arg_names(registered[tool_name])
                if allowed_args is not None:
                    assert set(expected["tool_input"]) <= allowed_args
                    assert _schema_required_args(registered[tool_name]) <= set(
                        expected["tool_input"]
                    )


def test_deferred_product_backlog_review_is_complete_and_actionable():
    review = json.loads(BACKLOG_REVIEW.read_text(encoding="utf-8"))
    items = {item["id"]: item for item in review["items"]}

    assert set(items) == DEFERRED_BACKLOG_ITEMS
    for item in items.values():
        assert item["status"] in {"deferred_needs_spec", "deferred_integration"}
        assert item["decision"]
        assert item["next_step"]
