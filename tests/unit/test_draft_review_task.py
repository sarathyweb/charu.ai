"""Unit tests for draft-review Celery task."""

from __future__ import annotations

from datetime import timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.email_draft_state import EmailDraftState
from app.models.enums import DraftStatus
from app.models.outbound_message import OutboundMessage
from app.models.user import User
from app.tasks import draft_review


class SessionFactory:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _wa(template_sid: str = "SM_template", reply_sids: list[str] | None = None):
    wa = MagicMock()
    wa.send_template_message = AsyncMock(return_value=template_sid)
    wa.send_reply = AsyncMock(return_value=reply_sids or ["SM_overflow"])
    return wa


async def _create_draft(session, *, status: str = DraftStatus.PENDING_REVIEW.value, text: str = "Looks good."):
    user = User(phone="+15558880001", name="Draft User", timezone="UTC")
    session.add(user)
    await session.commit()
    await session.refresh(user)

    draft = EmailDraftState(
        user_id=user.id,
        thread_id="thread-1",
        original_email_id="msg-1",
        original_from='"Sarah" <sarah@example.com>',
        original_subject="Project check-in",
        original_message_id="<msg-1@example.com>",
        draft_text=text,
        status=status,
    )
    session.add(draft)
    await session.commit()
    await session.refresh(draft)
    return user, draft


@pytest.mark.asyncio
async def test_send_draft_review_sends_template_and_stamps(session, monkeypatch):
    _, draft = await _create_draft(session)
    fake_wa = _wa()
    monkeypatch.setattr(draft_review, "async_session_factory", SessionFactory(session))
    monkeypatch.setattr(draft_review, "WhatsAppService", lambda: fake_wa)
    monkeypatch.setattr(draft_review, "_get_content_sid", lambda: "HX_draft")

    result = await draft_review._run_send_draft_review(draft.id)

    assert result == f"Draft review sent for draft {draft.id}"
    fake_wa.send_template_message.assert_awaited_once()
    fake_wa.send_reply.assert_not_awaited()
    refreshed = await session.get(EmailDraftState, draft.id)
    assert refreshed.draft_review_sent_at is not None
    assert refreshed.draft_review_sent_at.tzinfo is not None
    assert refreshed.draft_review_sent_at.utcoffset() == timezone.utc.utcoffset(None)


@pytest.mark.asyncio
async def test_send_draft_review_skips_non_pending(session, monkeypatch):
    _, draft = await _create_draft(session, status=DraftStatus.REVISION_REQUESTED.value)
    fake_wa = _wa()
    monkeypatch.setattr(draft_review, "async_session_factory", SessionFactory(session))
    monkeypatch.setattr(draft_review, "WhatsAppService", lambda: fake_wa)

    result = await draft_review._run_send_draft_review(draft.id)

    assert "skipping review" in result
    fake_wa.send_template_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_draft_review_sends_overflow_for_long_draft(session, monkeypatch):
    long_text = "A" * 2000
    _, draft = await _create_draft(session, text=long_text)
    fake_wa = _wa()
    monkeypatch.setattr(draft_review, "async_session_factory", SessionFactory(session))
    monkeypatch.setattr(draft_review, "WhatsAppService", lambda: fake_wa)
    monkeypatch.setattr(draft_review, "_get_content_sid", lambda: "HX_draft")

    await draft_review._run_send_draft_review(draft.id)

    fake_wa.send_template_message.assert_awaited_once()
    fake_wa.send_reply.assert_awaited_once()
    assert fake_wa.send_reply.await_args.kwargs["body"] == long_text


@pytest.mark.asyncio
async def test_send_draft_review_idempotent_after_stamp(session, monkeypatch):
    _, draft = await _create_draft(session)
    draft.draft_review_sent_at = draft.created_at
    session.add(draft)
    await session.commit()
    fake_wa = _wa()
    monkeypatch.setattr(draft_review, "async_session_factory", SessionFactory(session))
    monkeypatch.setattr(draft_review, "WhatsAppService", lambda: fake_wa)

    result = await draft_review._run_send_draft_review(draft.id)

    assert "already sent" in result
    fake_wa.send_template_message.assert_not_awaited()
