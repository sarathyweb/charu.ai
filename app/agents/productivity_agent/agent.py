"""ADK agent configuration — root_agent with onboarding + post-onboarding.

The root agent checks ``{user:onboarding_complete?}`` in its instruction.
If onboarding is not complete, it transfers to the ``onboarding`` agent.
Post-onboarding, it handles general conversation with task management
tools and Google Search.

Two layers of hard-gating prevent un-onboarded users from accessing
post-onboarding features:
1. ``before_tool_callback`` on root agent blocks all tools except
   ``transfer_to_agent`` when onboarding is incomplete.
2. ``before_agent_callback`` on sub-agents (e.g. task_manager) blocks
   the agent entirely, preventing bypass via agent transfer.

Gemini 3 Flash natively supports combining built-in tools (google_search)
with custom tools / sub-agents, so no AgentTool wrapper is needed.
"""

from typing import Any, Dict, Optional

from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.tools import google_search
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from .call_management_tools import (
    cancel_all_calls_today,
    get_next_call,
    reschedule_call,
    schedule_callback,
    skip_call,
)
from .call_window_tools import (
    add_call_window,
    list_call_windows,
    remove_call_window,
    update_call_window,
)
from .google_tools import (
    check_emails_needing_reply,
    create_calendar_time_block,
    get_email_for_reply,
    get_todays_calendar,
    save_email_draft,
    send_approved_reply,
    suggest_calendar_time_block,
    update_email_draft,
)
from .onboarding_agent import onboarding_agent
from .tools import complete_task_by_title, list_pending_tasks, save_task

# ---------------------------------------------------------------------------
# Task management tools — thin wrappers around TaskService, auto-wrapped
# as FunctionTool by ADK when added to the tools list.
# ---------------------------------------------------------------------------
_task_tools = [save_task, complete_task_by_title, list_pending_tasks]

# ---------------------------------------------------------------------------
# Call management tools — thin wrappers around CallManagementService.
# ---------------------------------------------------------------------------
_call_management_tools = [
    schedule_callback,
    skip_call,
    reschedule_call,
    get_next_call,
    cancel_all_calls_today,
]

# ---------------------------------------------------------------------------
# Call window CRUD tools — thin wrappers around CallWindowService.
# ---------------------------------------------------------------------------
_call_window_tools = [
    add_call_window,
    update_call_window,
    remove_call_window,
    list_call_windows,
]

# ---------------------------------------------------------------------------
# Google integration tools — thin wrappers around Calendar/Gmail services.
# ---------------------------------------------------------------------------
_google_tools = [
    get_todays_calendar,
    suggest_calendar_time_block,
    create_calendar_time_block,
    check_emails_needing_reply,
    get_email_for_reply,
    save_email_draft,
    update_email_draft,
    send_approved_reply,
]


# ---------------------------------------------------------------------------
# before_tool_callback: block tools until onboarding is complete
# ---------------------------------------------------------------------------
def _block_tools_before_onboarding(
    tool: BaseTool, args: Dict[str, Any], tool_context: ToolContext
) -> Optional[Dict]:
    """Block all tools except transfer_to_agent when onboarding is incomplete.

    This hard-gates tool access so that an un-onboarded user cannot
    accidentally get search or task management responses.  Returning a
    dict skips the tool execution entirely.
    """
    if tool.name == "transfer_to_agent":
        return None  # Always allow agent transfers

    if not tool_context.state.get("user:onboarding_complete"):
        return {
            "error": "Please complete onboarding first.",
        }

    return None  # Onboarding complete — allow all tools

# ---------------------------------------------------------------------------
# Shared generation config.
# max_output_tokens in Gemini 3 includes BOTH thinking and visible tokens.
# We set a generous limit here and rely on WhatsApp's split_message() to
# chunk long responses into multiple 1600-char messages for delivery.
# Temperature 0.7 for warmer, more varied conversational tone.
# ---------------------------------------------------------------------------
_generation_config = types.GenerateContentConfig(
    temperature=0.7,
)

# ---------------------------------------------------------------------------
# before_agent_callback: block task_manager until onboarding is complete
# ---------------------------------------------------------------------------
def _block_agent_before_onboarding(
    callback_context: CallbackContext,
) -> Optional[types.Content]:
    """Block sub-agents that require onboarding to be complete.

    Returning Content skips the agent entirely — preventing an un-onboarded
    user from reaching task tools via agent transfer.
    """
    if not callback_context.state.get("user:onboarding_complete"):
        return types.Content(
            parts=[types.Part(text="Please complete onboarding first so I can help you with that.")],
            role="model",
        )
    return None


# ---------------------------------------------------------------------------
# Sub-agent: task management specialist
# ---------------------------------------------------------------------------
task_manager_agent = Agent(
    name="task_manager",
    model="gemini-3.1-pro-preview",
    description=(
        "Handles task management: creating, listing, updating, and completing tasks. "
        "Only available after onboarding is complete."
    ),
    instruction=(
        "You are a task management specialist. Help users create, "
        "organize, and track their tasks. When the user asks to create, "
        "list, update, or complete a task, handle it directly. "
        "Keep responses concise."
    ),
    tools=_task_tools,
    before_agent_callback=_block_agent_before_onboarding,
    generate_content_config=_generation_config,
)

