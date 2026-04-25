"""Tests for expanded Google Calendar read/write service methods."""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from app.models.user import User
from app.services import google_calendar_read_service as calendar_read
from app.services import google_calendar_write_service as calendar_write


class _Executable:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class _FakeEvents:
    def __init__(self, *, items=None):
        self.items = items or []
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(("list", kwargs))
        return _Executable({"items": self.items})

    def insert(self, **kwargs):
        self.calls.append(("insert", kwargs))
        body = kwargs["body"]
        return _Executable(
            {
                "id": "event_created",
                "htmlLink": "https://calendar.google.com/event_created",
                "summary": body["summary"],
                "start": body["start"],
                "end": body["end"],
            }
        )

    def patch(self, **kwargs):
        self.calls.append(("patch", kwargs))
        body = kwargs["body"]
        return _Executable(
            {
                "id": kwargs["eventId"],
                "htmlLink": "https://calendar.google.com/event_updated",
                "summary": body.get("summary", "Existing"),
                "start": body.get("start", {"dateTime": "2026-05-01T10:00:00-04:00"}),
                "end": body.get("end", {"dateTime": "2026-05-01T10:30:00-04:00"}),
            }
        )

    def delete(self, **kwargs):
        self.calls.append(("delete", kwargs))
        return _Executable({})


class _FakeCalendarService:
    def __init__(self, events: _FakeEvents):
        self._events = events

    def events(self):
        return self._events


async def _run_google_call(**kwargs):
    return kwargs["api_callable"]()


def _user() -> User:
    return User(
        id=123,
        phone="+15551234567",
        timezone="America/New_York",
        google_access_token_encrypted="access",
        google_refresh_token_encrypted="refresh",
        google_granted_scopes="https://www.googleapis.com/auth/calendar",
    )


@pytest.mark.asyncio
async def test_fetch_events_for_range_filters_and_uses_inclusive_dates(session):
    events = _FakeEvents(
        items=[
            {
                "id": "keep",
                "summary": "Planning",
                "status": "confirmed",
                "start": {"dateTime": "2026-05-01T09:00:00-04:00"},
                "end": {"dateTime": "2026-05-01T09:30:00-04:00"},
            },
            {"id": "cancelled", "status": "cancelled"},
            {
                "id": "declined",
                "status": "confirmed",
                "attendees": [{"self": True, "responseStatus": "declined"}],
            },
        ]
    )
    service = _FakeCalendarService(events)

    with (
        patch.object(calendar_read, "build_google_credentials", return_value=MagicMock()),
        patch.object(calendar_read, "_build_calendar_service", return_value=service),
        patch.object(calendar_read, "google_api_call", side_effect=_run_google_call),
    ):
        result = await calendar_read.fetch_events_for_range(
            _user(),
            session,
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 2),
        )

    assert [event["id"] for event in result] == ["keep"]
    _, kwargs = events.calls[0]
    assert kwargs["calendarId"] == "primary"
    assert kwargs["timeMin"].startswith("2026-05-01T00:00:00")
    assert kwargs["timeMax"].startswith("2026-05-03T00:00:00")
    assert kwargs["singleEvents"] is True
    assert kwargs["orderBy"] == "startTime"


@pytest.mark.asyncio
async def test_calendar_event_create_update_delete_use_events_resource(session):
    events = _FakeEvents()
    service = _FakeCalendarService(events)

    with (
        patch.object(calendar_write, "build_google_credentials", return_value=MagicMock()),
        patch.object(calendar_write, "_build_calendar_service", return_value=service),
        patch.object(calendar_write, "google_api_call", side_effect=_run_google_call),
    ):
        created = await calendar_write.create_event(
            _user(),
            session,
            summary="Planning session",
            start_iso="2026-05-01T09:00:00-04:00",
            end_iso="2026-05-01T09:30:00-04:00",
            description="Quarterly planning",
        )
        updated = await calendar_write.update_event(
            _user(),
            session,
            event_id="event_created",
            summary="Updated planning session",
        )
        deleted = await calendar_write.delete_event(
            _user(),
            session,
            event_id="event_created",
        )

    assert created["status"] == "created"
    assert created["summary"] == "Planning session"
    assert updated["status"] == "updated"
    assert updated["summary"] == "Updated planning session"
    assert deleted == {"status": "deleted", "event_id": "event_created"}

    insert_call = events.calls[0][1]
    assert insert_call["calendarId"] == "primary"
    assert insert_call["body"]["description"] == "Quarterly planning"
    assert insert_call["body"]["source"]["title"] == "Charu AI"

    patch_call = events.calls[1][1]
    assert patch_call["eventId"] == "event_created"
    assert patch_call["body"] == {"summary": "Updated planning session"}

    delete_call = events.calls[2][1]
    assert delete_call["eventId"] == "event_created"


@pytest.mark.asyncio
async def test_calendar_event_validation(session):
    with pytest.raises(ValueError, match="summary cannot be empty"):
        await calendar_write.create_event(
            _user(),
            session,
            summary=" ",
            start_iso="2026-05-01T09:00:00-04:00",
            end_iso="2026-05-01T09:30:00-04:00",
        )

    with pytest.raises(ValueError, match="end_iso must be after start_iso"):
        await calendar_write.create_event(
            _user(),
            session,
            summary="Planning",
            start_iso="2026-05-01T09:30:00-04:00",
            end_iso="2026-05-01T09:00:00-04:00",
        )

    with pytest.raises(ValueError, match="At least one"):
        await calendar_write.update_event(
            _user(),
            session,
            event_id="event_created",
        )
