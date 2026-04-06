"""OutboundMessageService — at-most-once dedup for proactive WhatsApp sends.

Pattern:
1. INSERT OutboundMessage with status=pending (ON CONFLICT DO NOTHING on dedup_key)
   - If a stale row exists (pending or sending, older than CLAIM_TTL_SECONDS),
     reclaim it — the original worker is assumed crashed.
2. Atomically transition pending → sending (the "send lock").  This is a
   compare-and-swap gated on claim_token, so only the worker that owns the
   claim can enter the sending state.  While a row is in ``sending``, no
   other worker can reclaim it (reclaim only targets rows older than the TTL).
3. Call Twilio via WhatsAppService.
4. On success: update status=sent with Twilio SID.
5. On definitive rejection (TwilioRestException with 4xx status):
   release the claim (delete the row) so a retry with corrected
   config/template can succeed.  The message was never queued for delivery.
6. On ambiguous failure (5xx, timeout, connection error):
   mark as failed (terminal).  This is the safe at-most-once choice because
   we cannot know whether the message was delivered.
7. For free-form sends only: if send_reply returns an empty list with no
   exception (definitive non-delivery), delete the row so a retry can
   re-claim.

The unique constraint on dedup_key ensures at most one message is sent per
logical event, even under concurrent Celery retries or duplicate task dispatch.

Ownership fencing via claim_token + sending status:
Every claim writes a random UUID into ``claim_token``.  Before calling Twilio,
the worker atomically transitions pending → sending with a WHERE clause that
checks both ``status = 'pending'`` and ``claim_token = :token``.  If another
worker reclaimed the row (writing a new token), this CAS fails and the stale
worker never calls Twilio.  The ``sending`` status also blocks reclaim: only
rows older than CLAIM_TTL_SECONDS in pending *or* sending state are eligible
for reclaim, so a worker actively calling Twilio (fresh ``sending`` row) is
protected from being reclaimed.

Stale claim reclaim: if a worker crashes after claiming but before marking
sent or failed, the row would block all future retries forever.  To handle
this, _try_claim checks for rows in pending/sending state older than
CLAIM_TTL_SECONDS and reclaims them via an atomic UPDATE.

Dedup key conventions (used by Celery tasks):
- Recap:          "recap:{call_log_id}"
- Evening recap:  "evening_recap:{call_log_id}"
- Midday checkin: "checkin:{call_log_id}"
- Weekly summary: "weekly:{user_id}:{iso_week}"  (e.g. "weekly:42:2026-W15")
- Draft review:   "draft_review:{draft_id}"
- Missed call:    "missed:{call_log_id}"
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.enums import OutboundMessageStatus
from app.models.outbound_message import OutboundMessage

if TYPE_CHECKING:
    from app.services.whatsapp_service import WhatsAppService

logger = logging.getLogger(__name__)

# A pending/sending claim older than this is considered abandoned.
# Must be longer than the longest possible Twilio API round-trip.
CLAIM_TTL_SECONDS = 300  # 5 minutes


def _is_definitive_rejection(exc: Exception) -> bool:
    """Return True if the exception proves Twilio never accepted the message.

    Definitive rejections (4xx) mean the request was invalid — bad template
    SID, malformed number, etc.  The message was never queued for delivery,
    so it is safe to release the dedup claim for a future retry with
    corrected parameters.

    Ambiguous failures (5xx, timeouts, connection errors) mean Twilio *may*
    have accepted the request before the error surfaced.  These must remain
    marked as failed to preserve at-most-once semantics.
    """
    try:
        from twilio.base.exceptions import TwilioRestException
    except ImportError:  # pragma: no cover — test environments may mock Twilio
        return False

    if isinstance(exc, TwilioRestException):
        return 400 <= exc.status < 500
    return False


class OutboundMessageService:
    """Wraps WhatsApp sends with DB-backed at-most-once dedup."""

    def __init__(
        self,
        session: AsyncSession,
        whatsapp_service: WhatsAppService,
    ) -> None:
        self._session = session
        self._wa = whatsapp_service

    # ------------------------------------------------------------------
    # Core dedup send
    # ------------------------------------------------------------------

    async def send_template_dedup(
        self,
        *,
        user_id: int,
        dedup_key: str,
        to: str,
        content_sid: str,
        content_variables: dict[str, str] | None = None,
    ) -> str | None:
        """Send a template message with at-most-once dedup guarantee.

        Returns:
            The Twilio message SID on success, or None if the message was
            already sent (dedup hit) or the send failed.
        """
        # Step 1: Claim the dedup slot (insert pending row)
        token = _new_token()
        claimed = await self._try_claim(
            user_id=user_id, dedup_key=dedup_key, token=token
        )
        if not claimed:
            logger.info(
                "Dedup hit for key=%s user=%s — skipping send", dedup_key, user_id
            )
            return None

        # Step 2: Acquire send lock (pending → sending, gated on token)
        locked = await self._acquire_send_lock(dedup_key=dedup_key, token=token)
        if not locked:
            logger.warning(
                "Lost ownership before Twilio call for dedup_key=%s user=%s — "
                "another worker reclaimed the row between claim and send lock.",
                dedup_key,
                user_id,
            )
            return None

        # Step 3: Call Twilio (row is in 'sending' state — safe from reclaim)
        try:
            sid = await self._wa.send_template_message(
                to=to,
                content_sid=content_sid,
                content_variables=content_variables,
            )
        except Exception as exc:
            if _is_definitive_rejection(exc):
                logger.warning(
                    "Twilio definitive rejection for dedup_key=%s user=%s: %s",
                    dedup_key,
                    user_id,
                    exc,
                )
                await self._release_claim(dedup_key=dedup_key, token=token)
            else:
                logger.exception(
                    "Twilio send failed (ambiguous) for dedup_key=%s user=%s",
                    dedup_key,
                    user_id,
                )
                await self._mark_failed(dedup_key=dedup_key, token=token)
            return None

        # Step 4: Mark as sent (ownership-fenced)
        marked = await self._mark_sent(
            dedup_key=dedup_key, twilio_message_sid=sid, token=token
        )
        if not marked:
            logger.warning(
                "Ownership lost after Twilio call for dedup_key=%s — "
                "another worker reclaimed the row. Twilio SID %s may be orphaned.",
                dedup_key,
                sid,
            )
            return None
        return sid

    async def send_freeform_dedup(
        self,
        *,
        user_id: int,
        dedup_key: str,
        to: str,
        body: str,
    ) -> list[str] | None:
        """Send a free-form message with at-most-once dedup guarantee.

        Returns:
            List of Twilio message SIDs on success, or None if dedup hit / failure.
        """
        from app.services.whatsapp_service import WhatsAppPartialSendError

        token = _new_token()
        claimed = await self._try_claim(
            user_id=user_id, dedup_key=dedup_key, token=token
        )
        if not claimed:
            logger.info(
                "Dedup hit for key=%s user=%s — skipping freeform send",
                dedup_key,
                user_id,
            )
            return None

        # Acquire send lock (pending → sending)
        locked = await self._acquire_send_lock(dedup_key=dedup_key, token=token)
        if not locked:
            logger.warning(
                "Lost ownership before freeform Twilio call for dedup_key=%s",
                dedup_key,
            )
            return None

        try:
            sids = await self._wa.send_reply(to=to, body=body)
        except WhatsAppPartialSendError as exc:
            logger.warning(
                "Partial freeform send for dedup_key=%s user=%s: %d/%d chunks",
                dedup_key,
                user_id,
                len(exc.sent_sids),
                exc.total_chunks,
            )
            await self._mark_sent(
                dedup_key=dedup_key,
                twilio_message_sid=exc.sent_sids[0],
                token=token,
            )
            return exc.sent_sids
        except Exception as exc:
            if _is_definitive_rejection(exc):
                logger.warning(
                    "Freeform send definitively rejected for dedup_key=%s user=%s: %s",
                    dedup_key,
                    user_id,
                    exc,
                )
                await self._release_claim(dedup_key=dedup_key, token=token)
            else:
                logger.exception(
                    "Freeform send failed (ambiguous) for dedup_key=%s user=%s",
                    dedup_key,
                    user_id,
                )
                await self._mark_failed(dedup_key=dedup_key, token=token)
            return None

        if not sids:
            await self._release_claim(dedup_key=dedup_key, token=token)
            return None

        marked = await self._mark_sent(
            dedup_key=dedup_key, twilio_message_sid=sids[0], token=token
        )
        if not marked:
            logger.warning(
                "Ownership lost after freeform Twilio call for dedup_key=%s — "
                "another worker reclaimed the row.",
                dedup_key,
            )
            return None
        return sids

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _try_claim(
        self, *, user_id: int, dedup_key: str, token: str
    ) -> bool:
        """Insert a pending OutboundMessage row.  Returns True if we won the slot.

        If a row already exists in ``pending`` or ``sending`` state and is
        older than CLAIM_TTL_SECONDS, it is reclaimed (assumed to be from a
        crashed worker) and a *new* ``claim_token`` is written — fencing out
        the original holder.  The reclaimed row is reset to ``pending``.

        Rows in ``sent`` or ``failed`` status are never touched.
        """
        # Fast path: try to insert a fresh row with our token
        stmt = (
            pg_insert(OutboundMessage)
            .values(
                user_id=user_id,
                dedup_key=dedup_key,
                status=OutboundMessageStatus.PENDING.value,
                claim_token=token,
            )
            .on_conflict_do_nothing(constraint="uq_outbound_message_dedup")
        )
        result = await self._session.exec(stmt)  # type: ignore[arg-type]
        await self._session.commit()
        if result.rowcount == 1:  # type: ignore[union-attr]
            return True

        # Conflict — a row exists.  Check if it's a stale claim.
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=CLAIM_TTL_SECONDS)
        reclaim_stmt = text(
            "UPDATE outbound_messages "
            "SET created_at = :now, status = :pending, claim_token = :token "
            "WHERE dedup_key = :key "
            "  AND status IN (:pending, :sending) "
            "  AND created_at < :cutoff "
            "RETURNING id"
        )
        reclaim_result = await self._session.execute(
            reclaim_stmt,
            {
                "now": datetime.now(timezone.utc),
                "pending": OutboundMessageStatus.PENDING.value,
                "sending": OutboundMessageStatus.SENDING.value,
                "key": dedup_key,
                "cutoff": cutoff,
                "token": token,
            },
        )
        await self._session.commit()
        reclaimed = reclaim_result.first() is not None
        if reclaimed:
            logger.info(
                "Reclaimed stale claim for dedup_key=%s", dedup_key
            )
        else:
            logger.info(
                "Dedup hit (non-stale) for key=%s user=%s — skipping",
                dedup_key,
                user_id,
            )
        return reclaimed

    async def _acquire_send_lock(
        self, *, dedup_key: str, token: str
    ) -> bool:
        """Atomically transition pending → sending, gated on claim_token.

        This is the pre-flight gate before calling Twilio.  If another worker
        reclaimed the row (writing a different token), this CAS fails and we
        return False — the caller must NOT proceed to call Twilio.

        While the row is in ``sending`` status with a fresh ``created_at``,
        it is protected from reclaim (reclaim only targets rows older than
        CLAIM_TTL_SECONDS).
        """
        stmt = text(
            "UPDATE outbound_messages "
            "SET status = :sending "
            "WHERE dedup_key = :key "
            "  AND status = :pending "
            "  AND claim_token = :token "
            "RETURNING id"
        )
        result = await self._session.execute(
            stmt,
            {
                "sending": OutboundMessageStatus.SENDING.value,
                "pending": OutboundMessageStatus.PENDING.value,
                "key": dedup_key,
                "token": token,
            },
        )
        await self._session.commit()
        return result.first() is not None

    async def _mark_sent(
        self, *, dedup_key: str, twilio_message_sid: str, token: str
    ) -> bool:
        """Update the OutboundMessage to sent status.

        Returns True if the row was updated (we still own it), False if
        another worker reclaimed the row (token mismatch → 0 rows updated).
        """
        stmt = text(
            "UPDATE outbound_messages "
            "SET status = :status, twilio_message_sid = :sid, sent_at = :now "
            "WHERE dedup_key = :key "
            "  AND status = :sending "
            "  AND claim_token = :token"
        )
        result = await self._session.execute(
            stmt,
            {
                "status": OutboundMessageStatus.SENT.value,
                "sid": twilio_message_sid,
                "now": datetime.now(timezone.utc),
                "key": dedup_key,
                "sending": OutboundMessageStatus.SENDING.value,
                "token": token,
            },
        )
        await self._session.commit()
        return result.rowcount > 0  # type: ignore[union-attr]

    async def _mark_failed(self, *, dedup_key: str, token: str) -> None:
        """Mark the OutboundMessage as permanently failed.

        Used when the Twilio call raised an ambiguous exception.  The token
        fence ensures we don't overwrite a row that was already reclaimed
        and sent by another worker.
        """
        stmt = text(
            "UPDATE outbound_messages "
            "SET status = :status "
            "WHERE dedup_key = :key "
            "  AND status = :sending "
            "  AND claim_token = :token"
        )
        await self._session.execute(
            stmt,
            {
                "status": OutboundMessageStatus.FAILED.value,
                "key": dedup_key,
                "sending": OutboundMessageStatus.SENDING.value,
                "token": token,
            },
        )
        await self._session.commit()

    async def _release_claim(self, *, dedup_key: str, token: str) -> None:
        """Delete the OutboundMessage row so the dedup key can be retried.

        Used for definitive rejections (4xx) and free-form zero-chunk sends.
        Token-fenced: only deletes if we still own the claim.
        Matches both pending and sending status since release can happen
        from either state (e.g. 4xx during sending, or zero-chunks).
        """
        await self._session.execute(
            text(
                "DELETE FROM outbound_messages "
                "WHERE dedup_key = :key "
                "  AND status IN (:pending, :sending) "
                "  AND claim_token = :token"
            ),
            {
                "key": dedup_key,
                "pending": OutboundMessageStatus.PENDING.value,
                "sending": OutboundMessageStatus.SENDING.value,
                "token": token,
            },
        )
        await self._session.commit()


def _new_token() -> str:
    """Generate a random claim token (UUID4 hex, no dashes)."""
    return uuid.uuid4().hex


# ======================================================================
# Dedup key builders — importable by Celery tasks
# ======================================================================


def recap_dedup_key(call_log_id: int) -> str:
    """Dedup key for post-call recap (morning/afternoon)."""
    return f"recap:{call_log_id}"


def evening_recap_dedup_key(call_log_id: int) -> str:
    """Dedup key for evening reflection recap."""
    return f"evening_recap:{call_log_id}"


def checkin_dedup_key(call_log_id: int) -> str:
    """Dedup key for midday check-in."""
    return f"checkin:{call_log_id}"


def weekly_summary_dedup_key(user_id: int, iso_week: str) -> str:
    """Dedup key for weekly summary. *iso_week* is e.g. '2026-W15'."""
    return f"weekly:{user_id}:{iso_week}"


def draft_review_dedup_key(draft_id: int) -> str:
    """Dedup key for email draft review notification."""
    return f"draft_review:{draft_id}"


def missed_call_dedup_key(call_log_id: int) -> str:
    """Dedup key for missed-call encouragement."""
    return f"missed:{call_log_id}"
