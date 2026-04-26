"""Deterministic onboarding conversation contract tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents.productivity_agent.onboarding_agent import _ONBOARDING_INSTRUCTION
from app.agents.productivity_agent.onboarding_tools import complete_onboarding


def test_onboarding_instruction_is_single_step_and_state_driven():
    """The onboarding agent must advance one persisted step at a time."""
    assert "EXACTLY ONE step per message" in _ONBOARDING_INSTRUCTION
    assert "Find the FIRST incomplete step" in _ONBOARDING_INSTRUCTION

    expected_order = [
        "Step 1: Collect name",
        "Step 2: Collect timezone",
        "Step 3: Collect morning call time",
        "Step 4: Collect afternoon call time",
        "Step 5: Collect evening call time",
        "Step 6: Connect Google Calendar",
        "Step 7: Connect Gmail",
        "Step 8: Finalize",
    ]
    positions = [_ONBOARDING_INSTRUCTION.index(step) for step in expected_order]
    assert positions == sorted(positions)


@pytest.mark.asyncio
async def test_complete_onboarding_blocks_until_required_steps_are_present():
    """Completion should not silently skip call windows or Google setup."""
    tool_context = SimpleNamespace(
        state={
            "phone": "+15551234567",
            "user:name": "Asha",
            "user:timezone": "America/New_York",
            "user:morning_call_start": "08:00",
            "user:google_calendar_connected": True,
        }
    )

    result = await complete_onboarding(tool_context)

    assert "error" in result
    missing = result["error"]
    assert "user:afternoon_call_start" in missing
    assert "user:evening_call_start" in missing
    assert "user:google_gmail_connected" in missing
