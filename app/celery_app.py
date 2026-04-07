"""Celery application with Redis broker and RedBeat scheduler.

Usage:
    # Worker (processes tasks)
    celery -A app.celery_app worker --loglevel=info

    # Beat (schedules periodic tasks via RedBeat)
    celery -A app.celery_app beat -S redbeat.RedBeatScheduler --loglevel=info
"""

from __future__ import annotations

import asyncio

from celery import Celery
from celery.schedules import crontab

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "charu",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=[
        "app.tasks.calls",
        "app.tasks.recap",
        "app.tasks.checkin",
        "app.tasks.weekly",
        "app.tasks.cleanup",
    ],
)


# ---------------------------------------------------------------------------
# Core configuration
# ---------------------------------------------------------------------------
celery_app.conf.update(
    # Serialization
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    # Timezone
    timezone="UTC",
    enable_utc=True,
    # RedBeat scheduler (stores schedule metadata in Redis)
    beat_scheduler="redbeat.RedBeatScheduler",
    redbeat_redis_url=settings.REDIS_URL,
    # Reliability: acknowledge tasks only after they complete, not when fetched
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # Avoid ETA redelivery issues with Redis (default visibility_timeout is 1h)
    broker_transport_options={"visibility_timeout": 7200},  # 2 hours
)


# ---------------------------------------------------------------------------
# PatchedTask — async-safe dispatch from FastAPI
# ---------------------------------------------------------------------------
class PatchedTask(celery_app.Task):  # type: ignore[name-defined]
    """Celery Task subclass with async-safe dispatch helpers.

    FastAPI runs on an asyncio event loop.  Celery's ``apply_async`` and
    ``delay`` talk to the Redis broker synchronously, which would block the
    loop.  ``apply_asyncx`` and ``delayx`` wrap the sync calls in
    ``asyncio.to_thread`` so they can be awaited safely from async code.
    """

    async def apply_asyncx(
        self,
        args: list | tuple | None = None,
        kwargs: dict | None = None,
        **options,
    ):
        return await asyncio.to_thread(
            super().apply_async, args, kwargs, **options
        )

    async def delayx(self, *args, **kwargs):
        return await self.apply_asyncx(args, kwargs)


celery_app.Task = PatchedTask  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Per-worker asyncio runner — one event loop per worker process
# ---------------------------------------------------------------------------
from celery.signals import worker_process_init, worker_process_shutdown  # noqa: E402

_runner: asyncio.Runner | None = None


def run_async(coro):
    """Run an async coroutine on the worker's shared event loop.

    Replaces ``asyncio.run()`` in Celery tasks.  ``asyncio.run()`` creates
    (and closes) a new event loop each time, but the module-level async
    engine's connection pool is bound to a single loop.  Using a persistent
    ``asyncio.Runner`` keeps all tasks on the same loop so the pool stays
    valid.
    """
    if _runner is None:
        raise RuntimeError(
            "run_async() called outside a Celery worker process "
            "(no Runner initialised)"
        )
    return _runner.run(coro)


@worker_process_init.connect
def _init_worker_runner(**kwargs):
    """Set up the per-worker asyncio Runner and dispose inherited connections.

    1. Dispose the inherited asyncpg pool (fork-safety — connections hold
       Futures from the parent's dead loop).
    2. Create a persistent ``asyncio.Runner`` whose loop all tasks share,
       keeping the async engine's pool bound to one consistent loop.
    """
    global _runner

    from app.db import engine  # noqa: F811
    engine.sync_engine.dispose(close=False)

    _runner = asyncio.Runner()


@worker_process_shutdown.connect
def _close_worker_runner(**kwargs):
    """Tear down the per-worker asyncio Runner on graceful shutdown."""
    global _runner
    if _runner is not None:
        _runner.close()
        _runner = None


# ---------------------------------------------------------------------------
# Beat schedule — periodic tasks
# ---------------------------------------------------------------------------
celery_app.conf.beat_schedule = {
    # ── Call materialization ──────────────────────────────────────────────
    # Daily planner: materialize next day's calls for all users.
    # Runs at midnight UTC; the task itself computes each user's local date.
    "daily-planner": {
        "task": "app.tasks.calls.daily_planner",
        "schedule": crontab(minute=0, hour=0),
    },
    # Catch-up sweep: pick up newly-onboarded users or missed materializations.
    "planner-catchup-sweep": {
        "task": "app.tasks.calls.planner_catchup_sweep",
        "schedule": crontab(minute="*/15"),  # every 15 minutes
    },
    # Due-row dispatcher: find CallLog rows whose scheduled_time has arrived
    # and dispatch a Celery task to place each call.
    "due-row-dispatcher": {
        "task": "app.tasks.calls.due_row_dispatcher",
        "schedule": crontab(minute="*"),  # every 1 minute
    },
    # Stale-dispatching sweep: reclaim rows stuck in 'dispatching' for
    # over 10 minutes (broker publish + revert both failed, or worker crash).
    "stale-dispatching-sweep": {
        "task": "app.tasks.calls.stale_dispatching_sweep",
        "schedule": crontab(minute="*/5"),  # every 5 minutes
    },
    # ── WhatsApp messaging ───────────────────────────────────────────────
    # Weekly summary sweep: check which users' local time is Sunday 5 PM.
    "weekly-summary-sweep": {
        "task": "app.tasks.weekly.check_and_send_weekly_summaries",
        "schedule": crontab(minute=0),  # every hour on the hour
    },
    # ── Cleanup / retention ──────────────────────────────────────────────
    # Draft expiry: abandon non-terminal email drafts older than 2 hours.
    "draft-expiry": {
        "task": "app.tasks.cleanup.expire_stale_drafts",
        "schedule": crontab(minute="*/15"),  # every 15 minutes
    },
    # Transcript cleanup: delete transcript artifacts older than 30 days
    # and null out CallLog.transcript_filename.
    "transcript-cleanup": {
        "task": "app.tasks.cleanup.cleanup_old_transcripts",
        "schedule": crontab(minute=0, hour=3),  # daily at 03:00 UTC
    },
    # Call log retention: delete call log entries older than 90 days.
    "call-log-retention": {
        "task": "app.tasks.cleanup.cleanup_old_call_logs",
        "schedule": crontab(minute=0, hour=4),  # daily at 04:00 UTC
    },
}
