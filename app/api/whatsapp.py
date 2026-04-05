"""POST /webhook/whatsapp — Twilio WhatsApp webhook."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth.twilio import verify_twilio_signature
from app.db import async_session_factory
from app.models.processed_message import ProcessedMessage
from app.services.agent_service import AgentService
from app.services.user_service import UserService
from app.services.whatsapp_service import WhatsAppService
from app.utils import normalize_phone

logger = logging.getLogger(__name__)

router = APIRouter()


async def _get_db_session():
    """Yield an async DB session (local helper until task 10.1 lands)."""
    async with async_session_factory() as session:
        yield session


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
        await user_service.ensure_from_whatsapp(phone)

        # --- Agent invocation -------------------------------------------------
        agent_service = AgentService(
            runner=request.app.state.runner,
            session_service=request.app.state.session_service,
            session=session,
        )
        result = await agent_service.run(
            user_id=phone, message=body, channel="whatsapp"
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
            await wa_service.send_reply(to=phone, body=result.reply)
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
