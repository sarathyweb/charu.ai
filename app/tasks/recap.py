"""Post-call recap tasks (morning/afternoon and evening).

Each task reads the structured call outcome from the CallLog, selects
the appropriate WhatsApp template based on confidence, and sends via
the OutboundMessage dedup flow.

Template selection logic (Property 8):
  - Morning/afternoon:
      call_outcome_confidence in ("clear", "partial") → daily_recap
      call_outcome_confidence == "none" or NULL        → daily_recap_no_goal
  - Evening:
      reflection_confidence in ("clear", "partial")    → evening_recap
      reflection_confidence == "none" or NULL           → evening_recap_no_accomplishments

Design references:
  - Design §5: WhatsApp Messaging (template list, dedup)
  - Research 19: WhatsApp Recap After Calls
  - Property 8: Recap template selection matches outcome confidence
  - Property 36: At-most-once outbound message via dedup key
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.celery_app import celery_app, run_async
from app.config import get_settings
from app.db import async_session_factory
from app.models.call_log import CallLog
from app.models.enums import CallType, OutcomeConfidence
from app.models.user import User
from app.services.outbound_message_service import (
    OutboundMessageService,
    evening_recap_dedup_key,
    recap_dedup_key,
)
from app.services.whatsapp_service import (
    WhatsAppService,
    build_daily_recap_no_goal_params,
    build_daily_recap_params,
    build_evening_recap_no_accomplishments_params,
    build_evening_recap_params,
)

logger = logging.getLogger(__name__)

# Twilio Content SID environment variable names — populated from settings
# or hardcoded after templates are created in Twilio console.
_TEMPLATE_CONTENT_SIDS: dict[str, str] = {}


def _get_content_sid(template_name: str) -> str:
    """Return the Twilio Content SID for *template_name*.

    Reads from the cached dict first, falling back to Settings attributes
    of the form ``TWILIO_CONTENT_SID_<TEMPLATE_NAME_UPPER>``.
    """
    if template_name in _TEMPLATE_CONTENT_SIDS:
        return _TEMPLATE_CONTENT_SIDS[template_name]

    settings = get_settings()
    attr = f"TWILIO_CONTENT_SID_{template_name.upper()}"
    sid = getattr(settings, attr, None)
    if sid:
        _TEMPLATE_CONTENT_SIDS[template_name] = sid
        return sid

    # Placeholder — callers should ensure content SIDs are configured.
    # Returning the template name makes it easy to spot misconfiguration
    # in logs without crashing the task.
    logger.warning(
        "No Twilio Content SID configured for template %r (expected settings.%s)",
        template_name,
        attr,
    )
    return f"MISSING_CONTENT_SID:{template_name}"


# ---------------------------------------------------------------------------
# Morning / afternoon recap
# ---------------------------------------------------------------------------


async def _run_send_post_call_recap(call_log_id: int) -> str:
    """Core async logic for the post-call recap (morning/afternoon).

    Steps:
      1. Load the CallLog and its User.
      2. Select template based on call_outcome_confidence.
      3. Build template parameters.
      4. Send via OutboundMessageService.send_template_dedup.
      5. On success, update CallLog.recap_sent_at (convenience denorm).
    """
    async with async_session_factory() as session:
        call_log = await session.get(CallLog, call_log_id)
        if call_log is None:
            return f"CallLog {call_log_id} not found"

        # Only process completed calls
        if call_log.status != "completed":
            return (
                f"CallLog {call_log_id} is not completed "
                f"(status={call_log.status}), skipping recap"
            )

        # Skip non-morning/afternoon call types — evening has its own task
        if call_log.call_type == CallType.EVENING.value:
            return (
                f"CallLog {call_log_id} is an evening call, "
                "use send_evening_recap instead"
            )

        user = await session.get(User, call_log.user_id)
        if user is None:
            return f"User {call_log.user_id} not found for CallLog {call_log_id}"

        user_name = user.name or "there"
        phone = user.phone  # E.164 format as stored

        # --- Template selection (Property 8) ---
        confidence = call_log.call_outcome_confidence
        if confidence in (
            OutcomeConfidence.CLEAR.value,
            OutcomeConfidence.PARTIAL.value,
        ):
            # daily_recap — has goal/next_action details
            date_str = call_log.call_date.strftime("%B %d, %Y")
            content_variables = build_daily_recap_params(
                user_name=user_name,
                goal=call_log.goal or "No specific goal today",
                next_action=call_log.next_action or "Take one small step",
                date_str=date_str,
            )
            template_name = "daily_recap"
        else:
            # daily_recap_no_goal — encouraging message, no goal details
            content_variables = build_daily_recap_no_goal_params(
                user_name=user_name,
            )
            template_name = "daily_recap_no_goal"

        content_sid = _get_content_sid(template_name)

        # --- Send via OutboundMessage dedup flow ---
        wa_service = WhatsAppService()
        outbound_svc = OutboundMessageService(
            session=session, whatsapp_service=wa_service
        )

        dedup = recap_dedup_key(call_log_id)

        sid = await outbound_svc.send_template_dedup(
            user_id=call_log.user_id,
            dedup_key=dedup,
            to=phone,
            content_sid=content_sid,
            content_variables=content_variables,
        )

        if sid is None:
            # Dedup hit or send failure — already logged by OutboundMessageService
            return f"Recap for CallLog {call_log_id}: dedup hit or send failed"

        # --- Convenience denormalization: stamp recap_sent_at ---
        call_log.recap_sent_at = datetime.now(timezone.utc)
        session.add(call_log)
        await session.commit()

    return f"Post-call recap sent for CallLog {call_log_id} (template={template_name})"


@celery_app.task(
    bind=True,
    name="app.tasks.recap.send_post_call_recap",
    max_retries=3,
    default_retry_delay=30,
)
def send_post_call_recap(self, call_log_id: int) -> str:
    """Send WhatsApp recap after a morning/afternoon call completes.

    Triggered as a delayed Celery task (~30 s after call end).
    Reads the structured call outcome from CallLog, selects a template
    based on ``call_outcome_confidence``, and sends via the
    OutboundMessage dedup flow.

    Requirements: 5.1, 5.2, 5.3, 5.5
    Property 8: Recap template selection matches outcome confidence
    """
    try:
        return run_async(_run_send_post_call_recap(call_log_id))
    except Exception as exc:
        logger.exception(
            "send_post_call_recap failed for call_log_id=%d", call_log_id
        )
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# Evening recap
# ---------------------------------------------------------------------------


async def _run_send_evening_recap(call_log_id: int) -> str:
    """Core async logic for the evening reflection recap.

    Same pattern as morning/afternoon but uses evening-specific fields:
      - reflection_confidence → template selection
      - accomplishments → template param
      - tomorrow_intention → template param
    """
    async with async_session_factory() as session:
        call_log = await session.get(CallLog, call_log_id)
        if call_log is None:
            return f"CallLog {call_log_id} not found"

        if call_log.status != "completed":
            return (
                f"CallLog {call_log_id} is not completed "
                f"(status={call_log.status}), skipping evening recap"
            )

        if call_log.call_type != CallType.EVENING.value:
            return (
                f"CallLog {call_log_id} is not an evening call "
                f"(call_type={call_log.call_type}), "
                "use send_post_call_recap instead"
            )

        user = await session.get(User, call_log.user_id)
        if user is None:
            return f"User {call_log.user_id} not found for CallLog {call_log_id}"

        user_name = user.name or "there"
        phone = user.phone

        # --- Template selection (Property 8 — evening variant) ---
        confidence = call_log.reflection_confidence
        if confidence in (
            OutcomeConfidence.CLEAR.value,
            OutcomeConfidence.PARTIAL.value,
        ):
            date_str = call_log.call_date.strftime("%B %d, %Y")
            content_variables = build_evening_recap_params(
                user_name=user_name,
                accomplishments=call_log.accomplishments or "Making it through the day",
                tomorrow_intention=call_log.tomorrow_intention or "Take it one step at a time",
                date_str=date_str,
            )
            template_name = "evening_recap"
        else:
            content_variables = build_evening_recap_no_accomplishments_params(
                user_name=user_name,
            )
            template_name = "evening_recap_no_accomplishments"

        content_sid = _get_content_sid(template_name)

        # --- Send via OutboundMessage dedup flow ---
        wa_service = WhatsAppService()
        outbound_svc = OutboundMessageService(
            session=session, whatsapp_service=wa_service
        )

        dedup = evening_recap_dedup_key(call_log_id)

        sid = await outbound_svc.send_template_dedup(
            user_id=call_log.user_id,
            dedup_key=dedup,
            to=phone,
            content_sid=content_sid,
            content_variables=content_variables,
        )

        if sid is None:
            return f"Evening recap for CallLog {call_log_id}: dedup hit or send failed"

        # --- Convenience denormalization ---
        call_log.recap_sent_at = datetime.now(timezone.utc)
        session.add(call_log)
        await session.commit()

    return f"Evening recap sent for CallLog {call_log_id} (template={template_name})"


@celery_app.task(
    bind=True,
    name="app.tasks.recap.send_evening_recap",
    max_retries=3,
    default_retry_delay=30,
)
def send_evening_recap(self, call_log_id: int) -> str:
    """Send WhatsApp recap after an evening reflection call completes.

    Triggered as a delayed Celery task (~30 s after call end).
    Reads the structured evening outcome from CallLog, selects a
    template based on ``reflection_confidence``, and sends via the
    OutboundMessage dedup flow.

    Requirements: 20.7
    Property 8: Recap template selection matches outcome confidence
    """
    try:
        return run_async(_run_send_evening_recap(call_log_id))
    except Exception as exc:
        logger.exception(
            "send_evening_recap failed for call_log_id=%d", call_log_id
        )
        raise self.retry(exc=exc)
