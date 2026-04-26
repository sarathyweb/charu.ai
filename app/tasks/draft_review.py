"""Email draft-review WhatsApp notification task."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlmodel import select

from app.celery_app import celery_app, run_async
from app.config import get_settings
from app.db import async_session_factory
from app.models.email_draft_state import EmailDraftState
from app.models.enums import DraftStatus, OutboundMessageStatus
from app.models.outbound_message import OutboundMessage
from app.models.user import User
from app.services.outbound_message_service import (
    OutboundMessageService,
    draft_review_dedup_key,
)
from app.services.whatsapp_service import (
    WhatsAppService,
    build_email_draft_review_params,
)

logger = logging.getLogger(__name__)

_CONTENT_SID: str | None = None


def _get_content_sid() -> str:
    global _CONTENT_SID  # noqa: PLW0603
    if _CONTENT_SID is not None:
        return _CONTENT_SID

    settings = get_settings()
    sid = getattr(settings, "TWILIO_CONTENT_SID_EMAIL_DRAFT_REVIEW", None)
    if sid:
        _CONTENT_SID = sid
        return sid

    logger.warning(
        "No Twilio Content SID configured for email_draft_review "
        "(expected settings.TWILIO_CONTENT_SID_EMAIL_DRAFT_REVIEW)"
    )
    return "MISSING_CONTENT_SID:email_draft_review"


async def _outbound_was_sent(dedup_key: str) -> bool:
    async with async_session_factory() as session:
        result = await session.exec(
            select(OutboundMessage).where(OutboundMessage.dedup_key == dedup_key)
        )
        row = result.first()
        return bool(row and row.status == OutboundMessageStatus.SENT.value)


async def _run_send_draft_review(draft_id: int) -> str:
    """Send a WhatsApp draft-review prompt for a pending email draft."""
    async with async_session_factory() as session:
        draft = await session.get(EmailDraftState, draft_id)
        if draft is None:
            return f"EmailDraftState {draft_id} not found"

        if draft.status != DraftStatus.PENDING_REVIEW.value:
            return f"Draft {draft_id} status={draft.status}, skipping review"

        if draft.draft_review_sent_at is not None:
            return f"Draft {draft_id} review already sent, skipping"

        user = await session.get(User, draft.user_id)
        if user is None:
            return f"User {draft.user_id} not found for draft {draft_id}"

        sender_name = draft.original_from.split("<")[0].strip().strip('"')
        sender_name = sender_name or draft.original_from
        params, overflow = build_email_draft_review_params(
            sender_name=sender_name,
            subject=draft.original_subject,
            draft_text=draft.draft_text,
        )

        wa = WhatsAppService()
        outbound = OutboundMessageService(session, wa)
        template_key = draft_review_dedup_key(draft_id)
        template_sid = await outbound.send_template_dedup(
            user_id=user.id,  # type: ignore[arg-type]
            dedup_key=template_key,
            to=user.phone,
            content_sid=_get_content_sid(),
            content_variables=params,
        )

        template_sent = template_sid is not None or await _outbound_was_sent(template_key)
        if not template_sent:
            return f"Draft {draft_id} review send failed or is blocked by dedup"

        if overflow:
            overflow_key = f"draft_review_overflow:{draft_id}"
            await outbound.send_freeform_dedup(
                user_id=user.id,  # type: ignore[arg-type]
                dedup_key=overflow_key,
                to=user.phone,
                body=overflow,
            )

        draft.draft_review_sent_at = datetime.now(timezone.utc)
        draft.updated_at = datetime.now(timezone.utc)
        session.add(draft)
        await session.commit()

    return f"Draft review sent for draft {draft_id}"


@celery_app.task(
    bind=True,
    name="app.tasks.draft_review.send_draft_review",
    max_retries=3,
    default_retry_delay=30,
)
def send_draft_review(self, draft_id: int) -> str:
    """Send a WhatsApp draft-review notification for a saved Gmail draft."""
    try:
        return run_async(_run_send_draft_review(draft_id))
    except Exception as exc:
        logger.exception("send_draft_review failed for draft_id=%d", draft_id)
        raise self.retry(exc=exc)
