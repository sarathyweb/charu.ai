"""Celery tasks for warming voice call context before pickup."""

from __future__ import annotations

import logging

from app.celery_app import celery_app, run_async
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


async def _run_prefetch_call_context(call_log_id: int) -> str:
    """Build and cache voice context for a scheduled/ringing call."""
    async with async_session_factory() as session:
        call_log = await session.get(CallLog, call_log_id)
        if call_log is None:
            return f"CallLog {call_log_id} not found"

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

    await store_call_context(call_log_id, instruction, call_ctx)
    logger.info(
        "Prefetched voice context for call_log_id=%d (%d chars)",
        call_log_id,
        len(instruction),
    )
    return f"Prefetched context for CallLog {call_log_id}"


@celery_app.task(name="app.tasks.prefetch.prefetch_call_context")
def prefetch_call_context(call_log_id: int) -> str:
    """Celery entrypoint for voice context prefetch."""
    return run_async(_run_prefetch_call_context(call_log_id))
