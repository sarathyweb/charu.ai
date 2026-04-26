"""Celery tasks for warming voice call context before pickup."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.celery_app import celery_app, run_async
from app.config import get_settings
from app.db import async_session_factory
from app.models.call_log import CallLog
from app.models.enums import CallLogStatus
from app.services.call_context_cache import store_call_context
from app.voice.context import prepare_call_context

logger = logging.getLogger(__name__)

_PREFETCHABLE_STATUSES = {
    CallLogStatus.SCHEDULED.value,
    CallLogStatus.DISPATCHING.value,
    CallLogStatus.RINGING.value,
}
PREFETCH_LEAD_SECONDS = 5 * 60


def _scheduled_time_token(value: datetime) -> str:
    return value.isoformat()


def record_call_context_prefetch(session, call_log: CallLog) -> None:
    """Record a newly materialized call for post-commit prefetch enqueue."""
    if call_log.id is None:
        return
    session.info.setdefault("call_context_prefetches", []).append(
        (call_log.id, call_log.scheduled_time)
    )


async def enqueue_recorded_call_context_prefetches(session) -> list[str]:
    """Enqueue and clear calls recorded on ``session.info``."""
    entries = list(session.info.pop("call_context_prefetches", []))
    task_ids: list[str] = []
    for call_log_id, scheduled_time in entries:
        task_id = await enqueue_call_context_prefetch(call_log_id, scheduled_time)
        if task_id:
            task_ids.append(task_id)
    return task_ids


async def enqueue_call_context_prefetch(
    call_log_id: int,
    scheduled_time: datetime,
    *,
    now: datetime | None = None,
) -> str | None:
    """Schedule a prefetch job for ``scheduled_time - 5 minutes``."""
    if not get_settings().VOICE_CONTEXT_PREFETCH_ENABLED:
        return None

    now_utc = now or datetime.now(timezone.utc)
    eta = scheduled_time - timedelta(seconds=PREFETCH_LEAD_SECONDS)
    apply_options: dict[str, object] = {
        "kwargs": {
            "call_log_id": call_log_id,
            "scheduled_time": _scheduled_time_token(scheduled_time),
        },
        "retry": False,
    }
    if eta > now_utc:
        apply_options["eta"] = eta

    try:
        result = await prefetch_call_context.apply_asyncx(**apply_options)
    except Exception:
        logger.warning(
            "Failed to enqueue context prefetch for call_log_id=%d",
            call_log_id,
            exc_info=True,
        )
        return None

    return getattr(result, "id", None)


async def _run_prefetch_call_context(
    call_log_id: int,
    scheduled_time: str | None = None,
) -> str:
    """Build and cache voice context for a scheduled/ringing call."""
    async with async_session_factory() as session:
        call_log = await session.get(CallLog, call_log_id)
        if call_log is None:
            return f"CallLog {call_log_id} not found"

        if (
            scheduled_time is not None
            and _scheduled_time_token(call_log.scheduled_time) != scheduled_time
        ):
            return (
                f"CallLog {call_log_id} scheduled_time changed, "
                "skipping stale context prefetch"
            )

        if call_log.status not in _PREFETCHABLE_STATUSES:
            return (
                f"CallLog {call_log_id} status={call_log.status}, "
                "skipping context prefetch"
            )

        instruction, call_ctx = await prepare_call_context(
            user_id=call_log.user_id,
            call_type=call_log.call_type,
            session=session,
            call_log_id=call_log_id,
        )

    await store_call_context(
        call_log_id,
        call_log.scheduled_time,
        instruction,
        call_ctx,
    )
    logger.info(
        "Prefetched voice context for call_log_id=%d (%d chars)",
        call_log_id,
        len(instruction),
    )
    return f"Prefetched context for CallLog {call_log_id}"


@celery_app.task(name="app.tasks.prefetch.prefetch_call_context")
def prefetch_call_context(call_log_id: int, scheduled_time: str | None = None) -> str:
    """Celery entrypoint for voice context prefetch."""
    return run_async(_run_prefetch_call_context(call_log_id, scheduled_time))
