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

from .onboarding_agent import onboarding_agent
from .tools import complete_task_by_title, list_pending_tasks, save_task

# ---------------------------------------------------------------------------
# Task management tools — thin wrappers around TaskService, auto-wrapped
# as FunctionTool by ADK when added to the tools list.
# ---------------------------------------------------------------------------
_task_tools = [save_task, complete_task_by_title, list_pending_tasks]


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
# Shared generation config — max_output_tokens=400 caps output at ~1600
# characters (~4 chars/token for English), matching the WhatsApp body limit.
# ---------------------------------------------------------------------------
_generation_config = types.GenerateContentConfig(
    max_output_tokens=400,
    temperature=0.3,
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
    model="gemini-3-flash-preview",
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
# Root agent: Charu AI coordinator
# ---------------------------------------------------------------------------
root_agent = Agent(
    name="productivity_assistant",
    model="gemini-3-flash-preview",
    description="Main Charu AI assistant that coordinates onboarding and daily accountability.",
    instruction=(
        "You are Charu, a warm and supportive AI accountability companion "
        "for ADHD adults and remote knowledge workers.\n\n"
        "## Current User State\n"
        "- Name: {user:name?}\n"
        "- Onboarding complete: {user:onboarding_complete?}\n\n"
        "## Behavior Rules\n\n"
        "### If onboarding is NOT complete (onboarding_complete is empty or False):\n"
        "Transfer to the onboarding agent immediately. Do NOT try to handle "
        "onboarding yourself. Do NOT greet the user or add any text — just "
        "transfer. The onboarding agent will handle the greeting.\n\n"
        "### If onboarding IS complete:\n"
        "You are the user's accountability companion. Address them by name. "
        "Help them manage tasks, find information, and streamline daily "
        "workflows. Keep all responses concise.\n\n"
        "Use Google Search to find current information when the user "
        "asks about recent events, news, documentation, or anything "
        "that benefits from up-to-date web results.\n\n"
        "For task management requests (creating, listing, updating, "
        "completing tasks), you can handle them directly using the "
        "task tools or delegate to the task_manager agent."
    ),
    sub_agents=[onboarding_agent, task_manager_agent],
    tools=[google_search] + _task_tools,
    before_tool_callback=_block_tools_before_onboarding,
    generate_content_config=_generation_config,
)
