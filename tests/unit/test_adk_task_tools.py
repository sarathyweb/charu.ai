"""ADK task tool wrapper tests."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from google.adk.tools import FunctionTool
from sqlmodel.ext.asyncio.session import AsyncSession

from app.agents.productivity_agent import tools as task_tools
from app.agents.productivity_agent.agent import _task_tools
from app.models.user import User
from app.services.task_service import TaskService


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
    monkeypatch.setattr(
        task_tools,
        "async_session_factory",
        lambda: _SessionContext(session),
    )


def test_adk_task_tool_registration_includes_full_parity_set():
    names = {_tool_name(tool) for tool in _task_tools}

    assert names == {
        "save_task",
        "complete_task_by_title",
        "list_pending_tasks",
        "update_task",
        "delete_task",
        "snooze_task",
        "unsnooze_task",
    }


def _tool_name(tool) -> str:
    return getattr(tool, "__name__", None) or tool.name


def _declaration(tool):
    if hasattr(tool, "_get_declaration"):
        return tool._get_declaration()
    return FunctionTool(tool)._get_declaration()


def test_adk_task_tool_schemas_expose_required_fields():
    declarations = {_tool_name(tool): _declaration(tool) for tool in _task_tools}

    assert declarations["update_task"].parameters.required == ["title"]
    assert declarations["delete_task"].parameters.required == ["title"]
    assert declarations["snooze_task"].parameters.required == ["title", "snooze_until"]
    assert declarations["unsnooze_task"].parameters.required == ["title"]
    assert declarations["list_pending_tasks"].parameters.required == []


def test_adk_delete_task_requires_confirmation():
    delete_tool = next(
        tool for tool in _task_tools if _tool_name(tool) == "delete_task"
    )

    assert delete_tool._require_confirmation is True


@pytest.mark.asyncio
async def test_adk_task_tools_return_success_payloads(session, tool_context):
    user = await _create_user(session)

    saved = await task_tools.save_task(
        title="File my taxes",
        priority=50,
        source="user_mention",
        tool_context=tool_context,
    )
    assert saved["success"] is True
    assert saved["status"] == "created"
    assert saved["title"] == "File my taxes"

    listed = await task_tools.list_pending_tasks(tool_context=tool_context)
    assert listed["success"] is True
    assert listed["count"] == 1
    assert listed["tasks"][0]["title"] == "File my taxes"

    updated = await task_tools.update_task(
        title="file taxes",
        new_title="File quarterly taxes",
        new_priority=90,
        tool_context=tool_context,
    )
    assert updated["success"] is True
    assert updated["status"] == "updated"
    assert updated["title"] == "File quarterly taxes"
    assert updated["priority"] == 90

    snooze_until = datetime.now(timezone.utc) + timedelta(days=1)
    snoozed = await task_tools.snooze_task(
        title="quarterly taxes",
        snooze_until=snooze_until.isoformat(),
        tool_context=tool_context,
    )
    assert snoozed["success"] is True
    assert snoozed["status"] == "snoozed"
    assert snoozed["snoozed_until"] == snooze_until.isoformat()

    unsnoozed = await task_tools.unsnooze_task(
        title="quarterly taxes",
        tool_context=tool_context,
    )
    assert unsnoozed["success"] is True
    assert unsnoozed["status"] == "unsnoozed"
    assert unsnoozed["snoozed_until"] is None

    deleted = await task_tools.delete_task(
        title="quarterly taxes",
        tool_context=tool_context,
    )
    assert deleted["success"] is True
    assert deleted["status"] == "deleted"

    svc = TaskService(session)
    assert await svc.list_pending_tasks(user.id) == []


@pytest.mark.asyncio
async def test_adk_complete_task_is_idempotent(session, tool_context):
    await _create_user(session)
    await task_tools.save_task(
        title="File my taxes",
        priority=50,
        source="user_mention",
        tool_context=tool_context,
    )

    first = await task_tools.complete_task_by_title(
        title="file taxes",
        tool_context=tool_context,
    )
    second = await task_tools.complete_task_by_title(
        title="file taxes",
        tool_context=tool_context,
    )

    assert first["success"] is True
    assert second["success"] is True
    assert second["task_id"] == first["task_id"]


@pytest.mark.asyncio
async def test_adk_task_tools_return_structured_errors(session, tool_context):
    await _create_user(session)

    no_fields = await task_tools.update_task(
        title="file taxes",
        tool_context=tool_context,
    )
    assert no_fields == {
        "success": False,
        "error": "At least one of new_title or new_priority must be provided.",
    }

    bad_snooze = await task_tools.snooze_task(
        title="file taxes",
        snooze_until="2026-01-01T09:00:00",
        tool_context=tool_context,
    )
    assert bad_snooze["success"] is False
    assert "timezone offset" in bad_snooze["error"]

    bad_limit = await task_tools.list_pending_tasks(
        tool_context=tool_context,
        limit=0,
    )
    assert bad_limit == {
        "success": False,
        "error": "limit must be between 1 and 50.",
    }


@pytest.mark.asyncio
async def test_adk_task_tools_require_phone(tool_context):
    tool_context.state = {}

    result = await task_tools.list_pending_tasks(tool_context=tool_context)

    assert result == {
        "success": False,
        "error": "No phone number in session state.",
    }
