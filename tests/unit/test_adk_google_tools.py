"""ADK Google Calendar/Gmail tool registration and wrapper tests."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.adk.tools import FunctionTool
from sqlmodel.ext.asyncio.session import AsyncSession

from app.agents.productivity_agent import google_tools
from app.agents.productivity_agent.agent import _google_tools
from app.models.user import User


class _SessionContext:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def __aenter__(self) -> AsyncSession:
        return self.session

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


def _tool_name(tool) -> str:
    return getattr(tool, "__name__", None) or tool.name


def _declaration(tool):
    if hasattr(tool, "_get_declaration"):
        return tool._get_declaration()
    return FunctionTool(tool)._get_declaration()


def _required_fields(tool) -> list[str]:
    declaration = _declaration(tool)
    if declaration.parameters is None:
        return []
    return list(declaration.parameters.required or [])


@pytest.fixture
def tool_context():
    return SimpleNamespace(state={"phone": "+15551234567"})


@pytest.fixture(autouse=True)
def patch_tool_sessions(monkeypatch, session: AsyncSession):
    def factory():
        return _SessionContext(session)

    monkeypatch.setattr(google_tools, "async_session_factory", factory)


async def _create_user(session: AsyncSession, *, scopes: str | None = None) -> User:
    user = User(
        phone="+15551234567",
        timezone="America/New_York",
        onboarding_complete=True,
        google_access_token_encrypted="access",
        google_refresh_token_encrypted="refresh",
        google_granted_scopes=(
            scopes
            or "https://www.googleapis.com/auth/calendar "
            "https://www.googleapis.com/auth/gmail.modify"
        ),
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


def test_adk_google_tool_registration_includes_full_calendar_gmail_set():
    names = {_tool_name(tool) for tool in _google_tools}

    assert names == {
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


def test_adk_google_tool_schemas_expose_expected_required_fields():
    required = {_tool_name(tool): _required_fields(tool) for tool in _google_tools}

    assert required["get_todays_calendar"] == []
    assert set(required["get_events_for_date_range"]) == {
        "start_date",
        "end_date",
    }
    assert set(required["suggest_calendar_time_block"]) == {
        "task_title",
        "duration_minutes",
    }
    assert set(required["create_calendar_time_block"]) == {
        "task_title",
        "start_iso",
        "end_iso",
    }
    assert set(required["create_calendar_event"]) == {
        "summary",
        "start_iso",
        "end_iso",
    }
    assert required["update_calendar_event"] == ["event_id"]
    assert required["delete_calendar_event"] == ["event_id"]
    assert required["search_emails"] == ["query"]
    assert required["read_email"] == ["query"]
    assert set(required["compose_email"]) == {
        "to_address",
        "subject",
        "body_text",
    }
    assert required["archive_email"] == ["message_id"]


def test_adk_risky_google_tools_require_confirmation():
    tools = {_tool_name(tool): tool for tool in _google_tools}

    assert tools["delete_calendar_event"]._require_confirmation is True
    assert tools["compose_email"]._require_confirmation is True
    assert tools["archive_email"]._require_confirmation is True


@pytest.mark.asyncio
async def test_adk_google_tools_delegate_to_expanded_services(
    monkeypatch,
    session,
    tool_context,
):
    await _create_user(session)

    fetch_range = AsyncMock(
        return_value=[
            {
                "id": "event_1",
                "summary": "Planning",
                "start": {"dateTime": "2026-05-01T09:00:00-04:00"},
                "end": {"dateTime": "2026-05-01T09:30:00-04:00"},
            }
        ]
    )
    create_event = AsyncMock(return_value={"status": "created", "event_id": "event_1"})
    send_new_email = AsyncMock(
        return_value={
            "status": "sent",
            "gmail_message_id": "sent_1",
            "thread_id": "thread_1",
        }
    )
    search_emails = AsyncMock(
        return_value=[
            {
                "id": "msg_1",
                "thread_id": "thread_msg_1",
                "subject": "Launch",
                "from": "Asha <asha@example.com>",
                "date": "",
                "snippet": "Snippet",
            }
        ]
    )

    monkeypatch.setattr(
        "app.services.google_calendar_read_service.fetch_events_for_range",
        fetch_range,
    )
    monkeypatch.setattr(
        "app.services.google_calendar_write_service.create_event",
        create_event,
    )
    monkeypatch.setattr(
        "app.services.gmail_write_service.send_new_email",
        send_new_email,
    )
    monkeypatch.setattr(
        "app.services.gmail_read_service.search_emails",
        search_emails,
    )

    range_result = await google_tools.get_events_for_date_range(
        start_date="2026-05-01",
        end_date="2026-05-02",
        tool_context=tool_context,
    )
    created = await google_tools.create_calendar_event(
        summary="Planning",
        start_iso="2026-05-01T09:00:00-04:00",
        end_iso="2026-05-01T09:30:00-04:00",
        tool_context=tool_context,
    )
    sent = await google_tools.compose_email(
        to_address="asha@example.com",
        subject="Launch",
        body_text="Let's ship.",
        tool_context=tool_context,
    )
    searched = await google_tools.search_emails(
        query="from:asha launch",
        tool_context=tool_context,
    )

    assert range_result["count"] == 1
    assert created["event_id"] == "event_1"
    assert sent["gmail_message_id"] == "sent_1"
    assert searched["count"] == 1
    fetch_range.assert_awaited_once()
    assert fetch_range.await_args.kwargs == {
        "start_date": date(2026, 5, 1),
        "end_date": date(2026, 5, 2),
    }


@pytest.mark.asyncio
async def test_adk_google_tools_return_structured_validation_errors(
    session,
    tool_context,
):
    await _create_user(session)

    bad_date = await google_tools.get_events_for_date_range(
        start_date="May 1",
        end_date="2026-05-02",
        tool_context=tool_context,
    )
    assert bad_date == {"error": "start_date must be in YYYY-MM-DD format."}

    no_fields = await google_tools.update_calendar_event(
        event_id="event_1",
        tool_context=tool_context,
    )
    assert no_fields == {
        "error": (
            "At least one of summary, start_iso, end_iso, or description "
            "must be provided."
        )
    }


@pytest.mark.asyncio
async def test_adk_google_tools_require_connected_scope(session, tool_context):
    await _create_user(session, scopes="https://www.googleapis.com/auth/calendar")

    result = await google_tools.search_emails(
        query="from:asha",
        tool_context=tool_context,
    )

    assert result == {"error": "Gmail is not connected. Please connect it first."}
