"""Onboarding Agent — single-agent state-machine for multi-step onboarding.

Uses a single ADK ``Agent`` (not SequentialAgent) with all onboarding tools.
The instruction checks session state to determine which step to handle next,
executing exactly ONE step per conversational turn.

ADK's auto-injected ``transfer_to_agent`` tool handles delegation from the
root agent and the return transfer after step completion.

For the critical onboarding-complete gate, a ``before_agent_callback`` checks
session state first, then falls back to a direct DB read — handling the case
where the DB write succeeded but the state update failed in a previous session.

Flow: name → timezone → morning window → afternoon window → evening window
      → Google Calendar OAuth → Gmail OAuth → finalize onboarding

Requirements: 1, 2, 8, Design Onboarding Flow section
"""

from __future__ import annotations

import logging
from typing import Optional

from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.genai import types

from app.db import async_session_factory
from app.services.user_service import UserService

from .onboarding_tools import (
    check_oauth_status,
    complete_onboarding,
    generate_oauth_url,
    guard_save_user_name,
    infer_timezone_from_phone,
    save_call_window,
    save_user_name,
    save_user_timezone,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL = "gemini-3-flash-preview"

_GENERATION_CONFIG = types.GenerateContentConfig(
    temperature=0.4,
)

# ---------------------------------------------------------------------------
# before_agent_callback: skip if onboarding already complete
# ---------------------------------------------------------------------------


async def _skip_if_onboarding_complete(
    callback_context: CallbackContext,
) -> Optional[types.Content]:
    """Skip the entire onboarding agent if onboarding is already complete.

    Critical gating: checks session state first, then falls back to a
    direct DB read if the state key is absent — handling the case where
    the DB write succeeded but the state update failed in a previous session.
    """
    if callback_context.state.get("user:onboarding_complete"):
        logger.debug("Skipping onboarding — already complete (state)")
        name = callback_context.state.get("user:name", "")
        greeting = f"You're all set, {name}! How can I help you today?" if name else "You're all set! How can I help you today?"
        return types.Content(
            parts=[types.Part(text=greeting)],
            role="model",
        )

    # DB fallback
    phone = callback_context.state.get("phone")
    if phone:
        try:
            async with async_session_factory() as session:
                svc = UserService(session)
                user = await svc.get_by_phone(phone)
                if user is not None and user.onboarding_complete:
                    logger.debug(
                        "Skipping onboarding — already complete (DB fallback)"
                    )
                    callback_context.state["user:onboarding_complete"] = True
                    name = user.name or ""
                    greeting = f"You're all set, {name}! How can I help you today?" if name else "You're all set! How can I help you today?"
                    return types.Content(
                        parts=[types.Part(text=greeting)],
                        role="model",
                    )
        except Exception:
            logger.exception("DB error checking onboarding status for %s", phone)

    return None


# ---------------------------------------------------------------------------
# Onboarding Agent — single agent, all tools, state-driven instruction
# ---------------------------------------------------------------------------

_ONBOARDING_INSTRUCTION = """\
You are Charu, a warm and supportive accountability companion for people \
who struggle with task initiation. You are guiding a new user through setup.

## Your Current State
- Name: {user:name?}
- Timezone: {user:timezone?}
- Morning call time: {user:morning_call_start?}
- Afternoon call time: {user:afternoon_call_start?}
- Evening call time: {user:evening_call_start?}
- Google Calendar connected: {user:google_calendar_connected?}
- Google Gmail connected: {user:google_gmail_connected?}

## Rules

You handle EXACTLY ONE step per message. Find the FIRST incomplete step \
below and handle it. Do NOT move to the next step in the same message.

### Step 1: Collect name (if name is empty)
Greet the user warmly. Ask for their name. When they tell you, call \
save_user_name. Keep it to 1-2 sentences.

### Step 2: Collect timezone (if timezone is empty)
First call infer_timezone_from_phone to get a suggestion based on their \
phone number. If a suggestion is returned, present it to the user for \
confirmation (e.g. "Based on your number, it looks like you might be in \
Asia/Kolkata — is that right?"). If they confirm, call save_user_timezone \
with that timezone. If they correct you, resolve their answer to an IANA \
identifier and call save_user_timezone. If no suggestion, ask what city \
or timezone they're in.

### Step 3: Collect morning call time (if morning_call_start is empty)
Ask what time they'd like their morning accountability call. They just \
need to give a single time like "7 AM" or "8:30 AM". Convert to HH:MM \
format and call save_call_window with window_type='morning', \
start_time=their time, end_time=30 minutes later.

### Step 4: Collect afternoon call time (if afternoon_call_start is empty)
Ask what time they'd like their afternoon check-in call. Convert to \
HH:MM and call save_call_window with window_type='afternoon', \
start_time=their time, end_time=30 minutes later.

### Step 5: Collect evening call time (if evening_call_start is empty)
Ask what time they'd like their evening reflection call. Convert to \
HH:MM and call save_call_window with window_type='evening', \
start_time=their time, end_time=30 minutes later.

### Step 6: Connect Google Calendar (if google_calendar_connected is empty or False)
Explain briefly that connecting Google Calendar helps you see their schedule \
and suggest time blocks. Call generate_oauth_url with service='calendar'. \
Share the link and ask them to click it. When they say they're done, call \
check_oauth_status with service='calendar'. If not connected, gently remind.

### Step 7: Connect Gmail (if google_gmail_connected is empty or False)
Explain briefly that connecting Gmail lets you surface emails needing replies. \
Call generate_oauth_url with service='gmail'. Share the link and ask them \
to click it. When they say they're done, call check_oauth_status with \
service='gmail'. If not connected, gently remind.

### Step 8: Finalize (all above steps complete)
Summarize the user's settings warmly. Call complete_onboarding to finish \
setup. Tell them when to expect their first call based on the tool response. \
Then transfer back to your parent agent productivity_assistant.

## Tone
- Warm, brief, encouraging. No walls of text.
- One question per message. Never ask multiple things at once.
- Never use shame, guilt, or judgment language.
- Address the user by name once you know it.
"""

onboarding_agent = Agent(
    name="onboarding",
    model=MODEL,
    description=(
        "Guides new users through onboarding setup: collecting name, timezone, "
        "call times, Google Calendar and Gmail connections, and finalizing. "
        "Handles one step per turn based on what's already been completed."
    ),
    instruction=_ONBOARDING_INSTRUCTION,
    tools=[
        save_user_name,
        infer_timezone_from_phone,
        save_user_timezone,
        save_call_window,
        generate_oauth_url,
        check_oauth_status,
        complete_onboarding,
    ],
    before_agent_callback=_skip_if_onboarding_complete,
    before_tool_callback=guard_save_user_name,
    generate_content_config=_GENERATION_CONFIG,
)
