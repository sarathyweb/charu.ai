"""Tests for Gmail automation services."""

from datetime import datetime, time, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select

from app.models.call_log import CallLog
from app.models.email_automation_event import EmailAutomationEvent
from app.models.enums import (
    CallLogStatus,
    EmailAutomationEventType,
    EmailAutomationStatus,
    TaskSource,
)
from app.models.task import Task
from app.models.user import User
from app.services.email_automation_service import (
    EmailAutomationService,
    build_task_title_from_email,
    score_task_email,
    score_urgent_email,
)


def _summary(
    message_id: str = "msg-1",
    thread_id: str = "thread-1",
    subject: str = "Action required: review launch plan",
) -> dict:
    return {
        "id": message_id,
        "thread_id": thread_id,
        "from": "Asha <asha@example.com>",
        "subject": subject,
        "snippet": "Can you please review this by EOD?",
    }


def _full_email(
    *,
    message_id: str = "msg-1",
    thread_id: str = "thread-1",
    subject: str = "Action required: review launch plan",
    body: str = "Can you please review this and respond by EOD?",
) -> dict:
    return {
        "id": message_id,
        "thread_id": thread_id,
        "message_id": f"<{message_id}@example.com>",
        "from": "Asha <asha@example.com>",
        "subject": subject,
        "body": body,
    }


async def _create_user(
    session,
    *,
    urgent: bool = False,
    auto_task: bool = False,
) -> User:
    user = User(
        phone="+14155552671",
        timezone="UTC",
        google_refresh_token_encrypted="refresh",
        google_granted_scopes="https://www.googleapis.com/auth/gmail.modify",
        urgent_email_calls_enabled=urgent,
        auto_task_from_emails_enabled=auto_task,
        email_automation_quiet_hours_start=time(21, 0),
        email_automation_quiet_hours_end=time(8, 0),
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


def test_email_scoring_and_task_title_are_deterministic():
    email = _full_email(
        subject="Urgent: action required for launch deadline",
        body="Can you please review this and respond today?",
    )

    assert score_urgent_email(email).score >= 0.65
    assert score_task_email(email).score >= 0.7
    assert build_task_title_from_email(email) == (
        "Reply to Asha: Urgent: action required for launch deadline"
    )


@pytest.mark.asyncio
async def test_auto_task_from_email_creates_task_and_thread_event(session):
    user = await _create_user(session, auto_task=True)
    svc = EmailAutomationService(session)

    with (
        patch(
            "app.services.email_automation_service.search_emails",
            new=AsyncMock(return_value=[_summary()]),
        ) as search_mock,
        patch(
            "app.services.email_automation_service.get_email_for_reply",
            new=AsyncMock(return_value=_full_email()),
        ) as read_mock,
    ):
        first = await svc.process_user(
            user,
            now=datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc),
        )
        second = await svc.process_user(
            user,
            now=datetime(2026, 4, 26, 12, 15, tzinfo=timezone.utc),
        )

    assert search_mock.await_count == 2
    assert read_mock.await_count == 2
    assert first.tasks_created == 1
    assert second.tasks_created == 0

    tasks = list((await session.exec(select(Task))).all())
    assert len(tasks) == 1
    assert tasks[0].source == TaskSource.GMAIL.value
    assert tasks[0].priority == 70
    assert tasks[0].title.startswith("Reply to Asha:")

    events = list((await session.exec(select(EmailAutomationEvent))).all())
    assert len(events) == 1
    assert events[0].event_type == EmailAutomationEventType.AUTO_TASK.value
    assert events[0].status == EmailAutomationStatus.CREATED.value
    assert events[0].task_id == tasks[0].id


@pytest.mark.asyncio
async def test_urgent_email_schedules_on_demand_call_with_dedupe(session):
    user = await _create_user(session, urgent=True)
    svc = EmailAutomationService(session)
    urgent_email = _full_email(
        subject="Urgent: production blocker deadline",
        body="This is time sensitive and blocked. Please respond asap.",
    )

    with (
        patch(
            "app.services.email_automation_service.search_emails",
            new=AsyncMock(return_value=[_summary(subject=urgent_email["subject"])]),
        ),
        patch(
            "app.services.email_automation_service.get_email_for_reply",
            new=AsyncMock(return_value=urgent_email),
        ),
    ):
        first = await svc.process_user(
            user,
            now=datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc),
        )
        second = await svc.process_user(
            user,
            now=datetime(2026, 4, 26, 12, 15, tzinfo=timezone.utc),
        )

    assert first.urgent_calls_scheduled == 1
    assert second.urgent_calls_scheduled == 0

    calls = list((await session.exec(select(CallLog))).all())
    assert len(calls) == 1
    assert calls[0].status == CallLogStatus.SCHEDULED.value
    assert calls[0].call_type == "on_demand"
    assert calls[0].goal.startswith("Handle urgent email from Asha")
    assert calls[0].next_action == (
        "Decide the response or next action for this urgent email."
    )

    events = list((await session.exec(select(EmailAutomationEvent))).all())
    assert len(events) == 1
    assert events[0].event_type == EmailAutomationEventType.URGENT_CALL.value
    assert events[0].call_log_id == calls[0].id


@pytest.mark.asyncio
async def test_urgent_email_respects_quiet_hours(session):
    user = await _create_user(session, urgent=True)
    svc = EmailAutomationService(session)
    urgent_email = _full_email(
        subject="Urgent: action required",
        body="Please respond asap. This is time sensitive.",
    )

    with (
        patch(
            "app.services.email_automation_service.search_emails",
            new=AsyncMock(return_value=[_summary(subject=urgent_email["subject"])]),
        ),
        patch(
            "app.services.email_automation_service.get_email_for_reply",
            new=AsyncMock(return_value=urgent_email),
        ),
    ):
        result = await svc.process_user(
            user,
            now=datetime(2026, 4, 26, 23, 0, tzinfo=timezone.utc),
        )

    assert result.urgent_calls_scheduled == 0
    assert list((await session.exec(select(CallLog))).all()) == []
    assert list((await session.exec(select(EmailAutomationEvent))).all()) == []


@pytest.mark.asyncio
async def test_urgent_email_does_not_schedule_into_quiet_hours(session):
    user = await _create_user(session, urgent=True)
    svc = EmailAutomationService(session)
    urgent_email = _full_email(
        subject="Urgent: action required",
        body="Please respond asap. This is time sensitive.",
    )

    with (
        patch(
            "app.services.email_automation_service.search_emails",
            new=AsyncMock(return_value=[_summary(subject=urgent_email["subject"])]),
        ),
        patch(
            "app.services.email_automation_service.get_email_for_reply",
            new=AsyncMock(return_value=urgent_email),
        ),
    ):
        result = await svc.process_user(
            user,
            now=datetime(2026, 4, 26, 20, 59, tzinfo=timezone.utc),
        )

    assert result.urgent_calls_scheduled == 0
    assert list((await session.exec(select(CallLog))).all()) == []
    assert list((await session.exec(select(EmailAutomationEvent))).all()) == []


@pytest.mark.asyncio
async def test_process_user_requires_opt_in(session):
    user = await _create_user(session)
    result = await EmailAutomationService(session).process_user(user)

    assert result.emails_scanned == 0
    assert result.skipped == [f"user:{user.id}:automation_not_opted_in"]
