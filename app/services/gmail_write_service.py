"""Gmail write service — send approved replies with duplicate prevention.

Implements the draft-review-send pipeline for Gmail replies:
- ``send_approved_reply`` locks the ``EmailDraftState`` row, checks the
  ``SentReply`` table, calls the Gmail API, then records the send — all
  through the shared ``google_api_call`` wrapper (task 8.6).

Requirements: 18
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import formatdate

from googleapiclient.discovery import build
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.email_draft_state import EmailDraftState
from app.models.enums import DraftStatus
from app.models.sent_reply import SentReply
from app.models.user import User
from app.services.google_api_wrapper import google_api_call
from app.services.google_oauth_service import build_google_credentials

logger = logging.getLogger(__name__)


def _build_gmail_service(credentials):
    """Build a Gmail API v1 service object."""
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def _compose_reply_message(
    *,
    recipient: str,
    subject: str,
    reply_text: str,
    original_message_id: str,
) -> MIMEText:
    """Compose a properly-threaded MIME reply message.

    Sets ``In-Reply-To`` and ``References`` headers to the original
    MIME ``Message-ID`` so that Gmail (and other clients) thread the
    reply correctly.
    """
    message = MIMEText(reply_text, _charset="utf-8")
    message["to"] = recipient
    message["subject"] = (
        subject if subject.lower().startswith("re:") else f"Re: {subject}"
    )
    message["In-Reply-To"] = original_message_id
    message["References"] = original_message_id
    return message


def _clean_required_text(value: str, field_name: str) -> str:
    """Return stripped text or raise a validation error."""
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field_name} cannot be empty.")
    return clean


def _compose_new_message(
    *,
    to_address: str,
    subject: str,
    body_text: str,
) -> MIMEText:
    """Compose a new outbound email message."""
    message = MIMEText(body_text, _charset="utf-8")
    message["to"] = to_address
    message["subject"] = subject
    message["date"] = formatdate(localtime=True)
    return message


async def send_new_email(
    *,
    user: User,
    session: AsyncSession,
    to_address: str,
    subject: str,
    body_text: str,
) -> dict:
    """Send a new Gmail message that is not a reply."""
    clean_to = _clean_required_text(to_address, "to_address")
    clean_subject = _clean_required_text(subject, "subject")
    clean_body = _clean_required_text(body_text, "body_text")

    credentials = build_google_credentials(
        access_token_encrypted=user.google_access_token_encrypted,
        refresh_token_encrypted=user.google_refresh_token_encrypted,
        token_expiry=user.google_token_expiry,
    )
    service = _build_gmail_service(credentials)

    mime_msg = _compose_new_message(
        to_address=clean_to,
        subject=clean_subject,
        body_text=clean_body,
    )
    raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode("utf-8")

    send_result = await google_api_call(
        user=user,
        credentials=credentials,
        api_callable=lambda: service.users().messages().send(
            userId="me",
            body={"raw": raw},
        ).execute(),
        session=session,
    )

    if isinstance(send_result, dict) and "error" in send_result:
        return send_result

    return {
        "status": "sent",
        "gmail_message_id": send_result.get("id", ""),
        "thread_id": send_result.get("threadId", ""),
        "message": f"Email sent to {clean_to}.",
    }


async def archive_email(
    *,
    user: User,
    session: AsyncSession,
    message_id: str,
) -> dict:
    """Archive a Gmail message by removing the INBOX label."""
    clean_message_id = _clean_required_text(message_id, "message_id")

    credentials = build_google_credentials(
        access_token_encrypted=user.google_access_token_encrypted,
        refresh_token_encrypted=user.google_refresh_token_encrypted,
        token_expiry=user.google_token_expiry,
    )
    service = _build_gmail_service(credentials)

    result = await google_api_call(
        user=user,
        credentials=credentials,
        api_callable=lambda: service.users().messages().modify(
            userId="me",
            id=clean_message_id,
            body={"removeLabelIds": ["INBOX"]},
        ).execute(),
        session=session,
    )

    if isinstance(result, dict) and "error" in result:
        return result

    return {
        "status": "archived",
        "message_id": result.get("id", clean_message_id),
        "thread_id": result.get("threadId", ""),
    }


async def send_approved_reply(
    *,
    user: User,
    draft_id: int,
    session: AsyncSession,
) -> dict:
    """Send an approved email reply with duplicate prevention.

    The flow is:
    1. Lock the ``EmailDraftState`` row (``SELECT … FOR UPDATE``) to
       serialise concurrent send attempts for the same draft.
    2. Verify the draft is in ``approved`` status.
    3. Check the ``SentReply`` table — if a reply already exists for
       this user + thread, return ``already_sent`` without calling Gmail.
    4. Build the MIME message with proper threading headers.
    5. Call ``messages.send`` via the shared ``google_api_call`` wrapper.
    6. Insert a ``SentReply`` row (unique constraint catches races).
    7. Transition the draft to ``sent``.

    Parameters
    ----------
    user:
        The authenticated user with Gmail connected.
    draft_id:
        Primary key of the ``EmailDraftState`` row to send.
    session:
        Active DB session — the caller is responsible for committing
        or rolling back after this function returns.

    Returns
    -------
    A dict with ``"status"`` equal to ``"sent"``, ``"already_sent"``,
    or ``"error"`` (with a ``"message"`` key explaining the problem).
    """

    # ------------------------------------------------------------------
    # 1. Lock the draft row to prevent concurrent sends
    # ------------------------------------------------------------------
    stmt = (
        select(EmailDraftState)
        .where(EmailDraftState.id == draft_id)
        .with_for_update()
    )
    # Use session.execute() + scalars() instead of session.exec() because
    # session.exec() with with_for_update() may return a Row instead of the model.
    raw_result = await session.execute(stmt)
    draft: EmailDraftState | None = raw_result.scalars().first()

    if draft is None:
        return {"status": "error", "message": "Draft not found."}

    if draft.user_id != user.id:
        return {"status": "error", "message": "Draft does not belong to this user."}

    if draft.status == DraftStatus.SENT.value:
        return {"status": "already_sent", "message": "This draft has already been sent."}

    if draft.status != DraftStatus.APPROVED.value:
        return {
            "status": "error",
            "message": f"Draft is in '{draft.status}' state — only approved drafts can be sent.",
        }

    # ------------------------------------------------------------------
    # 2. Check SentReply table for existing send (thread-level dedup)
    # ------------------------------------------------------------------
    existing_stmt = select(SentReply).where(
        SentReply.user_id == user.id,
        SentReply.thread_id == draft.thread_id,
    )
    existing_raw = await session.execute(existing_stmt)
    existing_reply: SentReply | None = existing_raw.scalars().first()

    if existing_reply is not None:
        # A reply was already sent to this thread — mark draft as sent
        # (idempotent recovery if the draft status update failed last time).
        draft.status = DraftStatus.SENT.value
        draft.updated_at = datetime.now(timezone.utc)
        session.add(draft)
        await session.flush()
        return {
            "status": "already_sent",
            "gmail_message_id": existing_reply.gmail_message_id,
            "thread_id": draft.thread_id,
            "message": "A reply to this thread was already sent.",
        }

    # ------------------------------------------------------------------
    # 3. Build credentials and Gmail service
    # ------------------------------------------------------------------
    credentials = build_google_credentials(
        access_token_encrypted=user.google_access_token_encrypted,
        refresh_token_encrypted=user.google_refresh_token_encrypted,
        token_expiry=user.google_token_expiry,
    )
    service = _build_gmail_service(credentials)

    # ------------------------------------------------------------------
    # 4. Compose the MIME message
    # ------------------------------------------------------------------
    mime_msg = _compose_reply_message(
        recipient=draft.original_from,
        subject=draft.original_subject,
        reply_text=draft.draft_text,
        original_message_id=draft.original_message_id,
    )
    raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode("utf-8")

    # ------------------------------------------------------------------
    # 5. Send via Gmail API (through shared wrapper)
    # ------------------------------------------------------------------
    send_result = await google_api_call(
        user=user,
        credentials=credentials,
        api_callable=lambda: service.users().messages().send(
            userId="me",
            body={"raw": raw, "threadId": draft.thread_id},
        ).execute(),
        session=session,
    )

    # google_api_call returns a dict with "error" key on auth/API failure.
    if isinstance(send_result, dict) and "error" in send_result:
        return send_result

    gmail_message_id: str = send_result.get("id", "")

    # ------------------------------------------------------------------
    # 6. Record in SentReply (unique constraint guards against races)
    # ------------------------------------------------------------------
    sent_reply = SentReply(
        user_id=user.id,
        thread_id=draft.thread_id,
        gmail_message_id=gmail_message_id,
        reply_text=draft.draft_text,
    )
    session.add(sent_reply)

    try:
        await session.flush()
    except IntegrityError:
        # Another process inserted first — the email was already sent.
        await session.rollback()
        logger.warning(
            "SentReply race for user %s thread %s — duplicate prevented",
            user.id,
            draft.thread_id,
        )

        # The rollback discarded all pending changes including the FOR UPDATE
        # lock.  The email *was* sent via Gmail, so ensure the draft reflects
        # that — otherwise it stays as 'approved' and could be retried.
        try:
            draft_refresh = await session.get(EmailDraftState, draft_id)
            if draft_refresh and draft_refresh.status != DraftStatus.SENT.value:
                draft_refresh.status = DraftStatus.SENT.value
                draft_refresh.updated_at = datetime.now(timezone.utc)
                session.add(draft_refresh)
                await session.flush()
        except Exception:
            logger.exception(
                "Failed to mark draft %d as sent after SentReply race", draft_id
            )

        return {
            "status": "already_sent",
            "gmail_message_id": gmail_message_id,
            "thread_id": draft.thread_id,
            "message": "Reply was already sent (concurrent send detected).",
        }

    # ------------------------------------------------------------------
    # 7. Transition draft to "sent"
    # ------------------------------------------------------------------
    draft.status = DraftStatus.SENT.value
    draft.updated_at = datetime.now(timezone.utc)
    session.add(draft)
    await session.flush()

    logger.info(
        "Gmail reply sent for user %s, thread %s, gmail_id %s",
        user.id,
        draft.thread_id,
        gmail_message_id,
    )

    return {
        "status": "sent",
        "gmail_message_id": gmail_message_id,
        "thread_id": send_result.get("threadId", draft.thread_id),
        "message": f"Reply sent to {draft.original_from}.",
    }
