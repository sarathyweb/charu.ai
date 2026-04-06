"""Weekly summary Celery task.

Hourly sweep: checks which users' local time is Sunday 5 PM and sends
a weekly summary WhatsApp message via the OutboundMessage dedup flow.

The summary includes:
- Number of completed calls for the week (Mon–Sun)
- Number of goals set (calls with clear/partial outcome confidence)
- A closing message based on performance

Design references:
  - Design §5: WhatsApp Messaging (template list, dedup)
  - Requirements 5.6
  - Research 19: WhatsApp Recap After Calls (weekly summary section)
  - Research 37: Call State Tracking (weekly summary query)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlmodel import col, select

from app.celery_app import celery_app
from app.config import get_settings
from app.db import async_session_factory
from app.models.call_log import CallLog
from app.models.enums import CallLogStatus, OutcomeConfidence
from app.models.user import User
from app.services.outbound_message_service import (
    OutboundMessageService,
    weekly_summary_dedup_key,
)
from app.services.whatsapp_service import WhatsAppService, build_weekly_summary_params

logger = logging.getLogger(__name__)

# Twilio Content SID cache (same pattern as recap.py / checkin.py).
_CONTENT_SID: str | None = None


def _get_content_sid() -> str:
    """Return the Twilio Content SID for the ``weekly_summary`` template."""
    global _CONTENT_SID  # noqa: PLW0603
    if _CONTENT_SID is not None:
        return _CONTENT_SID

    settings = get_settings()
    sid = getattr(settings, "TWILIO_CONTENT_SID_WEEKLY_SUMMARY", None)
    if sid:
        _CONTENT_SID = sid
        return sid

    logger.warning(
        "No Twilio Content SID configured for weekly_summary "
        "(expected settings.TWILIO_CONTENT_SID_WEEKLY_SUMMARY)"
    )
    return "MISSING_CONTENT_SID:weekly_summary"


def _closing_message(calls_answered: int) -> str:
    """Generate a closing message based on weekly call count."""
    if calls_answered >= 5:
        return "Great consistency this week!"
    if calls_answered >= 3:
        return "Solid effort — keep building the habit."
    if calls_answered >= 1:
        return "Every call counts. Let's build on this."
    return "New week, fresh start. We're here when you're ready."


# ---------------------------------------------------------------------------
# Core async logic — send summary for a single user
# ---------------------------------------------------------------------------


async def _run_send_weekly_summary(user_id: int) -> str:
    """Build and send the weekly summary for *user_id*.

    Steps:
      1. Load User, validate timezone.
      2. Compute the week range (Mon–Sun) in the user's local timezone.
      3. Query completed CallLog entries for the week.
      4. Count goals set (clear/partial outcome confidence).
      5. Build template parameters.
      6. Send via OutboundMessageService.send_template_dedup.
      7. Stamp User.last_weekly_summary_sent_at.
    """
    async with async_session_factory() as session:
        user = await session.get(User, user_id)
        if user is None:
            return f"User {user_id} not found"

        if not user.timezone:
            return f"User {user_id} has no timezone, skipping weekly summary"

        tz = ZoneInfo(user.timezone)
        now_local = datetime.now(timezone.utc).astimezone(tz)

        # Week range: Monday through Sunday (today is Sunday).
        week_end = now_local.date()
        week_start = week_end - timedelta(days=6)

        # Query completed calls for the week using call_date (user-local date).
        stmt = select(CallLog).where(
            CallLog.user_id == user_id,
            col(CallLog.call_date) >= week_start,
            col(CallLog.call_date) <= week_end,
            CallLog.status == CallLogStatus.COMPLETED.value,
        )
        result = await session.exec(stmt)  # type: ignore[arg-type]
        calls = result.all()

        calls_answered = len(calls)

        # Count goals: calls with clear or partial outcome confidence.
        goals_set = sum(
            1
            for c in calls
            if c.call_outcome_confidence
            in (OutcomeConfidence.CLEAR.value, OutcomeConfidence.PARTIAL.value)
        )

        # Format week range for display.
        week_range = f"{week_start.strftime('%b %d')} – {week_end.strftime('%b %d')}"
        user_name = user.name or "there"
        closing = _closing_message(calls_answered)

        content_sid = _get_content_sid()
        content_variables = build_weekly_summary_params(
            user_name=user_name,
            week_range=week_range,
            calls_answered=calls_answered,
            goals_set=goals_set,
            closing_message=closing,
        )

        # ISO week string for dedup key (e.g. "2026-W15").
        iso_year, iso_week, _ = week_end.isocalendar()
        iso_week_str = f"{iso_year}-W{iso_week:02d}"
        dedup = weekly_summary_dedup_key(user_id, iso_week_str)

        wa_service = WhatsAppService()
        outbound_svc = OutboundMessageService(
            session=session, whatsapp_service=wa_service,
        )

        sid = await outbound_svc.send_template_dedup(
            user_id=user_id,
            dedup_key=dedup,
            to=user.phone,
            content_sid=content_sid,
            content_variables=content_variables,
        )

        if sid is None:
            return (
                f"Weekly summary for user {user_id}: "
                "dedup hit or send failed"
            )

        # Stamp last_weekly_summary_sent_at for idempotency tracking.
        user.last_weekly_summary_sent_at = datetime.now(timezone.utc)
        session.add(user)
        await session.commit()

    return (
        f"Weekly summary sent for user {user_id} "
        f"(calls={calls_answered}, goals={goals_set})"
    )


# ---------------------------------------------------------------------------
# Core async logic — hourly sweep
# ---------------------------------------------------------------------------


async def _run_check_and_send_weekly_summaries() -> str:
    """Hourly sweep: find users whose local time is Sunday 5 PM and queue sends.

    For each onboarded user with a timezone configured, convert the current
    UTC time to the user's local timezone.  If it is Sunday (weekday == 6)
    and the 17:xx hour, dispatch a per-user send task.
    """
    now_utc = datetime.now(timezone.utc)
    queued = 0

    async with async_session_factory() as session:
        stmt = select(User).where(
            col(User.timezone).isnot(None),
            User.onboarding_complete.is_(True),  # type: ignore[union-attr]
        )
        result = await session.exec(stmt)  # type: ignore[arg-type]
        users = result.all()

    for user in users:
        try:
            user_tz = ZoneInfo(user.timezone)  # type: ignore[arg-type]
        except (KeyError, TypeError):
            logger.warning(
                "Invalid timezone %r for user %s, skipping",
                user.timezone,
                user.id,
            )
            continue

        user_now = now_utc.astimezone(user_tz)

        # Sunday == weekday 6, 5 PM hour.
        if user_now.weekday() == 6 and user_now.hour == 17:
            send_weekly_summary.delay(user.id)
            queued += 1

    return f"Queued {queued} weekly summaries"


# ---------------------------------------------------------------------------
# Celery task entry points
# ---------------------------------------------------------------------------


@celery_app.task(name="app.tasks.weekly.check_and_send_weekly_summaries")
def check_and_send_weekly_summaries() -> str:
    """Hourly sweep: send weekly summary to users whose local time is Sunday 5 PM."""
    return asyncio.run(_run_check_and_send_weekly_summaries())


@celery_app.task(
    bind=True,
    name="app.tasks.weekly.send_weekly_summary",
    max_retries=2,
    default_retry_delay=60,
)
def send_weekly_summary(self, user_id: int) -> str:
    """Send the weekly summary WhatsApp message for a single user.

    Triggered by the hourly sweep. Reads completed calls for the week,
    counts goals, and sends via the OutboundMessage dedup flow.

    Requirements: 5.6
    """
    try:
        return asyncio.run(_run_send_weekly_summary(user_id))
    except Exception as exc:
        logger.exception(
            "send_weekly_summary failed for user_id=%d", user_id,
        )
        raise self.retry(exc=exc)
