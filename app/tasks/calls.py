"""Call scheduling tasks: daily planner, catch-up sweep, due-row dispatcher.

The daily planner materializes the next day's calls for all active users.
The catch-up sweep runs every 15 minutes to handle newly onboarded users
or missed materializations.  The due-row dispatcher (task 6.4) finds
CallLog rows whose scheduled_time has arrived and dispatches trigger tasks.

Design references:
  - Design §3: Call Scheduling (daily materialization, catch-up sweep)
  - Property 33: Planner idempotency — no duplicate scheduled calls
  - Property 39: DST-safe scheduling
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from zoneinfo import ZoneInfo

from app.celery_app import celery_app
from app.db import async_session_factory
from app.models.call_log import CallLog
from app.models.call_window import CallWindow
from app.models.enums import CallLogStatus, CallType, OccurrenceKind
from app.models.user import User
from app.services.scheduling_helpers import (
    compute_jittered_call_time,
    resolve_local_time,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _get_active_users(session: AsyncSession) -> list[User]:
    """Return all users who have completed onboarding and have a timezone."""
    result = await session.exec(
        select(User).where(
            User.onboarding_complete == True,  # noqa: E712
            User.timezone.isnot(None),  # type: ignore[union-attr]
        )
    )
    return list(result.all())


async def _get_active_windows(
    session: AsyncSession, user_id: int
) -> list[CallWindow]:
    """Return all active call windows for a user."""
    result = await session.exec(
        select(CallWindow).where(
            CallWindow.user_id == user_id,
            CallWindow.is_active == True,  # noqa: E712
        )
    )
    return list(result.all())


async def _materialize_call(
    session: AsyncSession,
    user: User,
    window: CallWindow,
    target_date: date,
) -> bool:
    """Create a planned CallLog entry for a user/window/date.

    Returns True if a new row was created, False if it already existed
    (idempotent skip via the partial unique index).

    Uses a nested savepoint so that an IntegrityError on duplicate
    insert rolls back only this single row, not the entire transaction.
    """
    # Pick a jittered call time within the window
    local_time = compute_jittered_call_time(
        window_start=window.start_time,
        window_end=window.end_time,
        call_type=window.window_type,
    )

    # Resolve to UTC, handling DST transitions
    resolved = resolve_local_time(
        target_date=target_date,
        local_time=local_time,
        tz_name=user.timezone,  # type: ignore[arg-type]
    )

    call_log = CallLog(
        user_id=user.id,  # type: ignore[arg-type]
        call_type=window.window_type,
        call_date=target_date,
        scheduled_time=resolved.utc_dt,
        scheduled_timezone=user.timezone,  # type: ignore[arg-type]
        status=CallLogStatus.SCHEDULED.value,
        occurrence_kind=OccurrenceKind.PLANNED.value,
        attempt_number=1,
        origin_window_id=window.id,
    )

    # Use a savepoint so a duplicate-key IntegrityError only rolls back
    # this single insert, not the entire transaction.
    try:
        async with session.begin_nested():
            session.add(call_log)
            await session.flush()
    except IntegrityError:
        # Partial unique index violation — row already exists for this
        # user/call_type/date with occurrence_kind='planned'.
        # The savepoint was automatically rolled back by begin_nested.
        logger.debug(
            "Skipped duplicate: user_id=%d, call_type=%s, date=%s",
            user.id,  # type: ignore[arg-type]
            window.window_type,
            target_date,
        )
        return False

    return True


async def _materialize_for_user(
    session: AsyncSession,
    user: User,
    target_date: date,
) -> tuple[int, int]:
    """Materialize calls for all active windows of a user on target_date.

    Returns a tuple of (created_count, total_windows).
    """
    windows = await _get_active_windows(session, user.id)  # type: ignore[arg-type]
    created = 0

    for window in windows:
        if await _materialize_call(session, user, window, target_date):
            created += 1

    return created, len(windows)


# ---------------------------------------------------------------------------
# Core planner logic
# ---------------------------------------------------------------------------


async def _run_daily_planner() -> dict[str, int]:
    """Materialize next day's calls for all active users.

    For each user, computes their local "tomorrow" and creates planned
    CallLog entries for every active window.  Idempotent — skips if a
    planned entry already exists for that user/call_type/date.

    Returns a summary dict with counts.
    """
    total_created = 0
    total_skipped = 0
    users_processed = 0

    async with async_session_factory() as session:
        users = await _get_active_users(session)

        for user in users:
            try:
                tz = ZoneInfo(user.timezone)  # type: ignore[arg-type]
            except (KeyError, Exception):
                logger.warning(
                    "Invalid timezone %r for user_id=%d, skipping",
                    user.timezone,
                    user.id,
                )
                continue

            now_local = datetime.now(timezone.utc).astimezone(tz)
            tomorrow = now_local.date() + timedelta(days=1)

            created, total = await _materialize_for_user(session, user, tomorrow)
            total_created += created
            total_skipped += total - created

            users_processed += 1

        # Commit all new rows in one batch
        await session.commit()

    logger.info(
        "daily_planner: processed %d users, created %d calls, skipped %d duplicates",
        users_processed,
        total_created,
        total_skipped,
    )
    return {
        "users_processed": users_processed,
        "created": total_created,
        "skipped": total_skipped,
    }


async def _run_catchup_sweep() -> dict[str, int]:
    """Catch-up sweep for newly onboarded users or missed materializations.

    Materializes both today's and tomorrow's calls for any user who is
    missing planned entries.  This handles:
    - Users who completed onboarding after the midnight planner ran
    - Missed materializations due to worker downtime
    - Edge cases around midnight in various timezones

    Returns a summary dict with counts.
    """
    total_created = 0
    total_skipped = 0
    users_processed = 0

    async with async_session_factory() as session:
        users = await _get_active_users(session)

        for user in users:
            try:
                tz = ZoneInfo(user.timezone)  # type: ignore[arg-type]
            except (KeyError, Exception):
                logger.warning(
                    "Invalid timezone %r for user_id=%d, skipping",
                    user.timezone,
                    user.id,
                )
                continue

            now_local = datetime.now(timezone.utc).astimezone(tz)
            today = now_local.date()
            tomorrow = today + timedelta(days=1)

            # Materialize today (if not already done)
            created_today, total_today = await _materialize_for_user(
                session, user, today
            )
            total_created += created_today
            total_skipped += total_today - created_today

            # Materialize tomorrow (if not already done)
            created_tomorrow, total_tomorrow = await _materialize_for_user(
                session, user, tomorrow
            )
            total_created += created_tomorrow
            total_skipped += total_tomorrow - created_tomorrow

            users_processed += 1

        await session.commit()

    logger.info(
        "planner_catchup_sweep: processed %d users, created %d calls, skipped %d duplicates",
        users_processed,
        total_created,
        total_skipped,
    )
    return {
        "users_processed": users_processed,
        "created": total_created,
        "skipped": total_skipped,
    }


# ---------------------------------------------------------------------------
# Due-row dispatcher logic
# ---------------------------------------------------------------------------


async def _run_due_row_dispatcher() -> dict[str, int]:
    """Find due CallLog rows and dispatch trigger_call tasks.

    For each row where ``status='scheduled'`` and ``scheduled_time <= now()``:
    1. Atomically claim the row: ``UPDATE SET status='dispatching'
       WHERE id=? AND status='scheduled'`` — commit immediately.
    2. Dispatch ``trigger_call_task`` with the ``call_log_id``.
    3. Store the Celery ``task_id`` on the row for revocation.

    If the atomic UPDATE affects 0 rows (another worker claimed it first),
    the row is skipped.  This guarantees at-most-once dispatch.
    """
    now_utc = datetime.now(timezone.utc)
    claimed = 0
    skipped = 0
    errors = 0

    async with async_session_factory() as session:
        # Step 1: Find all due rows (read-only query — no lock needed).
        result = await session.exec(
            select(CallLog.id, CallLog.version).where(
                CallLog.status == CallLogStatus.SCHEDULED.value,
                CallLog.scheduled_time <= now_utc,
            )
        )
        due_rows = list(result.all())

    if not due_rows:
        logger.debug("due_row_dispatcher: no due rows")
        return {"claimed": 0, "skipped": 0, "errors": 0}

    logger.info("due_row_dispatcher: found %d due rows", len(due_rows))

    # Step 2: Claim each row individually and dispatch.
    # Each claim uses its own session/transaction so a failure on one
    # row doesn't roll back claims on others.
    for call_log_id, version in due_rows:
        try:
            async with async_session_factory() as session:
                # Atomic claim: UPDATE … WHERE status='scheduled' AND version=?
                stmt = (
                    sa_update(CallLog)
                    .where(
                        CallLog.id == call_log_id,
                        CallLog.status == CallLogStatus.SCHEDULED.value,
                        CallLog.version == version,
                    )
                    .values(
                        status=CallLogStatus.DISPATCHING.value,
                        version=version + 1,
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                update_result = await session.exec(stmt)  # type: ignore[call-overload]
                if update_result.rowcount == 0:  # type: ignore[union-attr]
                    # Another worker/process claimed it, or status changed.
                    skipped += 1
                    continue

                await session.commit()

            # Step 3: Dispatch trigger_call_task outside the DB transaction.
            task_result = celery_app.send_task(
                "app.tasks.calls.trigger_call_task",
                kwargs={"call_log_id": call_log_id},
            )

            # Step 4: Store celery_task_id for revocation metadata.
            async with async_session_factory() as session:
                store_stmt = (
                    sa_update(CallLog)
                    .where(CallLog.id == call_log_id)
                    .values(celery_task_id=task_result.id)
                )
                await session.exec(store_stmt)  # type: ignore[call-overload]
                await session.commit()

            claimed += 1
            logger.info(
                "due_row_dispatcher: claimed call_log_id=%d, dispatched task=%s",
                call_log_id,
                task_result.id,
            )

        except Exception:
            errors += 1
            logger.exception(
                "due_row_dispatcher: error processing call_log_id=%d",
                call_log_id,
            )

    return {"claimed": claimed, "skipped": skipped, "errors": errors}


# ---------------------------------------------------------------------------
# Celery tasks
# ---------------------------------------------------------------------------


@celery_app.task(name="app.tasks.calls.daily_planner")
def daily_planner() -> str:
    """Materialize next day's calls for all active users/windows."""
    result = asyncio.run(_run_daily_planner())
    return (
        f"daily_planner: {result['users_processed']} users, "
        f"{result['created']} created, {result['skipped']} skipped"
    )


