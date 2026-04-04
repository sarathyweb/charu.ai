"""ADK agent configuration — root_agent with sub-agent and Google Search.

Gemini 3 Flash natively supports combining built-in tools (google_search)
with custom tools / sub-agents, so no AgentTool wrapper is needed.
See: https://ai.google.dev/gemini-api/docs/grounding
"""

from google.adk.agents import Agent
from google.adk.tools import google_search

# ---------------------------------------------------------------------------
# Sub-agent: task management specialist
# ---------------------------------------------------------------------------
task_manager_agent = Agent(
    name="task_manager",
    model="gemini-3-flash-preview",
    description=(
        "Handles task management: creating, listing, updating, "
        "and completing tasks."
    ),
    instruction=(
        "You are a task management specialist. Help users create, "
        "organize, and track their tasks. When the user asks to create, "
        "list, update, or complete a task, handle it directly."
    ),
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
        "find information, and streamline daily workflows.\n\n"
        "Use Google Search to find current information when the user "
        "asks about recent events, news, documentation, or anything "
        "that benefits from up-to-date web results.\n\n"
        "If Google Search fails or is unavailable, let the user know "
        "that live search is temporarily unavailable and answer from "
        "your existing knowledge if possible.\n\n"
        "Delegate task management requests (creating, listing, updating, "
        "completing tasks) to the task_manager agent."
    ),
    sub_agents=[task_manager_agent],
    tools=[google_search],
)
