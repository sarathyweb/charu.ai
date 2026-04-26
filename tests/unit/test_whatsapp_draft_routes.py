"""Route-level WhatsApp draft approval tests."""

from __future__ import annotations

import urllib.parse
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import whatsapp
from app.auth.twilio import verify_twilio_signature
from app.models.email_draft_state import EmailDraftState
from app.models.enums import DraftStatus
from app.models.schemas import AgentRunResult
from app.models.user import User


def _make_app(session, form_data: dict) -> FastAPI:
    app = FastAPI()
    app.include_router(whatsapp.router)
    app.dependency_overrides[verify_twilio_signature] = lambda: form_data
    app.dependency_overrides[whatsapp._get_db_session] = lambda: session
    app.state.runner = MagicMock()
    app.state.session_service = MagicMock()
    return app


async def _create_user_and_draft(session, *, status: str = DraftStatus.PENDING_REVIEW.value):
    user = User(phone="+14155552671", name="Asha", timezone="UTC")
    session.add(user)
    await session.commit()
    await session.refresh(user)

    draft = EmailDraftState(
        user_id=user.id,
        thread_id="thread-route",
        original_email_id="msg-route",
        original_from="Sarah <sarah@example.com>",
        original_subject="Project update",
        original_message_id="<msg-route@example.com>",
        draft_text="Thanks, I will review this today.",
        status=status,
    )
    session.add(draft)
    await session.commit()
    await session.refresh(draft)
    return user, draft


def _form(body: str, sid: str = "SM_draft_route") -> dict:
    return {
        "From": "whatsapp:+14155552671",
        "Body": body,
        "MessageSid": sid,
    }


async def _post(app: FastAPI, form_data: dict):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/webhook/whatsapp",
            content=urllib.parse.urlencode(form_data),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )


@pytest.mark.asyncio
async def test_whatsapp_route_approves_pending_draft(session):
    user, draft = await _create_user_and_draft(session)
    form_data = _form("send it")
    app = _make_app(session, form_data)
    fake_wa = MagicMock()
    fake_wa.send_reply = AsyncMock()

    async def fake_send_approved_reply(*, user, draft_id, session):
        saved = await session.get(EmailDraftState, draft_id)
        saved.status = DraftStatus.SENT.value
        session.add(saved)
        await session.flush()
        return {
            "status": "sent",
            "thread_id": "thread-route",
            "message": "sent",
        }

    with (
        patch("app.api.whatsapp.WhatsAppService", return_value=fake_wa),
        patch(
            "app.services.email_draft_service.send_approved_reply",
            AsyncMock(side_effect=fake_send_approved_reply),
        ),
    ):
        response = await _post(app, form_data)

    assert response.status_code == 200
    await session.refresh(draft)
    assert draft.status == DraftStatus.SENT.value
    fake_wa.send_reply.assert_awaited_once()
    assert "has been sent" in fake_wa.send_reply.await_args.kwargs["body"]
    assert fake_wa.send_reply.await_args.kwargs["to"] == user.phone


@pytest.mark.asyncio
async def test_whatsapp_route_abandons_pending_draft(session):
    _, draft = await _create_user_and_draft(session)
    form_data = _form("don't send", sid="SM_draft_abandon")
    app = _make_app(session, form_data)
    fake_wa = MagicMock()
    fake_wa.send_reply = AsyncMock()

    with patch("app.api.whatsapp.WhatsAppService", return_value=fake_wa):
        response = await _post(app, form_data)

    assert response.status_code == 200
    await session.refresh(draft)
    assert draft.status == DraftStatus.ABANDONED.value
    fake_wa.send_reply.assert_awaited_once()
    assert "No email was sent" in fake_wa.send_reply.await_args.kwargs["body"]


@pytest.mark.asyncio
async def test_whatsapp_route_sends_revision_request_through_agent(session):
    _, draft = await _create_user_and_draft(session)
    form_data = _form("make it warmer", sid="SM_draft_revise")
    app = _make_app(session, form_data)
    fake_wa = MagicMock()
    fake_wa.send_reply = AsyncMock()
    captured: dict[str, str] = {}

    class FakeAgentService:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, *, user_id: str, message: str, channel: str):
            captured["user_id"] = user_id
            captured["message"] = message
            captured["channel"] = channel
            return AgentRunResult(
                reply="I revised the draft and saved it for review.",
                session_id="session-draft",
            )

    with (
        patch("app.api.whatsapp.WhatsAppService", return_value=fake_wa),
        patch("app.api.whatsapp.AgentService", FakeAgentService),
    ):
        response = await _post(app, form_data)

    assert response.status_code == 200
    assert captured["user_id"] == "+14155552671"
    assert captured["channel"] == "whatsapp"
    assert f"draft_id={draft.id}" in captured["message"]
    assert "make it warmer" in captured["message"]
    fake_wa.send_reply.assert_awaited_once_with(
        to="+14155552671",
        body="I revised the draft and saved it for review.",
    )