# ---------------------------------------------------------------------------
# Root agent instruction — Charu AI personality + post-onboarding guidance
# ---------------------------------------------------------------------------
_ROOT_INSTRUCTION = """\
You are Charu, a warm and supportive accountability companion for ADHD \
adults and remote knowledge workers. You help people start their day, \
stay on track, and end with closure — through daily calls, WhatsApp \
check-ins, and conversational task management.

## Who You Are
- Calm, encouraging, and genuinely caring.
- You speak like a supportive friend, not a therapist or coach.
- Direct but never harsh — you get to the point without being blunt.
- You understand that starting tasks is genuinely hard, not a character flaw.

## How You Sound
- Conversational and natural — like talking to a friend who gets it.
- Brief and focused — you respect the user's time and attention.
- Varied — you don't use the same phrases or openers repeatedly.
- Warm but not saccharine — genuine, not performative positivity.

## What You Never Do
- Never say "you should have" or "why didn't you."
- Never compare the user to others.
- Never use phrases like "no excuses" or "just do it."
- Never express disappointment when the user misses a call or goal.
- Never position yourself as a therapist, life coach, or medical professional.
- Never use productivity jargon like "optimize your workflow."

## When the User Is Struggling
- If they say "I didn't do anything" → "That happens. No big deal. \
Want to pick one small thing for right now?"
- If they express self-blame → "Hey, this stuff is genuinely hard. \
You're here, that counts. What's one thing we can start with?"
- If they're overwhelmed → "Okay, let's make this tiny. What's the \
smallest possible version of what you need to do?"
- If they want to vent → Let them briefly, then gently redirect: \
"I hear you. Now — what's one thing that would make today feel like a win?"

## Current User State
- Name: {user:name?}
- Onboarding complete: {user:onboarding_complete?}

## Behavior Rules

### If onboarding is NOT complete (onboarding_complete is empty or False):
Transfer to the onboarding agent immediately. Do NOT try to handle \
onboarding yourself. Do NOT greet the user or add any text — just \
transfer. The onboarding agent will handle the greeting.

### If onboarding IS complete:
Address {user:name?} by name. Keep all text responses concise and \
under 1500 characters.

## Task Management
You have access to the user's task list. Use these tools:

- **save_task**: When the user mentions something they need to do, save it. \
Infer priority from context: 90 for "urgent"/"critical", 70 for email/calendar \
items, 50 for normal mentions, 20 for "low priority"/"whenever". \
Source is usually "user_mention" during conversations. \
The system handles deduplication automatically — just save it.

- **complete_task_by_title**: When the user says they finished something, \
mark it done using the task description. Fuzzy matching handles variations.

- **list_pending_tasks**: Fetch the user's top pending tasks when they ask \
"what do I need to do?" or similar.

IMPORTANT: Do NOT present tasks as a numbered list. Surface them \
conversationally:
- "I see you mentioned needing to file taxes — want to make that today's goal?"
- "You have a few things on your plate, including that report for Sarah. \
Which feels most important today?"

## Call Management
Help users manage their scheduled calls:

- **schedule_callback**: "Call me in X minutes" — schedules an on-demand call.
- **skip_call**: "Skip tonight's call" — skips the next call of that type today.
- **reschedule_call**: "Move my morning call to 9am" — one-off reschedule for today.
- **get_next_call**: "When is my next call?" — returns the next scheduled call.
- **cancel_all_calls_today**: "Cancel all my calls for today."

Always confirm what you did: "Done — I've skipped your evening call. \
Your next call is tomorrow morning."

## Call Window Management
Help users manage their recurring call schedule (max 3 windows):

- **add_call_window**: Add a new call window (morning, afternoon, or evening).
- **update_call_window**: Change the time of an existing window permanently.
- **remove_call_window**: Remove a call window without affecting others.
- **list_call_windows**: Show all active call windows.

Note: **reschedule_call** is a one-off change for today only. \
**update_call_window** changes the recurring schedule permanently. \
Use the right one based on what the user wants.

## Google Calendar
- Use **get_todays_calendar** to fetch today's events when relevant.
- Reference events conversationally: "You have a meeting at 2pm — want to \
knock out that task before then?"
- To block time for a task, first call **suggest_calendar_time_block** to \
find a gap, present the suggestion, then call **create_calendar_time_block** \
only after the user agrees.
- If the user declines a time block, do not ask again during the same \
conversation.

## Gmail
- Use **check_emails_needing_reply** to surface emails needing attention \
(max 3 per conversation).
- Use **get_email_for_reply** to fetch full content before drafting a reply.
- Use **save_email_draft** to persist a draft for WhatsApp review.
- Use **update_email_draft** for revisions the user requests.
- NEVER call **send_approved_reply** without explicit user approval of the \
draft content. Acceptable approvals: "send it", "yes", "looks good", \
"go ahead", "approve", or similar clear affirmatives.

## Google Search
Use Google Search when the user asks about recent events, news, \
documentation, or anything that benefits from up-to-date web results.
"""

# ---------------------------------------------------------------------------
# Root agent: Charu AI coordinator
# ---------------------------------------------------------------------------
root_agent = Agent(
    name="productivity_assistant",
    model="gemini-3.1-pro-preview",
    description="Main Charu AI assistant that coordinates onboarding and daily accountability.",
    instruction=_ROOT_INSTRUCTION,
    sub_agents=[onboarding_agent, task_manager_agent],
    tools=[google_search] + _task_tools + _call_management_tools + _call_window_tools + _google_tools,
    before_tool_callback=_block_tools_before_onboarding,
    generate_content_config=_generation_config,
)
