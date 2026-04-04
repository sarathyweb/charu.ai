"""ADK master agent + sub-agent + Google Search tool configuration.

Uses Vertex AI backend (GOOGLE_GENAI_USE_VERTEXAI=TRUE) with
Application Default Credentials for authentication.
"""

from google.adk.agents import Agent
from google.adk.tools import google_search

# ---------------------------------------------------------------------------
# Sub-agent: task management specialist
# ---------------------------------------------------------------------------
sub_agent = Agent(
    name="task_manager",
    model="gemini-2.5-flash",
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
# Master agent: productivity coordinator
# ---------------------------------------------------------------------------
master_agent = Agent(
    name="productivity_assistant",
    model="gemini-2.5-flash",
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
    sub_agents=[sub_agent],
    tools=[google_search],
)