@celery_app.task(name="app.tasks.calls.planner_catchup_sweep")
def planner_catchup_sweep() -> str:
    """Catch-up sweep for newly onboarded users or missed materializations."""
    result = asyncio.run(_run_catchup_sweep())
    return (
        f"planner_catchup_sweep: {result['users_processed']} users, "
        f"{result['created']} created, {result['skipped']} skipped"
    )


@celery_app.task(name="app.tasks.calls.due_row_dispatcher")
def due_row_dispatcher() -> str:
    """Find due CallLog rows and dispatch a trigger_call task per row.

    Runs every 1 minute via Celery Beat.  For each CallLog where
    ``status='scheduled'`` and ``scheduled_time <= now()``, atomically
    claims the row by setting ``status='dispatching'`` (with a
    ``WHERE status='scheduled'`` guard), then dispatches a
    ``trigger_call_task`` Celery task for that row.

    The atomic UPDATE ensures at-most-once dispatch even if two Beat
    instances overlap or the task takes longer than 1 minute.

    Design references:
      - Design §3: Dispatch state transitions (step 1)
      - Property 34: At-most-once call dispatch
    """
    result = asyncio.run(_run_due_row_dispatcher())
    return (
        f"due_row_dispatcher: {result['claimed']} dispatched, "
        f"{result['skipped']} already claimed, "
        f"{result['errors']} errors"
    )


