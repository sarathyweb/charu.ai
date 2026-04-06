"""ADK agent configuration — root_agent with sub-agent and Google Search.

Gemini 3 Flash natively supports combining built-in tools (google_search)
with custom tools / sub-agents, so no AgentTool wrapper is needed.
See: https://ai.google.dev/gemini-api/docs/grounding
"""

from google.adk.agents import Agent
from google.adk.tools import google_search
from google.genai import types

from .tools import complete_task_by_title, list_pending_tasks, save_task

# ---------------------------------------------------------------------------
# Task management tools — thin wrappers around TaskService, auto-wrapped
# as FunctionTool by ADK when added to the tools list.
# ---------------------------------------------------------------------------
_task_tools = [save_task, complete_task_by_title, list_pending_tasks]

# ---------------------------------------------------------------------------
# Shared generation config — cap output to ~400 tokens so replies stay
# within Twilio's 1600-character WhatsApp limit (~4 chars/token for English).
# ---------------------------------------------------------------------------
_generation_config = types.GenerateContentConfig(
    max_output_tokens=400,
    temperature=0.3,
)

# ---------------------------------------------------------------------------
# Sub-agent: task management specialist
# ---------------------------------------------------------------------------
task_manager_agent = Agent(
    name="task_manager",
    model="gemini-3-flash-preview",
    description=(
        "Handles task management: creating, listing, updating, and completing tasks."
    ),
    instruction=(
        "You are a task management specialist. Help users create, "
        "organize, and track their tasks. When the user asks to create, "
        "list, update, or complete a task, handle it directly. "
        "Keep responses concise."
    ),
    tools=_task_tools,
    generate_content_config=_generation_config,
)

# ---------------------------------------------------------------------------
# Root agent: productivity coordinator
# ---------------------------------------------------------------------------
root_agent = Agent(
    name="productivity_assistant",
    model="gemini-3-flash-preview",
    description="Main productivity assistant that coordinates all tasks.",
    instruction=(
        "You are a productivity assistant. Help users manage tasks, "
        "find information, and streamline daily workflows. "
        "Keep all responses concise and under 1500 characters.\n\n"
        "Use Google Search to find current information when the user "
        "asks about recent events, news, documentation, or anything "
        "that benefits from up-to-date web results.\n\n"
        "If Google Search fails or is unavailable, let the user know "
        "that live search is temporarily unavailable and answer from "
        "your existing knowledge if possible.\n\n"
        "For task management requests (creating, listing, updating, "
        "completing tasks), you can handle them directly using the "
        "task tools or delegate to the task_manager agent."
    ),
    sub_agents=[task_manager_agent],
    tools=[google_search] + _task_tools,
    generate_content_config=_generation_config,
)
