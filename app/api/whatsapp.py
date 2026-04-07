"""POST /webhook/whatsapp — Twilio WhatsApp webhook."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth.twilio import verify_twilio_signature
from app.db import async_session_factory
from app.models.processed_message import ProcessedMessage
from app.models.user import User
from app.services.agent_service import AgentService
from app.services.checkin_context import (
    build_checkin_reply_prefix,
    find_pending_checkin,
    mark_checkin_replied,
)
from app.services.draft_context import (
    DraftContext,
    DraftIntent,
    classify_draft_intent,
    find_pending_draft,
)
from app.services.email_draft_service import EmailDraftService
from app.services.user_service import UserService
from app.services.whatsapp_service import WhatsAppService
from app.utils import normalize_phone

logger = logging.getLogger(__name__)

router = APIRouter()


async def _get_db_session():
    """Yield an async DB session (local helper until task 10.1 lands)."""
    async with async_session_factory() as session:
        yield session


# ---------------------------------------------------------------------------
# Email draft approval helpers
# ---------------------------------------------------------------------------


async def _handle_draft_approve(
    draft_ctx: DraftContext,
    user: User,
    phone: str,
    session: AsyncSession,
    wa_service: WhatsAppService,
) -> Response:
    """Approve a pending draft and send the email via Gmail."""
    svc = EmailDraftService(session)
    try:
        result = await svc.approve_draft(draft_ctx.draft_id, user)
        if "error" in result:
            # Auth/quota errors from google_api_call — surface the message
            # directly so the user knows what to do (e.g. reconnect Google).
            reply = result.get("message", "Something went wrong sending that email.")
        elif result.get("status") == "sent":
            reply = (
                f"✅ Done — your reply to {draft_ctx.original_from} "
                f"re: {draft_ctx.original_subject} has been sent."
            )
        elif result.get("status") == "already_sent":
            reply = "That reply was already sent — you're all good."
        else:
            reply = f"Something unexpected happened (status: {result.get('status', 'unknown')}). Please try again."
    except ValueError as exc:
        reply = f"Couldn't send: {exc}"
    except Exception:
        logger.exception("Failed to approve draft %d for user %s", draft_ctx.draft_id, phone)
        reply = "Something went wrong sending that email. Please try again."

    try:
        await wa_service.send_reply(to=phone, body=reply)
    except Exception:
        logger.exception("Failed to send draft approval reply to %s", phone)

    return Response(status_code=200)


async def _handle_draft_abandon(
    draft_ctx: DraftContext,
    phone: str,
    session: AsyncSession,
    wa_service: WhatsAppService,
) -> Response:
    """Abandon a pending draft."""
    svc = EmailDraftService(session)
    try:
        await svc.abandon_draft(draft_ctx.draft_id)
        reply = (
            f"Got it — I've dropped the draft reply to {draft_ctx.original_from}. "
            "No email was sent."
        )
    except ValueError as exc:
        reply = f"Couldn't abandon: {exc}"
    except Exception:
        logger.exception("Failed to abandon draft %d for user %s", draft_ctx.draft_id, phone)
        reply = "Something went wrong. The draft may have already expired."

    try:
        await wa_service.send_reply(to=phone, body=reply)
    except Exception:
        logger.exception("Failed to send draft abandon reply to %s", phone)

    return Response(status_code=200)


async def _handle_draft_revise(
    draft_ctx: DraftContext,
    body: str,
    user: User,
    phone: str,
    request: Request,
    session: AsyncSession,
    wa_service: WhatsAppService,
) -> Response:
    """Route a revision request through the agent, then re-present the updated draft."""
    # Build context prefix so the agent knows this is a draft revision
    prefix = (
        f"[SYSTEM: The user is requesting changes to an email draft "
        f"(draft_id={draft_ctx.draft_id}). "
        f"The draft is a reply to {draft_ctx.original_from} "
        f"re: {draft_ctx.original_subject}. "
        f"Current draft text:\n{draft_ctx.draft_text}\n\n"
        f"The user's revision request follows. "
        f"Generate the revised draft and call update_email_draft with "
        f"draft_id={draft_ctx.draft_id} and the new text. "
        f"Then present the updated draft to the user and ask for approval.]"
    )
    agent_message = f"{prefix}\n\n{body}"

    agent_service = AgentService(
        runner=request.app.state.runner,
        session_service=request.app.state.session_service,
        session=session,
    )
    result = await agent_service.run(
        user_id=phone, message=agent_message, channel="whatsapp"
    )

    if result.reply and result.reply.strip():
        try:
            await wa_service.send_reply(to=phone, body=result.reply)
        except Exception:
            logger.exception("Failed to send draft revision reply to %s", phone)
    else:
        logger.warning(
            "Agent returned empty reply for draft revision from %s", phone
        )

    return Response(status_code=200)


@router.post("/webhook/whatsapp")
async def whatsapp_webhook(
    request: Request,
    form_data: dict = Depends(verify_twilio_signature),
    session: AsyncSession = Depends(_get_db_session),
) -> Response:
    """Receive an inbound WhatsApp message, run it through the agent, and reply.

    Auth is handled by the ``verify_twilio_signature`` dependency which
    raises HTTP 403 on invalid signatures.  All other errors are caught
    and swallowed (returning 200) to prevent Twilio retries.
    """
    try:
        # --- Extract fields ---------------------------------------------------
        raw_from = form_data.get("From", "")  # "whatsapp:+971501234567"
        body = form_data.get("Body", "").strip()
        message_sid = form_data.get("MessageSid", "")

        # Strip the "whatsapp:" prefix and normalise to E.164
        phone = normalize_phone(raw_from.removeprefix("whatsapp:"))

        logger.info(
            "WhatsApp inbound from=%s body_len=%d sid=%s",
            phone,
            len(body),
            message_sid,
        )

        # Empty body → nothing to process
        if not body:
            logger.info("Empty body from %s — skipping", phone)
            return Response(status_code=200)

        # --- Idempotency check (insert-first to prevent race) ----------------
        if message_sid:
            try:
                session.add(
                    ProcessedMessage(
                        message_sid=message_sid,
                        processed_at=datetime.now(timezone.utc),
                    )
                )
                await session.commit()
            except Exception:
                # PK conflict → another request already claimed this sid
                await session.rollback()
                logger.info("Duplicate MessageSid %s — skipping", message_sid)
                return Response(status_code=200)

        # --- User resolution --------------------------------------------------
        user_service = UserService(session)
        user = await user_service.ensure_from_whatsapp(phone)

        # --- Check-in reply detection -----------------------------------------
        # If the user has a recent midday check-in, prepend context so the
        # agent knows this message is a response to the check-in and can
        # follow the Midday Check-In Response Guidelines.
        agent_message = body
        is_checkin_reply = False
        if user.id is not None:
            checkin_ctx = await find_pending_checkin(user.id, session)
            if checkin_ctx is not None:
                prefix = build_checkin_reply_prefix(checkin_ctx)
                agent_message = f"{prefix}\n\n{body}"
                await mark_checkin_replied(checkin_ctx.call_log_id, session)
                is_checkin_reply = True
                logger.info(
                    "Check-in reply detected for user %s (call_log_id=%d)",
                    phone,
                    checkin_ctx.call_log_id,
                )

        # --- Email draft approval detection -----------------------------------
        # If the user has a pending email draft, classify their intent
        # (approve / revise / abandon) and handle directly or via agent.
        # Draft handling takes priority over normal agent invocation when
        # a draft is pending — UNLESS a check-in reply was detected, in
        # which case the message should reach the agent with check-in context.
        if user.id is not None and not is_checkin_reply:
            draft_ctx = await find_pending_draft(user.id, session)
            if draft_ctx is not None:
                intent = classify_draft_intent(body)
                logger.info(
                    "Draft reply detected for user %s (draft_id=%d, intent=%s)",
                    phone,
                    draft_ctx.draft_id,
                    intent.value,
                )
                wa_service = WhatsAppService()
                if intent == DraftIntent.APPROVE:
                    return await _handle_draft_approve(
                        draft_ctx, user, phone, session, wa_service,
                    )
                elif intent == DraftIntent.ABANDON:
                    return await _handle_draft_abandon(
                        draft_ctx, phone, session, wa_service,
                    )
                else:
                    # Revision — route through agent with draft context
                    return await _handle_draft_revise(
                        draft_ctx, body, user, phone, request, session,
                        wa_service,
                    )

        # --- Agent invocation -------------------------------------------------
        agent_service = AgentService(
            runner=request.app.state.runner,
            session_service=request.app.state.session_service,
            session=session,
        )
        result = await agent_service.run(
            user_id=phone, message=agent_message, channel="whatsapp"
        )

        # --- Send reply -------------------------------------------------------
        wa_service = WhatsAppService()
        if result.reply and result.reply.strip():
            logger.info(
                "WhatsApp reply to %s (session %s): %d chars",
                phone,
                result.session_id,
                len(result.reply),
            )
            try:
                await wa_service.send_reply(to=phone, body=result.reply)
            except Exception:
                logger.exception(
                    "Failed to send WhatsApp reply to %s", phone
                )
        else:
            logger.warning(
                "Agent returned empty reply for %s (session %s, message: %.100s)",
                phone,
                result.session_id,
                body,
            )

        return Response(status_code=200)

    except HTTPException:
        raise  # Let 403 (signature) propagate
    except Exception:
        logger.exception("WhatsApp webhook error")
        return Response(status_code=200)