# ---------------------------------------------------------------------------
# Trigger call task — places the actual Twilio outbound call
# ---------------------------------------------------------------------------


async def _run_trigger_call(call_log_id: int) -> dict[str, str]:
    """Place an outbound Twilio call for a dispatched CallLog row.

    Design references:
      - Design §3: Dispatch state transitions (steps 2–5)
      - Design §2: Voice Call Pipeline (pre-call context injection)
      - Property 34: At-most-once call dispatch
      - Property 42: WebSocket stream token validation

    Steps:
      1. Verify CallLog status is still ``dispatching`` (guard against
         race with cancellation).
      2. Fetch the user's phone number.
      3. Build TwiML with ``<Connect><Stream>`` pointing to the voice
         WebSocket endpoint, including an HMAC stream token and custom
         parameters (``call_log_id``, ``user_id``, ``call_type``).
      4. Place the outbound call via Twilio REST API with AMD, status
         callbacks, and a ``time_limit`` based on call type.
      5. On success: update CallLog to ``ringing`` with ``twilio_call_sid``.
      6. On Twilio transport error (5xx, timeout): revert to ``scheduled``.
      7. On Twilio terminal error (invalid number, 4xx): mark ``missed``.
    """
    from twilio.base.exceptions import TwilioRestException
    from twilio.rest import Client as TwilioClient

    from app.config import get_settings
    from app.services.call_log_service import (
        CallLogService,
        InvalidTransitionError,
        StaleVersionError,
    )
    from app.utils import generate_stream_token

    settings = get_settings()

    # ------------------------------------------------------------------
    # Step 1: Verify the CallLog is still in 'dispatching' state
    # ------------------------------------------------------------------
    async with async_session_factory() as session:
        call_log = await session.get(CallLog, call_log_id)
        if call_log is None:
            logger.error("trigger_call_task: CallLog %d not found", call_log_id)
            return {"status": "error", "reason": "call_log_not_found"}

        if call_log.status != CallLogStatus.DISPATCHING.value:
            logger.info(
                "trigger_call_task: CallLog %d status is %r (expected 'dispatching'), skipping",
                call_log_id,
                call_log.status,
            )
            return {"status": "skipped", "reason": f"status={call_log.status}"}

        current_version = call_log.version
        user_id = call_log.user_id
        call_type = call_log.call_type

        # Fetch user phone
        user = await session.get(User, user_id)
        if user is None:
            logger.error(
                "trigger_call_task: User %d not found for CallLog %d",
                user_id,
                call_log_id,
            )
            return {"status": "error", "reason": "user_not_found"}

        user_phone = user.phone

    # ------------------------------------------------------------------
    # Step 2: Build TwiML with <Connect><Stream>
    # ------------------------------------------------------------------
    stream_token = generate_stream_token(
        secret=settings.STREAM_TOKEN_SECRET,
        call_log_id=call_log_id,
        user_id=user_id,
    )

    base_url = settings.WEBHOOK_BASE_URL.rstrip("/")
    stream_url = f"{base_url}/voice/stream"
    # Append token as query parameter for WebSocket auth
    stream_url_with_token = f"{stream_url}?token={stream_token}"

    twiml = (
        "<Response>"
        "  <Connect>"
        f'    <Stream url="{stream_url_with_token}">'
        f'      <Parameter name="call_log_id" value="{call_log_id}" />'
        f'      <Parameter name="user_id" value="{user_id}" />'
        f'      <Parameter name="call_type" value="{call_type}" />'
        "    </Stream>"
        "  </Connect>"
        "</Response>"
    )

    # Call type determines time_limit (seconds)
    time_limit = 180 if call_type == CallType.EVENING.value else 300

    # ------------------------------------------------------------------
    # Step 3: Place the outbound call via Twilio REST API
    # ------------------------------------------------------------------
    status_callback_url = f"{base_url}/voice/status-callback"
    amd_callback_url = f"{base_url}/voice/amd-callback"

    try:
        twilio_client = TwilioClient(
            settings.TWILIO_ACCOUNT_SID,
            settings.TWILIO_AUTH_TOKEN,
        )

        call = twilio_client.calls.create(
            from_=settings.TWILIO_VOICE_NUMBER,
            to=user_phone,
            twiml=twiml,
            time_limit=time_limit,
            timeout=30,  # ring timeout in seconds
            status_callback=status_callback_url,
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            status_callback_method="POST",
            machine_detection="Enable",
            async_amd=True,
            async_amd_status_callback=amd_callback_url,
            async_amd_status_callback_method="POST",
        )

        twilio_call_sid = call.sid
        logger.info(
            "trigger_call_task: Twilio call placed for CallLog %d, sid=%s",
            call_log_id,
            twilio_call_sid,
        )

    except TwilioRestException as exc:
        # ------------------------------------------------------------------
        # Step 4a: Handle Twilio errors
        # ------------------------------------------------------------------
        if exc.status >= 500:
            # Transport error (5xx) → return to scheduled for retry
            logger.warning(
                "trigger_call_task: Twilio 5xx for CallLog %d: %s",
                call_log_id,
                exc,
            )
            async with async_session_factory() as session:
                svc = CallLogService(session)
                try:
                    await svc.update_status(
                        call_log_id,
                        CallLogStatus.SCHEDULED,
                        expected_version=current_version,
                    )
                except (StaleVersionError, InvalidTransitionError):
                    logger.warning(
                        "trigger_call_task: Could not revert CallLog %d to scheduled",
                        call_log_id,
                    )
            return {"status": "transport_error", "reason": str(exc)}

        else:
            # Terminal error (4xx — invalid number, etc.) → mark missed
            logger.error(
                "trigger_call_task: Twilio terminal error for CallLog %d: %s",
                call_log_id,
                exc,
            )
            async with async_session_factory() as session:
                svc = CallLogService(session)
                try:
                    await svc.update_status(
                        call_log_id,
                        CallLogStatus.MISSED,
                        expected_version=current_version,
                    )
                except (StaleVersionError, InvalidTransitionError):
                    logger.warning(
                        "trigger_call_task: Could not mark CallLog %d as missed",
                        call_log_id,
                    )
            return {"status": "terminal_error", "reason": str(exc)}

    except Exception as exc:
        # Network timeout or unexpected error → return to scheduled
        logger.exception(
            "trigger_call_task: Unexpected error for CallLog %d",
            call_log_id,
        )
        async with async_session_factory() as session:
            svc = CallLogService(session)
            try:
                await svc.update_status(
                    call_log_id,
                    CallLogStatus.SCHEDULED,
                    expected_version=current_version,
                )
            except (StaleVersionError, InvalidTransitionError):
                pass
        return {"status": "error", "reason": str(exc)}

    # ------------------------------------------------------------------
    # Step 5: Success — transition to ringing with twilio_call_sid
    # ------------------------------------------------------------------
    async with async_session_factory() as session:
        svc = CallLogService(session)
        try:
            await svc.update_status(
                call_log_id,
                CallLogStatus.RINGING,
                expected_version=current_version,
                twilio_call_sid=twilio_call_sid,
            )
        except (StaleVersionError, InvalidTransitionError) as exc:
            logger.warning(
                "trigger_call_task: Could not transition CallLog %d to ringing: %s",
                call_log_id,
                exc,
            )
            return {"status": "transition_error", "reason": str(exc)}

    return {"status": "ringing", "twilio_call_sid": twilio_call_sid}


@celery_app.task(name="app.tasks.calls.trigger_call_task")
def trigger_call_task(call_log_id: int) -> str:
    """Place an outbound Twilio call for a dispatched CallLog row.

    Dispatched by the due-row dispatcher after atomically claiming the
    row (``status='dispatching'``).  This task:

    1. Verifies the CallLog is still ``dispatching`` (guards against
       cancellation between dispatch and execution).
    2. Places the outbound call via Twilio REST API with AMD, status
       callbacks, and a ``<Connect><Stream>`` TwiML pointing to the
       voice WebSocket endpoint with an HMAC stream token.
    3. On success: transitions to ``ringing`` with ``twilio_call_sid``.
    4. On Twilio transport error (5xx): reverts to ``scheduled`` for
       the dispatcher to retry.
    5. On Twilio terminal error (invalid number): marks ``missed``.

    Design references:
      - Design §3: Dispatch state transitions (steps 2–5)
      - Requirements 6, 14
      - Property 34: At-most-once call dispatch
      - Property 42: WebSocket stream token validation
    """
    result = asyncio.run(_run_trigger_call(call_log_id))
    return (
        f"trigger_call_task: call_log_id={call_log_id}, "
        f"status={result['status']}"
    )
