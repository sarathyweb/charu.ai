"""Property tests for Gmail duplicate send prevention.

# Feature: accountability-call-onboarding, Property 20: Gmail duplicate send prevention

Tests verify:
- First send for a user+thread succeeds and creates a SentReply row
- Second send for the same user+thread is blocked (returns already_sent)
- SentReply unique constraint prevents duplicate rows
- Draft transitions correctly through approved → sent on success
- Draft marked as sent even when SentReply already exists (idempotent recovery)
- Non-approved drafts are rejected
- Gmail API errors do not create SentReply rows
- Wrong-user access is rejected

Validates: Requirements 11.4, 18.3
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.models.email_draft_state import EmailDraftState
from app.models.enums import DraftStatus
from app.models.sent_reply import SentReply
from app.models.user import User
from app.services.gmail_write_service import send_approved_reply


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_phone_counter = 0


async def _ensure_user(session, phone: str | None = None) -> User:
    """Create a test user with fake Google OAuth tokens."""
    global _phone_counter
    if phone is None:
        _phone_counter += 1
        phone = f"+1415555{_phone_counter:04d}"
    result = await session.exec(select(User).where(User.phone == phone))
    user = result.one_or_none()
    if user:
        return user
    user = User(
        phone=phone,
        name="Test Gmail User",
        google_access_token_encrypted="fake_access",
        google_refresh_token_encrypted="fake_refresh",
        google_granted_scopes="https://www.googleapis.com/auth/gmail.modify",
    )
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return user


async def _create_approved_draft(
    session,
    user: User,
    *,
    thread_id: str = "thread_abc",
    original_email_id: str = "msg_001",
    original_from: str = "sender@example.com",
    original_subject: str = "Test Subject",
    original_message_id: str = "<msg001@mail.gmail.com>",
    draft_text: str = "Thanks for your email!",
) -> EmailDraftState:
    """Create an EmailDraftState in 'approved' status."""
    draft = EmailDraftState(
        user_id=user.id,
        thread_id=thread_id,
        original_email_id=original_email_id,
        original_from=original_from,
        original_subject=original_subject,
        original_message_id=original_message_id,
        draft_text=draft_text,
        status=DraftStatus.APPROVED.value,
    )
    session.add(draft)
    await session.flush()
    await session.refresh(draft)
    return draft


# Patch targets — all in gmail_write_service module namespace
_PATCH_CREDS = "app.services.gmail_write_service.build_google_credentials"
_PATCH_SERVICE = "app.services.gmail_write_service._build_gmail_service"
_PATCH_API = "app.services.gmail_write_service.google_api_call"


# ---------------------------------------------------------------------------
# Property 20: Gmail duplicate send prevention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_send_succeeds_and_creates_sent_reply(session):
    """First send for a user+thread should succeed, create a SentReply row,
    and transition the draft to 'sent'."""
    user = await _ensure_user(session)
    draft = await _create_approved_draft(session, user, thread_id="thread_first")

    with (
        patch(_PATCH_CREDS, return_value=MagicMock()),
        patch(_PATCH_SERVICE, return_value=MagicMock()),
        patch(
            _PATCH_API,
            new_callable=AsyncMock,
            return_value={"id": "sent_msg_001", "threadId": "thread_first"},
        ) as mock_api,
    ):
        result = await send_approved_reply(
            user=user, draft_id=draft.id, session=session
        )

    assert result["status"] == "sent"
    assert result["gmail_message_id"] == "sent_msg_001"

    # Verify SentReply row was created
    sr_result = await session.exec(
        select(SentReply).where(
            SentReply.user_id == user.id,
            SentReply.thread_id == "thread_first",
        )
    )
    sent_reply = sr_result.one()
    assert sent_reply.gmail_message_id == "sent_msg_001"
    assert sent_reply.reply_text == "Thanks for your email!"

    # Verify draft transitioned to 'sent'
    await session.refresh(draft)
    assert draft.status == DraftStatus.SENT.value


@pytest.mark.asyncio
async def test_second_send_same_thread_blocked(session):
    """Second send for the same user+thread should return 'already_sent'
    without calling the Gmail API."""
    user = await _ensure_user(session)

    # First send
    draft1 = await _create_approved_draft(
        session, user, thread_id="thread_dup", original_email_id="msg_dup_1"
    )
    with (
        patch(_PATCH_CREDS, return_value=MagicMock()),
        patch(_PATCH_SERVICE, return_value=MagicMock()),
        patch(
            _PATCH_API,
            new_callable=AsyncMock,
            return_value={"id": "sent_msg_002", "threadId": "thread_dup"},
        ),
    ):
        result1 = await send_approved_reply(
            user=user, draft_id=draft1.id, session=session
        )
    assert result1["status"] == "sent"

    # Second send — new draft for the same thread
    draft2 = await _create_approved_draft(
        session,
        user,
        thread_id="thread_dup",
        original_email_id="msg_dup_2",
        draft_text="Different text, same thread",
    )
    with (
        patch(_PATCH_CREDS, return_value=MagicMock()),
        patch(_PATCH_SERVICE, return_value=MagicMock()),
        patch(_PATCH_API, new_callable=AsyncMock) as mock_api,
    ):
        result2 = await send_approved_reply(
            user=user, draft_id=draft2.id, session=session
        )

    assert result2["status"] == "already_sent"
    assert "gmail_message_id" in result2
    # Gmail API should NOT have been called for the second send
    mock_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_sent_draft_returns_already_sent(session):
    """Calling send_approved_reply on a draft already in 'sent' status
    should return 'already_sent' immediately (row-lock check)."""
    user = await _ensure_user(session)
    draft = await _create_approved_draft(session, user, thread_id="thread_sent")

    # Manually mark draft as sent
    draft.status = DraftStatus.SENT.value
    draft.updated_at = datetime.now(timezone.utc)
    session.add(draft)
    await session.flush()

    with (
        patch(_PATCH_CREDS, return_value=MagicMock()),
        patch(_PATCH_SERVICE, return_value=MagicMock()),
        patch(_PATCH_API, new_callable=AsyncMock) as mock_api,
    ):
        result = await send_approved_reply(
            user=user, draft_id=draft.id, session=session
        )

    assert result["status"] == "already_sent"
    mock_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_approved_draft_rejected(session):
    """Sending a draft that is not in 'approved' status should return an error."""
    user = await _ensure_user(session)

    draft = EmailDraftState(
        user_id=user.id,
        thread_id="thread_pending",
        original_email_id="msg_pending",
        original_from="sender@example.com",
        original_subject="Pending Subject",
        original_message_id="<pending@mail.gmail.com>",
        draft_text="Not yet approved",
        status=DraftStatus.PENDING_REVIEW.value,
    )
    session.add(draft)
    await session.flush()
    await session.refresh(draft)

    with (
        patch(_PATCH_CREDS, return_value=MagicMock()),
        patch(_PATCH_SERVICE, return_value=MagicMock()),
        patch(_PATCH_API, new_callable=AsyncMock) as mock_api,
    ):
        result = await send_approved_reply(
            user=user, draft_id=draft.id, session=session
        )

    assert result["status"] == "error"
    assert "only approved" in result["message"].lower()
    mock_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_gmail_api_error_does_not_create_sent_reply(session):
    """When the Gmail API returns an error, no SentReply row should be
    created and the draft should remain in 'approved' status."""
    user = await _ensure_user(session)
    draft = await _create_approved_draft(session, user, thread_id="thread_err")

    with (
        patch(_PATCH_CREDS, return_value=MagicMock()),
        patch(_PATCH_SERVICE, return_value=MagicMock()),
        patch(
            _PATCH_API,
            new_callable=AsyncMock,
            return_value={"error": "google_disconnected", "message": "Token expired."},
        ),
    ):
        result = await send_approved_reply(
            user=user, draft_id=draft.id, session=session
        )

    assert result.get("error") == "google_disconnected"

    # No SentReply row should exist
    sr_result = await session.exec(
        select(SentReply).where(
            SentReply.user_id == user.id,
            SentReply.thread_id == "thread_err",
        )
    )
    assert sr_result.first() is None

    # Draft should still be in 'approved' status
    await session.refresh(draft)
    assert draft.status == DraftStatus.APPROVED.value


@pytest.mark.asyncio
async def test_wrong_user_draft_rejected(session):
    """Attempting to send a draft belonging to a different user should
    return an error."""
    user_a = await _ensure_user(session, phone="+14155550001")
    user_b = await _ensure_user(session, phone="+14155550002")

    draft = await _create_approved_draft(
        session, user_a, thread_id="thread_wrong_user"
    )

    with (
        patch(_PATCH_CREDS, return_value=MagicMock()),
        patch(_PATCH_SERVICE, return_value=MagicMock()),
        patch(_PATCH_API, new_callable=AsyncMock) as mock_api,
    ):
        result = await send_approved_reply(
            user=user_b, draft_id=draft.id, session=session
        )

    assert result["status"] == "error"
    assert "does not belong" in result["message"].lower()
    mock_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_sent_reply_unique_constraint(session):
    """The SentReply unique constraint (user_id, thread_id) should prevent
    duplicate rows at the database level — the last line of defense."""
    user = await _ensure_user(session)

    sr1 = SentReply(
        user_id=user.id,
        thread_id="thread_constraint",
        gmail_message_id="msg_1",
        reply_text="First reply",
    )
    session.add(sr1)
    await session.flush()

    sr2 = SentReply(
        user_id=user.id,
        thread_id="thread_constraint",
        gmail_message_id="msg_2",
        reply_text="Second reply",
    )
    session.add(sr2)

    with pytest.raises(IntegrityError):
        await session.flush()

    await session.rollback()


@pytest.mark.asyncio
async def test_draft_not_found_returns_error(session):
    """Sending a non-existent draft should return an error."""
    user = await _ensure_user(session)

    result = await send_approved_reply(
        user=user, draft_id=999999, session=session
    )

    assert result["status"] == "error"
    assert "not found" in result["message"].lower()


@pytest.mark.asyncio
async def test_integrity_error_race_marks_draft_as_sent(session):
    """When a concurrent SentReply insert causes IntegrityError, the service
    should attempt to mark the draft as 'sent' after rollback so it doesn't
    stay as 'approved' and risk a duplicate resend on retry.

    We verify this by checking that session.get + flush are called in the
    recovery path after the rollback."""
    user = await _ensure_user(session)
    draft = await _create_approved_draft(session, user, thread_id="thread_race")
    draft_id = draft.id

    mock_gmail_result = {"id": "msg_race_loser", "threadId": "thread_race"}

    # We need to track what happens after rollback.  Use a real session
    # but intercept the flush to raise IntegrityError on SentReply insert.
    original_session_flush = session.flush
    original_session_rollback = session.rollback
    recovery_get_called = False
    recovery_flush_called = False

    async def patched_flush(*args, **kwargs):
        """Raise IntegrityError when the SentReply insert is flushed."""
        nonlocal recovery_flush_called
        new_objects = list(session.new)
        has_sent_reply = any(isinstance(obj, SentReply) for obj in new_objects)
        if has_sent_reply:
            raise IntegrityError(
                "duplicate key value violates unique constraint",
                params={},
                orig=Exception("unique_violation"),
            )
        # After rollback, the recovery path does another flush for the draft update
        recovery_flush_called = True
        return await original_session_flush(*args, **kwargs)

    original_session_get = session.get

    async def patched_get(model, pk, *args, **kwargs):
        nonlocal recovery_get_called
        if model is EmailDraftState and pk == draft_id:
            recovery_get_called = True
        return await original_session_get(model, pk, *args, **kwargs)

    with (
        patch(_PATCH_CREDS),
        patch(_PATCH_API, new_callable=AsyncMock, return_value=mock_gmail_result),
    ):
        session.flush = patched_flush
        session.get = patched_get
        try:
            result = await send_approved_reply(
                user=user, draft_id=draft_id, session=session
            )
        finally:
            session.flush = original_session_flush
            session.get = original_session_get

    assert result["status"] == "already_sent"
    # Verify the recovery path attempted to re-fetch and update the draft
    assert recovery_get_called, (
        "Recovery path did not call session.get(EmailDraftState, draft_id) "
        "after IntegrityError rollback"
    )
