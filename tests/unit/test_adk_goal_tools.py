"""ADK goal tool wrapper tests."""

from types import SimpleNamespace

import pytest
from google.adk.tools import FunctionTool
from sqlmodel.ext.asyncio.session import AsyncSession

from app.agents.productivity_agent import goal_tools
from app.agents.productivity_agent import tools as task_tools
from app.agents.productivity_agent.agent import _goal_tools
from app.models.goal import Goal
from app.models.user import User


class _SessionContext:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def __aenter__(self) -> AsyncSession:
        return self.session

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


async def _create_user(session: AsyncSession, phone: str = "+15551234567") -> User:
    user = User(
        phone=phone,
        timezone="America/New_York",
        onboarding_complete=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@pytest.fixture
def tool_context():
    return SimpleNamespace(state={"phone": "+15551234567"})


@pytest.fixture(autouse=True)
def patch_tool_sessions(monkeypatch, session: AsyncSession):
    def factory():
        return _SessionContext(session)

    monkeypatch.setattr(goal_tools, "async_session_factory", factory)
    monkeypatch.setattr(task_tools, "async_session_factory", factory)


def _tool_name(tool) -> str:
    return getattr(tool, "__name__", None) or tool.name


def _declaration(tool):
    if hasattr(tool, "_get_declaration"):
        return tool._get_declaration()
    return FunctionTool(tool)._get_declaration()


def test_adk_goal_tool_registration_includes_crud_set():
    names = {_tool_name(tool) for tool in _goal_tools}

    assert names == {
        "create_goal",
        "list_goals",
        "update_goal",
        "complete_goal",
        "abandon_goal",
        "delete_goal",
    }


def test_adk_goal_tool_schemas_expose_required_fields():
    declarations = {_tool_name(tool): _declaration(tool) for tool in _goal_tools}

    assert declarations["create_goal"].parameters.required == ["title"]
    assert declarations["list_goals"].parameters.required == []
    assert declarations["update_goal"].parameters.required == ["goal_id"]
    assert declarations["complete_goal"].parameters.required == ["goal_id"]
    assert declarations["abandon_goal"].parameters.required == ["goal_id"]
    assert declarations["delete_goal"].parameters.required == ["goal_id"]


def test_adk_delete_goal_requires_confirmation():
    delete_tool = next(
        tool for tool in _goal_tools if _tool_name(tool) == "delete_goal"
    )

    assert delete_tool._require_confirmation is True


@pytest.mark.asyncio
async def test_adk_goal_tools_return_success_payloads(session, tool_context):
    await _create_user(session)

    created = await goal_tools.create_goal(
        title="Finish tax filing",
        description="Collect forms and submit",
        target_date="2026-05-15",
        tool_context=tool_context,
    )
    assert created["success"] is True
    assert created["status"] == "created"
    assert created["title"] == "Finish tax filing"
    assert created["target_date"] == "2026-05-15"

    listed = await goal_tools.list_goals(tool_context=tool_context, status="active")
    assert listed["success"] is True
    assert listed["count"] == 1
    assert listed["goals"][0]["title"] == "Finish tax filing"

    updated = await goal_tools.update_goal(
        goal_id=created["goal_id"],
        new_title="Finish quarterly tax filing",
        new_target_date="2026-05-20",
        tool_context=tool_context,
    )
    assert updated["success"] is True
    assert updated["status"] == "updated"
    assert updated["title"] == "Finish quarterly tax filing"
    assert updated["target_date"] == "2026-05-20"

    completed = await goal_tools.complete_goal(
        goal_id=created["goal_id"],
        tool_context=tool_context,
    )
    assert completed["success"] is True
    assert completed["status"] == "completed"
    assert completed["goal_status"] == "completed"
    assert completed["completed_at"] is not None

    abandoned = await goal_tools.abandon_goal(
        goal_id=created["goal_id"],
        tool_context=tool_context,
    )
    assert abandoned["success"] is True
    assert abandoned["status"] == "abandoned"
    assert abandoned["goal_status"] == "abandoned"
    assert abandoned["completed_at"] is None

    deleted = await goal_tools.delete_goal(
        goal_id=created["goal_id"],
        tool_context=tool_context,
    )
    assert deleted["success"] is True
    assert deleted["status"] == "deleted"

    assert await session.get(Goal, created["goal_id"]) is None


@pytest.mark.asyncio
async def test_adk_goal_tools_return_structured_errors(session, tool_context):
    await _create_user(session)

    no_fields = await goal_tools.update_goal(
        goal_id=999,
        tool_context=tool_context,
    )
    assert no_fields == {
        "success": False,
        "error": (
            "At least one of new_title, new_description, or new_target_date "
            "must be provided."
        ),
    }

    bad_date = await goal_tools.create_goal(
        title="Finish tax filing",
        target_date="May 15",
        tool_context=tool_context,
    )
    assert bad_date == {
        "success": False,
        "error": "target_date must be in YYYY-MM-DD format.",
    }

    bad_status = await goal_tools.list_goals(
        status="stuck",
        tool_context=tool_context,
    )
    assert bad_status == {
        "success": False,
        "error": "status must be one of: active, completed, abandoned.",
    }


@pytest.mark.asyncio
async def test_adk_goal_tools_require_phone(tool_context):
    tool_context.state = {}

    result = await goal_tools.list_goals(tool_context=tool_context)

    assert result == {
        "success": False,
        "error": "No phone number in session state.",
    }
