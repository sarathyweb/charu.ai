"""Tests for expanded Gmail read/write service methods."""

import base64
from email import message_from_bytes
from unittest.mock import MagicMock, patch

import pytest

from app.models.user import User
from app.services import gmail_read_service as gmail_read
from app.services import gmail_write_service as gmail_write


class _Executable:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class _FakeMessages:
    def __init__(self):
        self.calls = []
        self.sent_raw = ""

    def list(self, **kwargs):
        self.calls.append(("list", kwargs))
        return _Executable({"messages": [{"id": "msg_1"}, {"id": "msg_2"}]})

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        msg_id = kwargs["id"]
        if kwargs.get("format") == "full":
            encoded_body = base64.urlsafe_b64encode(b"Full body text").decode()
            return _Executable(
                {
                    "id": msg_id,
                    "threadId": "thread_1",
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "Asha <asha@example.com>"},
                            {"name": "Subject", "value": "Launch plan"},
                            {"name": "Date", "value": "Fri, 1 May 2026 09:00:00 -0400"},
                            {"name": "Message-ID", "value": "<msg_1@example.com>"},
                        ],
                        "mimeType": "text/plain",
                        "body": {"data": encoded_body},
                    },
                }
            )

        return _Executable(
            {
                "id": msg_id,
                "threadId": f"thread_{msg_id}",
                "snippet": f"Snippet for {msg_id}",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Asha <asha@example.com>"},
                        {"name": "Subject", "value": f"Subject {msg_id}"},
                        {"name": "Date", "value": "Fri, 1 May 2026 09:00:00 -0400"},
                    ]
                },
            }
        )

    def send(self, **kwargs):
        self.calls.append(("send", kwargs))
        self.sent_raw = kwargs["body"]["raw"]
        return _Executable({"id": "sent_msg_1", "threadId": "sent_thread_1"})

    def modify(self, **kwargs):
        self.calls.append(("modify", kwargs))
        return _Executable({"id": kwargs["id"], "threadId": "thread_archived"})


class _FakeUsers:
    def __init__(self, messages: _FakeMessages):
        self._messages = messages

    def messages(self):
        return self._messages


class _FakeGmailService:
    def __init__(self, messages: _FakeMessages):
        self._users = _FakeUsers(messages)

    def users(self):
        return self._users


async def _run_google_call(**kwargs):
    return kwargs["api_callable"]()


def _user() -> User:
    return User(
        id=123,
        phone="+15551234567",
        google_access_token_encrypted="access",
        google_refresh_token_encrypted="refresh",
        google_granted_scopes="https://www.googleapis.com/auth/gmail.modify",
    )


@pytest.mark.asyncio
async def test_search_and_read_email_by_query(session):
    messages = _FakeMessages()
    service = _FakeGmailService(messages)

    with (
        patch.object(gmail_read, "build_google_credentials", return_value=MagicMock()),
        patch.object(gmail_read, "_build_gmail_service", return_value=service),
        patch.object(gmail_read, "google_api_call", side_effect=_run_google_call),
    ):
        summaries = await gmail_read.search_emails(
            _user(),
            session,
            query="from:asha launch",
            max_results=2,
        )
        full_email = await gmail_read.read_email_by_query(
            _user(),
            session,
            query="subject:launch",
        )

    assert [email["id"] for email in summaries] == ["msg_1", "msg_2"]
    assert summaries[0]["subject"] == "Subject msg_1"
    assert full_email["id"] == "msg_1"
    assert full_email["body"] == "Full body text"

    list_calls = [kwargs for name, kwargs in messages.calls if name == "list"]
    assert list_calls[0]["q"] == "from:asha launch"
    assert list_calls[0]["maxResults"] == 2


@pytest.mark.asyncio
async def test_send_new_email_and_archive_email(session):
    messages = _FakeMessages()
    service = _FakeGmailService(messages)

    with (
        patch.object(gmail_write, "build_google_credentials", return_value=MagicMock()),
        patch.object(gmail_write, "_build_gmail_service", return_value=service),
        patch.object(gmail_write, "google_api_call", side_effect=_run_google_call),
    ):
        sent = await gmail_write.send_new_email(
            user=_user(),
            session=session,
            to_address="asha@example.com",
            subject="Launch plan",
            body_text="Let's ship this.",
        )
        archived = await gmail_write.archive_email(
            user=_user(),
            session=session,
            message_id="msg_1",
        )

    assert sent == {
        "status": "sent",
        "gmail_message_id": "sent_msg_1",
        "thread_id": "sent_thread_1",
        "message": "Email sent to asha@example.com.",
    }
    raw_bytes = base64.urlsafe_b64decode(messages.sent_raw.encode())
    parsed = message_from_bytes(raw_bytes)
    assert parsed["to"] == "asha@example.com"
    assert parsed["subject"] == "Launch plan"
    assert "Let's ship this." in parsed.get_payload(decode=True).decode()

    assert archived == {
        "status": "archived",
        "message_id": "msg_1",
        "thread_id": "thread_archived",
    }
    modify_call = next(kwargs for name, kwargs in messages.calls if name == "modify")
    assert modify_call["body"] == {"removeLabelIds": ["INBOX"]}


@pytest.mark.asyncio
async def test_gmail_expansion_validation(session):
    with pytest.raises(ValueError, match="query cannot be empty"):
        await gmail_read.search_emails(_user(), session, query=" ")

    with pytest.raises(ValueError, match="max_results must be between 1 and 10"):
        await gmail_read.search_emails(_user(), session, query="launch", max_results=0)

    with pytest.raises(ValueError, match="to_address cannot be empty"):
        await gmail_write.send_new_email(
            user=_user(),
            session=session,
            to_address=" ",
            subject="Launch plan",
            body_text="Let's ship this.",
        )

    with pytest.raises(ValueError, match="message_id cannot be empty"):
        await gmail_write.archive_email(
            user=_user(),
            session=session,
            message_id=" ",
        )
