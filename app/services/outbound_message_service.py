"""OutboundMessageService — at-most-once dedup for proactive WhatsApp sends.

Pattern:
1. INSERT OutboundMessage with status=pending (ON CONFLICT DO NOTHING on dedup_key)
   - If a stale pending row exists (older than CLAIM_TTL_SECONDS), reclaim it
2. If insert/reclaim succeeded, call Twilio via WhatsAppService
3. On success: update status=sent with Twilio SID
4. On exception (ambiguous — Twilio may or may not have accepted):
   mark as failed (terminal).  This is the safe at-most-once choice because
   we cannot know whether the message was delivered.
5. For free-form sends only: if send_reply returns an empty list with no
   exception (definitive non-delivery), delete the pending row so a retry
   can re-claim.  Template sends never hit this path — send_template_message
   either returns a SID or raises.

The unique constraint on dedup_key ensures at most one message is sent per
logical event, even under concurrent Celery retries or duplicate task dispatch.

Stale claim reclaim: if a worker crashes after claiming (inserting a pending
row) but before marking sent or failed, the row would block all future
retries forever.  To handle this, _try_claim checks for pending rows older
than CLAIM_TTL_SECONDS and reclaims them via an atomic UPDATE.

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

# A pending claim older than this is considered abandoned (worker crash).
# Must be longer than the longest possible Twilio API round-trip.
CLAIM_TTL_SECONDS = 300  # 5 minutes


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

        Args:
            user_id: The user this message belongs to.
            dedup_key: Unique key for this logical send (e.g. "recap:{call_log_id}").
            to: Recipient phone in E.164 format.
            content_sid: Twilio Content SID for the template.
            content_variables: Template placeholder values.

        Returns:
            The Twilio message SID on success, or None if the message was
            already sent (dedup hit) or the send failed.
        """
        # Step 1: Try to claim the send slot via conflict-ignore insert
        claimed = await self._try_claim(user_id=user_id, dedup_key=dedup_key)
        if not claimed:
            logger.info(
                "Dedup hit for key=%s user=%s — skipping send", dedup_key, user_id
            )
            return None

        # Step 2: Send via Twilio
        try:
            sid = await self._wa.send_template_message(
                to=to,
                content_sid=content_sid,
                content_variables=content_variables,
            )
        except Exception:
            logger.exception(
                "Twilio send failed for dedup_key=%s user=%s", dedup_key, user_id
            )
            # Ambiguous: Twilio may have accepted the request before the
            # exception (e.g. timeout).  Mark as failed to prevent duplicates.
            await self._mark_failed(dedup_key=dedup_key)
            return None

        # Step 3: Mark as sent
        await self._mark_sent(dedup_key=dedup_key, twilio_message_sid=sid)
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

        claimed = await self._try_claim(user_id=user_id, dedup_key=dedup_key)
        if not claimed:
            logger.info(
                "Dedup hit for key=%s user=%s — skipping freeform send",
                dedup_key,
                user_id,
            )
            return None

        try:
            sids = await self._wa.send_reply(to=to, body=body)
        except WhatsAppPartialSendError as exc:
            # Some chunks were delivered — mark as sent with what we have.
            # Retrying would duplicate the already-sent chunks.
            logger.warning(
                "Partial freeform send for dedup_key=%s user=%s: %d/%d chunks",
                dedup_key,
                user_id,
                len(exc.sent_sids),
                exc.total_chunks,
            )
            await self._mark_sent(
                dedup_key=dedup_key, twilio_message_sid=exc.sent_sids[0]
            )
            return exc.sent_sids
        except Exception:
            logger.exception(
                "Freeform send failed for dedup_key=%s user=%s", dedup_key, user_id
            )
            # Ambiguous: Twilio may have accepted before the exception.
            # Mark as failed to prevent duplicates.
            await self._mark_failed(dedup_key=dedup_key)
            return None

        if not sids:
            # Definitive non-delivery (empty list, no exception).
            # Safe to release for retry.
            await self._release_claim(dedup_key=dedup_key)
            return None

        # Use the first SID as the representative
        await self._mark_sent(dedup_key=dedup_key, twilio_message_sid=sids[0])
        return sids

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _try_claim(self, *, user_id: int, dedup_key: str) -> bool:
        """Insert a pending OutboundMessage row.  Returns True if we won the slot.

        If a ``pending`` row already exists but is older than CLAIM_TTL_SECONDS,
        it is reclaimed (assumed to be from a crashed worker).  Rows in ``sent``
        status are never touched — the dedup key is permanently consumed.
        """
        # Fast path: try to insert a fresh row
        stmt = (
            pg_insert(OutboundMessage)
            .values(
                user_id=user_id,
                dedup_key=dedup_key,
                status=OutboundMessageStatus.PENDING.value,
            )
            .on_conflict_do_nothing(constraint="uq_outbound_message_dedup")
        )
        result = await self._session.exec(stmt)  # type: ignore[arg-type]
        await self._session.commit()
        if result.rowcount == 1:  # type: ignore[union-attr]
            return True

        # Conflict — a row exists.  Check if it's a stale pending claim.
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=CLAIM_TTL_SECONDS)
        reclaim_stmt = text(
            "UPDATE outbound_messages "
            "SET created_at = :now, status = :pending "
            "WHERE dedup_key = :key AND status = :pending AND created_at < :cutoff "
            "RETURNING id"
        )
        reclaim_result = await self._session.execute(
            reclaim_stmt,
            {
                "now": datetime.now(timezone.utc),
                "pending": OutboundMessageStatus.PENDING.value,
                "key": dedup_key,
                "cutoff": cutoff,
            },
        )
        await self._session.commit()
        reclaimed = reclaim_result.first() is not None
        if reclaimed:
            logger.info(
                "Reclaimed stale pending claim for dedup_key=%s", dedup_key
            )
        else:
            # Row exists and is either sent/failed or a fresh pending claim
            # from another worker — we lost the race.
            logger.info(
                "Dedup hit (non-stale) for key=%s user=%s — skipping",
                dedup_key,
                user_id,
            )
        return reclaimed

    async def _mark_sent(self, *, dedup_key: str, twilio_message_sid: str) -> None:
        """Update the OutboundMessage to sent status."""
        stmt = text(
            "UPDATE outbound_messages "
            "SET status = :status, twilio_message_sid = :sid, sent_at = :now "
            "WHERE dedup_key = :key AND status = :pending"
        )
        await self._session.execute(
            stmt,
            {
                "status": OutboundMessageStatus.SENT.value,
                "sid": twilio_message_sid,
                "now": datetime.now(timezone.utc),
                "key": dedup_key,
                "pending": OutboundMessageStatus.PENDING.value,
            },
        )
        await self._session.commit()

    async def _mark_failed(self, *, dedup_key: str) -> None:
        """Mark the OutboundMessage as permanently failed.

        Used when the Twilio call raised an exception — we cannot know
        whether the message was accepted, so the safe at-most-once choice
        is to consume the dedup key permanently.  A stale ``failed`` row
        will NOT be reclaimed by ``_try_claim`` (only ``pending`` rows are).
        """
        stmt = text(
            "UPDATE outbound_messages "
            "SET status = :status "
            "WHERE dedup_key = :key AND status = :pending"
        )
        await self._session.execute(
            stmt,
            {
                "status": OutboundMessageStatus.FAILED.value,
                "key": dedup_key,
                "pending": OutboundMessageStatus.PENDING.value,
            },
        )
        await self._session.commit()

    async def _release_claim(self, *, dedup_key: str) -> None:
        """Delete the pending OutboundMessage row so the dedup key can be
        retried by a future worker.

        Only used by the free-form send path when ``send_reply`` returns an
        empty list with no exception — a definitive signal that nothing was
        delivered.  Template sends never use this: ``send_template_message``
        either returns a SID or raises, so all template failures go through
        ``_mark_failed``.
        """
        await self._session.execute(
            text(
                "DELETE FROM outbound_messages "
                "WHERE dedup_key = :key AND status = :pending"
            ),
            {
                "key": dedup_key,
                "pending": OutboundMessageStatus.PENDING.value,
            },
        )
        await self._session.commit()


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
