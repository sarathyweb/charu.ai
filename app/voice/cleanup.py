"""Post-call cleanup — transcript save, task dispatch, anti-habituation update.

Called from the ``finally`` block of the voice WebSocket handler after a
normal (non-early-disconnect) call ends.  Performs all post-call side
effects in a single async function so the voice endpoint stays clean.

Responsibilities:
  1. Save transcript as JSON, update ``CallLog.transcript_filename``
  2. Transition CallLog to ``completed`` (if not already terminal)
  3. Dispatch recap Celery task (30 s delay)
  4. Dispatch midday check-in Celery task (4-5 h delay, morning calls only)
  5. Dispatch email draft review Celery task (60 s delay, if pending draft)
  6. Update anti-habituation state on User model

Design references:
  - Design §2: Voice Call Pipeline (post-call cleanup)
  - Design §7: Anti-Habituation System (streak tracking)
  - Requirements 5, 13, 14.5
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path

from app.db import async_session_factory
from app.models.call_log import CallLog
from app.models.email_draft_state import EmailDraftState
from app.models.enums import CallLogStatus, CallType, DraftStatus
from app.models.user import User
from app.services.anti_habituation import update_streak
from app.services.call_log_service import (
    CallLogService,
    InvalidTransitionError,
    StaleVersionError,
)

logger = logging.getLogger(__name__)

#: Directory where transcript JSON files are stored.
TRANSCRIPT_DIR = os.environ.get("TRANSCRIPT_DIR", "data/transcripts")


# ---------------------------------------------------------------------------
# Transcript persistence
# ---------------------------------------------------------------------------


def _save_transcript_file(
    call_log_id: int,
    transcript_dicts: list[dict],
) -> str:
    """Write transcript entries to a JSON file and return the filename.

    The file is stored under ``TRANSCRIPT_DIR`` with a deterministic
    name: ``transcript_{call_log_id}.json``.
    """
    Path(TRANSCRIPT_DIR).mkdir(parents=True, exist_ok=True)
    filename = f"transcript_{call_log_id}.json"
    filepath = Path(TRANSCRIPT_DIR) / filename

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(
            {
                "call_log_id": call_log_id,
                "entries": transcript_dicts,
                "saved_at": datetime.now(timezone.utc).isoformat(),
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    logger.info(
        "Saved transcript for call_log_id=%d (%d entries) → %s",
        call_log_id,
        len(transcript_dicts),
        filepath,
    )
    return filename


# ---------------------------------------------------------------------------
# Celery task dispatch helpers (async-safe via PatchedTask)
# ---------------------------------------------------------------------------


async def _dispatch_recap(call_log_id: int, call_type: str) -> None:
    """Dispatch the appropriate recap Celery task with a 30 s delay."""
    from app.tasks.recap import send_evening_recap, send_post_call_recap

    try:
        if call_type == CallType.EVENING.value:
            await send_evening_recap.apply_asyncx(
                args=[call_log_id], countdown=30
            )
        else:
            await send_post_call_recap.apply_asyncx(
                args=[call_log_id], countdown=30
            )
        logger.info(
            "Dispatched recap task for call_log_id=%d (type=%s, delay=30s)",
            call_log_id,
            call_type,
        )
    except Exception:
        logger.exception(
            "Failed to dispatch recap task for call_log_id=%d", call_log_id
        )


async def _dispatch_midday_checkin(
    call_log_id: int,
    call_type: str,
    call_end_utc: datetime,
    user_timezone: str | None,
) -> None:
    """Dispatch midday check-in for morning/afternoon calls only."""
    if call_type not in (CallType.MORNING.value, CallType.AFTERNOON.value):
        return
    if not user_timezone:
        logger.warning(
            "Skipping midday check-in for call_log_id=%d — no user timezone",
            call_log_id,
        )
        return

    from app.services.scheduling_helpers import compute_midday_checkin_time
    from app.tasks.checkin import send_midday_checkin

    checkin_utc = compute_midday_checkin_time(call_end_utc, user_timezone)
    if checkin_utc is None:
        logger.info(
            "Midday check-in skipped for call_log_id=%d — would be after 6pm",
            call_log_id,
        )
        return

    delay_seconds = max(
        int((checkin_utc - call_end_utc).total_seconds()), 1
    )

    try:
        await send_midday_checkin.apply_asyncx(
            args=[call_log_id], countdown=delay_seconds
        )
        logger.info(
            "Dispatched midday check-in for call_log_id=%d (delay=%ds)",
            call_log_id,
            delay_seconds,
        )
    except Exception:
        logger.exception(
            "Failed to dispatch midday check-in for call_log_id=%d",
            call_log_id,
        )


async def _dispatch_draft_review(user_id: int) -> None:
    """Dispatch email draft review task if a pending draft exists."""
    try:
        from sqlmodel import select

        async with async_session_factory() as session:
            result = await session.exec(
                select(EmailDraftState)
                .where(
                    EmailDraftState.user_id == user_id,
                    EmailDraftState.status == DraftStatus.PENDING_REVIEW.value,
                    EmailDraftState.draft_review_sent_at.is_(None),  # type: ignore[union-attr]
                )
                .limit(1)
            )
            draft = result.first()

        if draft is None:
            return

        # Import lazily — the task module may not exist yet for MVP
        try:
            from app.tasks.draft_review import send_draft_review  # type: ignore[import-not-found]

            await send_draft_review.apply_asyncx(
                args=[draft.id], countdown=60
            )
            logger.info(
                "Dispatched draft review task for draft_id=%d (delay=60s)",
                draft.id,
            )
        except ImportError:
            logger.debug(
                "Draft review task not implemented yet — skipping for draft_id=%d",
                draft.id,
            )
    except Exception:
        logger.exception(
            "Failed to dispatch draft review for user_id=%d", user_id
        )


# ---------------------------------------------------------------------------
# Anti-habituation state update
# ---------------------------------------------------------------------------


async def _update_anti_habituation(
    user_id: int,
    call_ctx: dict,
) -> None:
    """Persist opener, approach, and streak from the pre-call context."""
    opener = call_ctx.get("opener")
    approach = call_ctx.get("approach")
    new_streak = call_ctx.get("streak_days")
    new_last_active = call_ctx.get("new_last_active")

    if not opener and not approach:
        return

    try:
        async with async_session_factory() as session:
            user = await session.get(User, user_id)
            if user is None:
                logger.warning(
                    "Anti-habituation update: user %d not found", user_id
                )
                return

            if opener and isinstance(opener, dict):
                user.last_opener_id = opener.get("id")
            if approach:
                user.last_approach = str(approach)
            if new_streak is not None:
                user.consecutive_active_days = new_streak
            if new_last_active is not None:
                user.last_active_date = new_last_active

            session.add(user)
            await session.commit()

            logger.info(
                "Updated anti-habituation state for user %d: "
                "opener=%s, approach=%s, streak=%s",
                user_id,
                user.last_opener_id,
                user.last_approach,
                user.consecutive_active_days,
            )
    except Exception:
        logger.exception(
            "Failed to update anti-habituation state for user %d", user_id
        )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def post_call_cleanup(
    call_log_id: int,
    user_id: int,
    call_type: str,
    transcript_dicts: list[dict],
    call_ctx: dict,
) -> None:
    """Run all post-call cleanup actions.

    Called from the ``finally`` block of ``voice_stream`` for normal
    (non-early-disconnect) calls.

    Args:
        call_log_id: The CallLog row ID.
        user_id: The user's database ID.
        call_type: One of ``morning``, ``afternoon``, ``evening``,
            ``on_demand``.
        transcript_dicts: Serialised transcript entries from
            ``TranscriptCollector.to_dicts()``.
        call_ctx: The context dict returned by ``prepare_call_context``
            (contains opener, approach, streak data).
    """
    now_utc = datetime.now(timezone.utc)

    # 1. Save transcript file
    transcript_filename: str | None = None
    try:
        if transcript_dicts:
            transcript_filename = _save_transcript_file(
                call_log_id, transcript_dicts
            )
    except Exception:
        logger.exception(
            "Failed to save transcript for call_log_id=%d", call_log_id
        )

    # 2. Transition CallLog to completed + save transcript filename
    user_timezone: str | None = None
    try:
        async with async_session_factory() as session:
            call_log = await session.get(CallLog, call_log_id)
            if call_log is None:
                logger.error(
                    "post_call_cleanup: CallLog %d not found", call_log_id
                )
                return

            user_timezone = call_log.scheduled_timezone

            # Only transition if currently in_progress
            if call_log.status == CallLogStatus.IN_PROGRESS.value:
                svc = CallLogService(session)
                try:
                    call_log = await svc.update_status(
                        call_log_id,
                        CallLogStatus.COMPLETED,
                        expected_version=call_log.version,
                        end_time=now_utc,
                        transcript_filename=transcript_filename,
                    )
                except (StaleVersionError, InvalidTransitionError) as exc:
                    logger.warning(
                        "post_call_cleanup: could not complete CallLog %d: %s",
                        call_log_id,
                        exc,
                    )
            elif transcript_filename:
                # Already in a terminal state but we still want the transcript
                call_log.transcript_filename = transcript_filename
                session.add(call_log)
                await session.commit()
    except Exception:
        logger.exception(
            "post_call_cleanup: error updating CallLog %d", call_log_id
        )

    # 3. Dispatch recap task (30 s delay)
    await _dispatch_recap(call_log_id, call_type)

    # 4. Dispatch midday check-in (4-5 h delay, morning/afternoon only)
    await _dispatch_midday_checkin(
        call_log_id, call_type, now_utc, user_timezone
    )

    # 5. Dispatch email draft review (60 s delay, if pending draft)
    await _dispatch_draft_review(user_id)

    # 6. Update anti-habituation state
    await _update_anti_habituation(user_id, call_ctx)

    logger.info(
        "post_call_cleanup complete for call_log_id=%d "
        "(transcript=%s, type=%s)",
        call_log_id,
        transcript_filename,
        call_type,
    )
