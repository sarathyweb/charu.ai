"""Voice task tool registration and callback tests."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pipecat.adapters.schemas.tools_schema import AdapterType
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.goal import Goal
from app.models.user import User
from app.services.call_window_service import CallWindowService
from app.services.task_service import TaskService
from app.voice import tools as voice_tools


class _FakeLLM:
    def __init__(self) -> None:
        self.functions = {}
        self.registration_options = {}

    def register_direct_function(self, fn, **kwargs):
        self.functions[fn.__name__] = fn
        self.registration_options[fn.__name__] = kwargs


class _SessionContext:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def __aenter__(self) -> AsyncSession:
        return self.session

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


async def _create_user(session: AsyncSession) -> User:
    user = User(
        phone="+15551234567",
        timezone="America/New_York",
        onboarding_complete=True,
        google_access_token_encrypted="access",
        google_refresh_token_encrypted="refresh",
        google_granted_scopes=(
            "https://www.googleapis.com/auth/calendar "
            "https://www.googleapis.com/auth/gmail.modify"
        ),
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@pytest.fixture(autouse=True)
def patch_voice_sessions(monkeypatch, session: AsyncSession):
    monkeypatch.setattr(
        voice_tools,
        "async_session_factory",
        lambda: _SessionContext(session),
    )


def _register(user_id: int = 42) -> _FakeLLM:
    llm = _FakeLLM()
    voice_tools.register_voice_tools(llm, call_log_id=7, user_id=user_id)
    return llm


def _params() -> SimpleNamespace:
    return SimpleNamespace(result_callback=AsyncMock())


def test_voice_tool_registration_includes_task_parity_tools():
    llm = _register()

    assert set(llm.functions) == {
        "save_call_outcome",
        "save_evening_call_outcome",
        "save_task",
        "complete_task_by_title",
        "list_pending_tasks",
        "update_task",
        "delete_task",
        "snooze_task",
        "unsnooze_task",
        "create_goal",
        "list_goals",
        "update_goal",
        "complete_goal",
        "abandon_goal",
        "delete_goal",
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
        "schedule_callback",
        "skip_call",
        "reschedule_call",
        "get_next_call",
        "cancel_all_calls_today",
        "add_call_window",
        "update_call_window",
        "remove_call_window",
        "list_call_windows",
    }
    assert len(llm.functions) == 40


def test_voice_google_search_is_registered_as_gemini_custom_tool():
    llm = _FakeLLM()

    tools = voice_tools.register_voice_tools(llm, call_log_id=7, user_id=42)

    assert tools.custom_tools == {
        AdapterType.GEMINI: [{"google_search": {}}],
    }
    assert "google_search" not in llm.functions


def test_voice_mutating_tools_are_not_cancelled_on_interruption():
    llm = _register()

    non_cancellable = {
        "save_call_outcome",
        "save_evening_call_outcome",
        "save_task",
        "complete_task_by_title",
        "update_task",
        "delete_task",
        "snooze_task",
        "unsnooze_task",
        "create_goal",
        "update_goal",
        "complete_goal",
        "abandon_goal",
        "delete_goal",
        "create_calendar_time_block",
        "create_calendar_event",
        "update_calendar_event",
        "delete_calendar_event",
        "save_email_draft",
        "update_email_draft",
        "send_approved_reply",
        "compose_email",
        "archive_email",
        "schedule_callback",
        "skip_call",
        "reschedule_call",
        "cancel_all_calls_today",
        "add_call_window",
        "update_call_window",
        "remove_call_window",
    }

    for name in non_cancellable:
        assert llm.registration_options[name]["cancel_on_interruption"] is False
    assert (
        llm.registration_options["list_pending_tasks"]["cancel_on_interruption"] is True
    )
    assert llm.registration_options["list_goals"]["cancel_on_interruption"] is True
    assert (
        llm.registration_options["get_events_for_date_range"][
            "cancel_on_interruption"
        ]
        is True
    )
    assert llm.registration_options["search_emails"]["cancel_on_interruption"] is True
    assert llm.registration_options["get_next_call"]["cancel_on_interruption"] is True
    assert llm.registration_options["list_call_windows"]["cancel_on_interruption"] is True


@pytest.mark.asyncio
async def test_voice_task_tools_return_callback_payloads(session):
    user = await _create_user(session)
    llm = _register(user_id=user.id)

    save_params = _params()
    await llm.functions["save_task"](save_params, title="File my taxes", priority=50)
    save_payload = save_params.result_callback.await_args.args[0]
    assert save_payload["success"] is True
    assert save_payload["status"] == "created"

    list_params = _params()
    await llm.functions["list_pending_tasks"](list_params, limit=5)
    list_payload = list_params.result_callback.await_args.args[0]
    assert list_payload["success"] is True
    assert list_payload["count"] == 1
    assert list_payload["tasks"][0]["title"] == "File my taxes"

    update_params = _params()
    await llm.functions["update_task"](
        update_params,
        title="file taxes",
        new_title="File quarterly taxes",
        new_priority=90,
    )
    update_payload = update_params.result_callback.await_args.args[0]
    assert update_payload["success"] is True
    assert update_payload["status"] == "updated"
    assert update_payload["title"] == "File quarterly taxes"
    assert update_payload["priority"] == 90

    snooze_until = datetime.now(timezone.utc) + timedelta(days=1)
    snooze_params = _params()
    await llm.functions["snooze_task"](
        snooze_params,
        title="quarterly taxes",
        snooze_until=snooze_until.isoformat(),
    )
    snooze_payload = snooze_params.result_callback.await_args.args[0]
    assert snooze_payload["success"] is True
    assert snooze_payload["status"] == "snoozed"
    assert snooze_payload["snoozed_until"] == snooze_until.isoformat()

    unsnooze_params = _params()
    await llm.functions["unsnooze_task"](unsnooze_params, title="quarterly taxes")
    unsnooze_payload = unsnooze_params.result_callback.await_args.args[0]
    assert unsnooze_payload["success"] is True
    assert unsnooze_payload["status"] == "unsnoozed"
    assert unsnooze_payload["snoozed_until"] is None

    delete_params = _params()
    await llm.functions["delete_task"](delete_params, title="quarterly taxes")
    delete_payload = delete_params.result_callback.await_args.args[0]
    assert delete_payload["success"] is True
    assert delete_payload["status"] == "deleted"

    svc = TaskService(session)
    assert await svc.list_pending_tasks(user.id) == []


@pytest.mark.asyncio
async def test_voice_goal_tools_return_callback_payloads(session):
    user = await _create_user(session)
    llm = _register(user_id=user.id)

    create_params = _params()
    await llm.functions["create_goal"](
        create_params,
        title="Finish tax filing",
        description="Collect forms and submit",
        target_date="2026-05-15",
    )
    create_payload = create_params.result_callback.await_args.args[0]
    assert create_payload["success"] is True
    assert create_payload["status"] == "created"
    assert create_payload["title"] == "Finish tax filing"
    assert create_payload["target_date"] == "2026-05-15"

    list_params = _params()
    await llm.functions["list_goals"](list_params, status="active")
    list_payload = list_params.result_callback.await_args.args[0]
    assert list_payload["success"] is True
    assert list_payload["count"] == 1
    assert list_payload["goals"][0]["title"] == "Finish tax filing"

    update_params = _params()
    await llm.functions["update_goal"](
        update_params,
        goal_id=create_payload["goal_id"],
        new_title="Finish quarterly tax filing",
        new_target_date="2026-05-20",
    )
    update_payload = update_params.result_callback.await_args.args[0]
    assert update_payload["success"] is True
    assert update_payload["status"] == "updated"
    assert update_payload["title"] == "Finish quarterly tax filing"
    assert update_payload["target_date"] == "2026-05-20"

    complete_params = _params()
    await llm.functions["complete_goal"](
        complete_params,
        goal_id=create_payload["goal_id"],
    )
    complete_payload = complete_params.result_callback.await_args.args[0]
    assert complete_payload["success"] is True
    assert complete_payload["status"] == "completed"
    assert complete_payload["goal_status"] == "completed"
    assert complete_payload["completed_at"] is not None

    abandon_params = _params()
    await llm.functions["abandon_goal"](
        abandon_params,
        goal_id=create_payload["goal_id"],
    )
    abandon_payload = abandon_params.result_callback.await_args.args[0]
    assert abandon_payload["success"] is True
    assert abandon_payload["status"] == "abandoned"
    assert abandon_payload["goal_status"] == "abandoned"
    assert abandon_payload["completed_at"] is None

    delete_params = _params()
    await llm.functions["delete_goal"](
        delete_params,
        goal_id=create_payload["goal_id"],
    )
    delete_payload = delete_params.result_callback.await_args.args[0]
    assert delete_payload["success"] is True
    assert delete_payload["status"] == "deleted"

    assert await session.get(Goal, create_payload["goal_id"]) is None


@pytest.mark.asyncio
async def test_voice_calendar_and_gmail_tools_return_callback_payloads(
    monkeypatch,
    session,
):
    user = await _create_user(session)
    llm = _register(user_id=user.id)

    monkeypatch.setattr(
        "app.services.google_calendar_read_service.fetch_events_for_range",
        AsyncMock(
            return_value=[
                {
                    "id": "event_1",
                    "summary": "Planning",
                    "start": {"dateTime": "2026-05-01T09:00:00-04:00"},
                    "end": {"dateTime": "2026-05-01T09:30:00-04:00"},
                }
            ]
        ),
    )
    monkeypatch.setattr(
        "app.services.google_calendar_write_service.create_event",
        AsyncMock(return_value={"status": "created", "event_id": "event_1"}),
    )
    monkeypatch.setattr(
        "app.services.gmail_read_service.search_emails",
        AsyncMock(
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
        ),
    )
    monkeypatch.setattr(
        "app.services.gmail_write_service.send_new_email",
        AsyncMock(
            return_value={
                "status": "sent",
                "gmail_message_id": "sent_1",
                "thread_id": "thread_1",
            }
        ),
    )
    monkeypatch.setattr(
        "app.services.gmail_write_service.archive_email",
        AsyncMock(return_value={"status": "archived", "message_id": "msg_1"}),
    )

    range_params = _params()
    await llm.functions["get_events_for_date_range"](
        range_params,
        start_date="2026-05-01",
        end_date="2026-05-02",
    )
    range_payload = range_params.result_callback.await_args.args[0]
    assert range_payload["success"] is True
    assert range_payload["count"] == 1
    assert range_payload["events"][0]["summary"] == "Planning"

    create_event_params = _params()
    await llm.functions["create_calendar_event"](
        create_event_params,
        summary="Planning",
        start_iso="2026-05-01T09:00:00-04:00",
        end_iso="2026-05-01T09:30:00-04:00",
    )
    create_event_payload = create_event_params.result_callback.await_args.args[0]
    assert create_event_payload == {
        "success": True,
        "status": "created",
        "event_id": "event_1",
    }

    search_params = _params()
    await llm.functions["search_emails"](
        search_params,
        query="from:asha launch",
    )
    search_payload = search_params.result_callback.await_args.args[0]
    assert search_payload["success"] is True
    assert search_payload["count"] == 1
    assert search_payload["emails"][0]["id"] == "msg_1"

    compose_params = _params()
    await llm.functions["compose_email"](
        compose_params,
        to_address="asha@example.com",
        subject="Launch",
        body_text="Let's ship.",
    )
    compose_payload = compose_params.result_callback.await_args.args[0]
    assert compose_payload["success"] is True
    assert compose_payload["gmail_message_id"] == "sent_1"

    archive_params = _params()
    await llm.functions["archive_email"](archive_params, message_id="msg_1")
    archive_payload = archive_params.result_callback.await_args.args[0]
    assert archive_payload == {
        "success": True,
        "status": "archived",
        "message_id": "msg_1",
    }


@pytest.mark.asyncio
async def test_voice_call_window_tools_return_callback_payloads(session):
    user = await _create_user(session)
    llm = _register(user_id=user.id)

    add_params = _params()
    await llm.functions["add_call_window"](
        add_params,
        window_type="morning",
        start_time="08:00",
        end_time="08:30",
    )
    add_payload = add_params.result_callback.await_args.args[0]
    assert add_payload == {
        "success": True,
        "status": "added",
        "window_type": "morning",
        "start": "08:00",
        "end": "08:30",
    }

    list_params = _params()
    await llm.functions["list_call_windows"](list_params)
    list_payload = list_params.result_callback.await_args.args[0]
    assert list_payload["success"] is True
    assert list_payload["count"] == 1
    assert list_payload["windows"][0] == {
        "window_type": "morning",
        "start": "08:00",
        "end": "08:30",
        "is_active": True,
    }

    update_params = _params()
    await llm.functions["update_call_window"](
        update_params,
        window_type="morning",
        start_time="08:15",
        end_time="08:45",
    )
    update_payload = update_params.result_callback.await_args.args[0]
    assert update_payload == {
        "success": True,
        "status": "updated",
        "window_type": "morning",
        "start": "08:15",
        "end": "08:45",
    }

    remove_params = _params()
    await llm.functions["remove_call_window"](remove_params, window_type="morning")
    remove_payload = remove_params.result_callback.await_args.args[0]
    assert remove_payload == {
        "success": True,
        "status": "removed",
        "window_type": "morning",
        "start": "08:15",
        "end": "08:45",
    }

    svc = CallWindowService(session)
    assert await svc.list_windows_for_user(user.id) == []


@pytest.mark.asyncio
async def test_voice_call_window_tools_return_validation_errors(session):
    user = await _create_user(session)
    llm = _register(user_id=user.id)

    bad_type_params = _params()
    await llm.functions["add_call_window"](
        bad_type_params,
        window_type="lunch",
        start_time="12:00",
        end_time="12:30",
    )
    bad_type_payload = bad_type_params.result_callback.await_args.args[0]
    assert bad_type_payload["success"] is False
    assert "window_type must be one of" in bad_type_payload["error"]

    short_window_params = _params()
    await llm.functions["add_call_window"](
        short_window_params,
        window_type="morning",
        start_time="08:00",
        end_time="08:10",
    )
    short_window_payload = short_window_params.result_callback.await_args.args[0]
    assert short_window_payload == {
        "success": False,
        "error": "Call window must be at least 20 minutes wide.",
    }

    await llm.functions["add_call_window"](
        _params(),
        window_type="morning",
        start_time="08:00",
        end_time="08:30",
    )

    overlap_params = _params()
    await llm.functions["add_call_window"](
        overlap_params,
        window_type="afternoon",
        start_time="08:15",
        end_time="08:45",
    )
    overlap_payload = overlap_params.result_callback.await_args.args[0]
    assert overlap_payload["success"] is False
    assert "overlaps with your morning window" in overlap_payload["error"]

    no_fields_params = _params()
    await llm.functions["update_call_window"](
        no_fields_params,
        window_type="morning",
    )
    no_fields_payload = no_fields_params.result_callback.await_args.args[0]
    assert no_fields_payload == {
        "success": False,
        "error": "Provide at least one of start_time or end_time.",
    }

    missing_remove_params = _params()
    await llm.functions["remove_call_window"](
        missing_remove_params,
        window_type="evening",
    )
    missing_remove_payload = missing_remove_params.result_callback.await_args.args[0]
    assert missing_remove_payload == {
        "success": True,
        "status": "already_removed",
        "window_type": "evening",
    }


@pytest.mark.asyncio
async def test_voice_task_tools_return_errors(session):
    user = await _create_user(session)
    llm = _register(user_id=user.id)

    no_fields_params = _params()
    await llm.functions["update_task"](no_fields_params, title="file taxes")
    no_fields_payload = no_fields_params.result_callback.await_args.args[0]
    assert no_fields_payload == {
        "success": False,
        "error": "At least one of new_title or new_priority must be provided.",
    }

    blank_title_params = _params()
    await llm.functions["save_task"](
        _params(),
        title="Schedule dentist appointment",
        priority=40,
    )
    await llm.functions["update_task"](
        blank_title_params,
        title="dentist appointment",
        new_title="",
        new_priority=75,
    )
    blank_title_payload = blank_title_params.result_callback.await_args.args[0]
    assert blank_title_payload["success"] is True
    assert blank_title_payload["title"] == "Schedule dentist appointment"
    assert blank_title_payload["priority"] == 75

    sentinel_priority_params = _params()
    await llm.functions["update_task"](
        sentinel_priority_params,
        title="dentist appointment",
        new_title="Schedule annual dentist appointment",
        new_priority=-1,
    )
    sentinel_priority_payload = (
        sentinel_priority_params.result_callback.await_args.args[0]
    )
    assert sentinel_priority_payload["success"] is True
    assert sentinel_priority_payload["title"] == "Schedule annual dentist appointment"
    assert sentinel_priority_payload["priority"] == 75

    bad_snooze_params = _params()
    await llm.functions["snooze_task"](
        bad_snooze_params,
        title="file taxes",
        snooze_until="2026-01-01T09:00:00",
    )
    bad_snooze_payload = bad_snooze_params.result_callback.await_args.args[0]
    assert bad_snooze_payload["success"] is False
    assert "snooze_until must include a timezone offset" in bad_snooze_payload["error"]

    bad_limit_params = _params()
    await llm.functions["list_pending_tasks"](bad_limit_params, limit=0)
    bad_limit_payload = bad_limit_params.result_callback.await_args.args[0]
    assert bad_limit_payload == {
        "success": False,
        "error": "limit must be between 1 and 50.",
    }


@pytest.mark.asyncio
async def test_voice_goal_tools_return_errors(session):
    user = await _create_user(session)
    llm = _register(user_id=user.id)

    no_fields_params = _params()
    await llm.functions["update_goal"](no_fields_params, goal_id=999)
    no_fields_payload = no_fields_params.result_callback.await_args.args[0]
    assert no_fields_payload == {
        "success": False,
        "error": (
            "At least one of new_title, new_description, or new_target_date "
            "must be provided."
        ),
    }

    bad_date_params = _params()
    await llm.functions["create_goal"](
        bad_date_params,
        title="Finish tax filing",
        target_date="May 15",
    )
    bad_date_payload = bad_date_params.result_callback.await_args.args[0]
    assert bad_date_payload == {
        "success": False,
        "error": "target_date must be in YYYY-MM-DD format.",
    }

    bad_status_params = _params()
    await llm.functions["list_goals"](bad_status_params, status="stuck")
    bad_status_payload = bad_status_params.result_callback.await_args.args[0]
    assert bad_status_payload == {
        "success": False,
        "error": "status must be one of: active, completed, abandoned.",
    }


@pytest.mark.asyncio
async def test_voice_list_pending_tasks_uses_default_limit(session):
    user = await _create_user(session)
    svc = TaskService(session)
    for index in range(6):
        await svc.save_task(user.id, f"Task {index}", priority=index)

    llm = _register(user_id=user.id)
    params = _params()
    await llm.functions["list_pending_tasks"](params)

    payload = params.result_callback.await_args.args[0]
    assert payload["success"] is True
    assert payload["count"] == 5
