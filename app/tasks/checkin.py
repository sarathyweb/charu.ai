"""Midday check-in Celery task.

Sends a WhatsApp check-in message ~4-5 hours after a completed morning
or afternoon call where a Goal and Next_Action were identified.

Template rotation: selects from ``midday_checkin``, ``midday_checkin_v2``,
``midday_checkin_v3`` — excluding ``User.last_checkin_template`` to avoid
consecutive repeats.  After a successful send the chosen template name is
persisted back to ``User.last_checkin_template``.

Design references:
  - Design §5: WhatsApp Messaging (template list, dedup)
  - Design §7: Anti-Habituation System (template rotation)
  - Research 27: Midday Check-In
  - Requirements 12, 13
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.celery_app import celery_app
from app.config import get_settings
from app.db import async_session_factory
from app.models.call_log import CallLog
from app.models.enums import CallType, OutcomeConfidence
from app.models.user import User
from app.services.outbound_message_service import (
    OutboundMessageService,
    checkin_dedup_key,
)
from app.services.scheduling_helpers import MIDDAY_CHECKIN_CUTOFF_HOUR
from app.services.whatsapp_service import (
    WhatsAppService,
    build_midday_checkin_params,
)

logger = logging.getLogger(__name__)

# All midday check-in template variants.
_CHECKIN_TEMPLATES: list[str] = [
    "midday_checkin",
    "midday_checkin_v2",
    "midday_checkin_v3",
]

# Twilio Content SID cache (same pattern as recap.py).
_TEMPLATE_CONTENT_SIDS: dict[str, str] = {}


def _get_content_sid(template_name: str) -> str:
    """Return the Twilio Content SID for *template_name*."""
    if template_name in _TEMPLATE_CONTENT_SIDS:
        return _TEMPLATE_CONTENT_SIDS[template_name]

    settings = get_settings()
    attr = f"TWILIO_CONTENT_SID_{template_name.upper()}"
    sid = getattr(settings, attr, None)
    if sid:
        _TEMPLATE_CONTENT_SIDS[template_name] = sid
        return sid

    logger.warning(
        "No Twilio Content SID configured for template %r (expected settings.%s)",
        template_name,
        attr,
    )
    return f"MISSING_CONTENT_SID:{template_name}"


def _select_checkin_template(last_template: str | None) -> str:
    """Pick a template variant, excluding *last_template* to avoid repeats."""
    candidates = [t for t in _CHECKIN_TEMPLATES if t != last_template]
    if not candidates:
        # Shouldn't happen with 3 templates, but be safe.
        candidates = _CHECKIN_TEMPLATES
    return random.choice(candidates)


# ---------------------------------------------------------------------------
# Core async logic
# ---------------------------------------------------------------------------


async def _run_send_midday_checkin(call_log_id: int) -> str:
    """Send a midday check-in WhatsApp message for *call_log_id*.

    Steps:
      1. Load CallLog and User.
      2. Validate: completed morning/afternoon call with a next_action.
      3. Double-check 6 PM local cutoff at execution time.
      4. Select template variant (anti-habituation rotation).
      5. Build template parameters.
      6. Send via OutboundMessageService.send_template_dedup.
      7. Persist chosen template to User.last_checkin_template.
      8. Stamp CallLog.checkin_sent_at (convenience denorm).
    """
    async with async_session_factory() as session:
        call_log = await session.get(CallLog, call_log_id)
        if call_log is None:
            return f"CallLog {call_log_id} not found"

        # Guard against Celery retry overwriting the audit timestamp.
        if call_log.checkin_sent_at is not None:
            return (
                f"CallLog {call_log_id} already has checkin_sent_at="
                f"{call_log.checkin_sent_at}, skipping duplicate check-in"
            )

        # Only completed calls produce check-ins.
        if call_log.status != "completed":
            return (
                f"CallLog {call_log_id} is not completed "
                f"(status={call_log.status}), skipping check-in"
            )

        # Only morning/afternoon calls trigger midday check-ins.
        if call_log.call_type not in (
            CallType.MORNING.value,
            CallType.AFTERNOON.value,
        ):
            return (
                f"CallLog {call_log_id} is {call_log.call_type}, "
                "midday check-in only applies to morning/afternoon calls"
            )

        # Must have a next_action to check in about.
        confidence = call_log.call_outcome_confidence
        if (
            confidence not in (
                OutcomeConfidence.CLEAR.value,
                OutcomeConfidence.PARTIAL.value,
            )
            or not call_log.next_action
        ):
            return (
                f"CallLog {call_log_id} has no actionable outcome "
                f"(confidence={confidence}, next_action={call_log.next_action!r}), "
                "skipping check-in"
            )

        user = await session.get(User, call_log.user_id)
        if user is None:
            return f"User {call_log.user_id} not found for CallLog {call_log_id}"

        if not user.timezone:
            return f"User {call_log.user_id} has no timezone, skipping check-in"

        # --- 6 PM cutoff double-check at execution time ---
        # Worker delay or clock drift may push execution past the cutoff
        # that was valid when the task was scheduled.
        tz = ZoneInfo(user.timezone)
        now_local = datetime.now(timezone.utc).astimezone(tz)
        if now_local.hour >= MIDDAY_CHECKIN_CUTOFF_HOUR:
            return (
                f"Past {MIDDAY_CHECKIN_CUTOFF_HOUR}:00 local "
                f"({now_local.strftime('%H:%M')}) for user {user.id}, "
                "skipping check-in"
            )

        # --- Template selection (anti-habituation rotation) ---
        template_name = _select_checkin_template(user.last_checkin_template)
        content_sid = _get_content_sid(template_name)

        # --- Build template parameters ---
        user_name = user.name or "there"
        content_variables = build_midday_checkin_params(
            user_name=user_name,
            next_action=call_log.next_action,
        )

        # --- Send via OutboundMessage dedup flow ---
        wa_service = WhatsAppService()
        outbound_svc = OutboundMessageService(
            session=session, whatsapp_service=wa_service,
        )

        dedup = checkin_dedup_key(call_log_id)

        sid = await outbound_svc.send_template_dedup(
            user_id=call_log.user_id,
            dedup_key=dedup,
            to=user.phone,
            content_sid=content_sid,
            content_variables=content_variables,
        )

        if sid is None:
            return (
                f"Midday check-in for CallLog {call_log_id}: "
                "dedup hit or send failed"
            )

        # --- Persist template choice for anti-habituation ---
        user.last_checkin_template = template_name
        session.add(user)

        # --- Convenience denormalization ---
        call_log.checkin_sent_at = datetime.now(timezone.utc)
        session.add(call_log)

        await session.commit()

    return (
        f"Midday check-in sent for CallLog {call_log_id} "
        f"(template={template_name})"
    )


# ---------------------------------------------------------------------------
# Celery task entry point
# ---------------------------------------------------------------------------


@celery_app.task(
    bind=True,
    name="app.tasks.checkin.send_midday_checkin",
    max_retries=2,
    default_retry_delay=60,
)
def send_midday_checkin(self, call_log_id: int) -> str:
    """Send a midday check-in WhatsApp message.

    Triggered as a delayed Celery task (~4-5 hours after morning call).
    Reads the structured call outcome from CallLog, selects a check-in
    template variant (rotating to avoid consecutive repeats), and sends
    via the OutboundMessage dedup flow.

    Requirements: 13, 12
    """
    try:
        return asyncio.run(_run_send_midday_checkin(call_log_id))
    except Exception as exc:
        logger.exception(
            "send_midday_checkin failed for call_log_id=%d", call_log_id,
        )
        raise self.retry(exc=exc)
