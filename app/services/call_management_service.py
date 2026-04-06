"""CallManagementService — user-facing call management operations.

Provides: schedule_callback, skip_call, reschedule_call, get_next_call,
cancel_all_calls_today.

All methods delegate to CallLogService for state transitions and
optimistic locking.  "Today"-scoped queries use
``CallLog.scheduled_timezone`` (not the current ``User.timezone``).

State guards:
- Operations on terminal states return an error (except idempotent
  no-op on matching terminal state).
- ``in_progress`` only allows ``get_next_call`` and
  ``schedule_callback`` (deferred mode).

Validates: Requirement 21
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlmodel.ext.asyncio.session import AsyncSession

try:
    from twilio.rest import Client as TwilioClient
except ImportError:  # pragma: no cover
    TwilioClient = None  # type: ignore[assignment,misc]

from app.models.call_log import CallLog
from app.models.enums import CallLogStatus, CallType, OccurrenceKind
from app.models.user import User
from app.services.call_log_service import (
    CallLogService,
    InvalidTransitionError,
    StaleVersionError,
    TERMINAL_STATUSES,
)
from app.services.scheduling_helpers import compute_latest_first_call, resolve_local_time

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CallManagementResult:
    """Uniform result for all call management operations."""

    success: bool
    message: str
    call_log_id: int | None = None
    cancelled_count: int | None = None
    next_call: NextCallInfo | None = None


@dataclass(frozen=True, slots=True)
class NextCallInfo:
    """Info about the next scheduled call, in the user's local timezone."""

    call_type: str
    date: str  # e.g. "Monday, April 6"
    time: str  # e.g. "09:15 AM"
    timezone: str  # IANA identifier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TERMINAL_VALUES = frozenset(s.value for s in TERMINAL_STATUSES)


def _is_terminal(status: str) -> bool:
    return status in _TERMINAL_VALUES


def _two_layer_cancel(
    call_log: CallLog,
    twilio_client: object | None,
) -> None:
    """Best-effort Celery revoke + Twilio cancel.  Never raises."""
    if call_log.celery_task_id:
        try:
            from celery.result import AsyncResult

            AsyncResult(call_log.celery_task_id).revoke()
        except Exception:
            logger.warning(
                "Failed to revoke Celery task %s for CallLog %d",
                call_log.celery_task_id,
                call_log.id,
            )

    if call_log.twilio_call_sid and twilio_client is not None:
        try:
            twilio_client.calls(call_log.twilio_call_sid).update(status="canceled")  # type: ignore[union-attr]
        except Exception:
            logger.warning(
                "Failed to cancel Twilio call %s for CallLog %d",
                call_log.twilio_call_sid,
                call_log.id,
            )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class CallManagementService:
    """User-facing call management: skip, reschedule, callback, cancel."""

    def __init__(
        self,
        session: AsyncSession,
        twilio_client: object | None = None,
    ) -> None:
        self.session = session
        self.twilio_client = twilio_client
        self.cls = CallLogService(session)

    # ------------------------------------------------------------------
    # schedule_callback
    # ------------------------------------------------------------------

    async def schedule_callback(
        self,
        user_id: int,
        minutes_from_now: int,
        current_call_log_id: int | None = None,
    ) -> CallManagementResult:
        """Schedule an on-demand callback.

        Two modes:
        - **Standalone** (``current_call_log_id is None``): creates an
          on-demand CallLog picked up by the due-row dispatcher.  Replaces
          any existing pending on-demand call for this user.
        - **Deferred** (``current_call_log_id`` provided): transitions the
          current in-progress call to ``deferred``, then creates a new
          on-demand CallLog with ``replaced_call_log_id`` pointing to it.
        """
        if minutes_from_now < 1 or minutes_from_now > 120:
            return CallManagementResult(
                success=False,
                message="Please choose between 1 and 120 minutes.",
            )

        user = await self.session.get(User, user_id)
        if user is None:
            return CallManagementResult(success=False, message="User not found.")
        tz_name = user.timezone or "UTC"

        now_utc = datetime.now(timezone.utc)
        eta = now_utc + timedelta(minutes=minutes_from_now)
        tz = ZoneInfo(tz_name)
        local_date = eta.astimezone(tz).date()

        # -- Deferred mode: transition current call first ----------------
        replaced_id: int | None = None
        if current_call_log_id is not None:
            current = await self.session.get(CallLog, current_call_log_id)
            if current is None:
                return CallManagementResult(
                    success=False, message="Current call not found."
                )
            if current.status != CallLogStatus.IN_PROGRESS.value:
                return CallManagementResult(
                    success=False,
                    message=(
                        "Can only defer an in-progress call, "
                        f"but call is {current.status!r}."
                    ),
                )
            try:
                await self.cls.update_status(
                    current.id,  # type: ignore[arg-type]
                    CallLogStatus.DEFERRED,
                    current.version,
                )
            except (InvalidTransitionError, StaleVersionError) as exc:
                return CallManagementResult(success=False, message=str(exc))
            replaced_id = current.id

        # -- Replace any existing pending on-demand call -----------------
        existing = await self.cls.find_active_on_demand(user_id)
        if existing is not None:
            _two_layer_cancel(existing, self.twilio_client)
            try:
                await self.cls.update_status(
                    existing.id,  # type: ignore[arg-type]
                    CallLogStatus.CANCELLED,
                    existing.version,
                )
            except (InvalidTransitionError, StaleVersionError) as exc:
                # Cannot cancel the existing on-demand call.  If we already
                # deferred the current call, revert it so the user isn't
                # stranded with no active call and no callback.
                if replaced_id is not None:
                    try:
                        deferred_row = await self.session.get(CallLog, replaced_id)
                        if (
                            deferred_row
                            and deferred_row.status == CallLogStatus.DEFERRED.value
                        ):
                            # Direct revert — DEFERRED is terminal in the
                            # state machine, but this is a compensating
                            # action for a failed atomic sequence.
                            deferred_row.status = CallLogStatus.IN_PROGRESS.value
                            deferred_row.version += 1
                            deferred_row.updated_at = datetime.now(timezone.utc)
                            self.session.add(deferred_row)
                            await self.session.commit()
                            logger.info(
                                "Reverted CallLog %d from deferred to "
                                "in_progress after on-demand cancel failure",
                                replaced_id,
                            )
                    except Exception:
                        logger.error(
                            "Failed to revert deferred CallLog %d after "
                            "on-demand cancel failure",
                            replaced_id,
                        )
                return CallManagementResult(
                    success=False,
                    message=(
                        f"Cannot replace existing on-demand call "
                        f"(status {existing.status!r}): {exc}"
                    ),
                )

        # -- Create new on-demand CallLog --------------------------------
        new_log = CallLog(
            user_id=user_id,
            call_type=CallType.ON_DEMAND.value,
            call_date=local_date,
            scheduled_time=eta,
            scheduled_timezone=tz_name,
            status=CallLogStatus.SCHEDULED.value,
            occurrence_kind=OccurrenceKind.ON_DEMAND.value,
            replaced_call_log_id=replaced_id,
        )
        created = await self.cls.create_call_log(new_log)

        local_time_str = eta.astimezone(tz).strftime("%I:%M %p")
        return CallManagementResult(
            success=True,
            message=f"I'll call you in {minutes_from_now} minutes (at {local_time_str}).",
            call_log_id=created.id,
        )

    # ------------------------------------------------------------------
    # skip_call
    # ------------------------------------------------------------------

    async def skip_call(
        self,
        user_id: int,
        call_type: str,
    ) -> CallManagementResult:
        """Skip the next scheduled call of *call_type* for today.

        Idempotent: skipping an already-skipped call returns success.
        """
        rows = await self.cls.find_today(
            user_id,
            statuses=[CallLogStatus.SCHEDULED],
            call_type=call_type,
        )
        if not rows:
            # Check for idempotent no-op: already skipped today?
            skipped = await self.cls.find_today(
                user_id,
                statuses=[CallLogStatus.SKIPPED],
                call_type=call_type,
            )
            if skipped:
                return CallManagementResult(
                    success=True,
                    message=f"Your {call_type} call is already skipped for today.",
                )
            return CallManagementResult(
                success=False,
                message=f"No upcoming {call_type} call found for today.",
            )

        # Pick the earliest scheduled one
        target = min(rows, key=lambda r: r.scheduled_time)

        # State guard
        if _is_terminal(target.status):
            if target.status == CallLogStatus.SKIPPED.value:
                return CallManagementResult(
                    success=True,
                    message=f"Your {call_type} call is already skipped for today.",
                )
            return CallManagementResult(
                success=False,
                message=f"Cannot skip a {target.status} call.",
            )

        _two_layer_cancel(target, self.twilio_client)

        try:
            await self.cls.update_status(
                target.id,  # type: ignore[arg-type]
                CallLogStatus.SKIPPED,
                target.version,
            )
        except (InvalidTransitionError, StaleVersionError) as exc:
            return CallManagementResult(success=False, message=str(exc))

        return CallManagementResult(
            success=True,
            message=f"Your {call_type} call has been skipped for today.",
            call_log_id=target.id,
        )

    # ------------------------------------------------------------------
    # reschedule_call
    # ------------------------------------------------------------------

    async def reschedule_call(
        self,
        user_id: int,
        call_type: str,
        new_time: time,
    ) -> CallManagementResult:
        """Reschedule today's *call_type* call to *new_time* (user-local).

        One-off exception for today only — does not change the CallWindow.
        Validates that *new_time* is in the future and satisfies the
        retry-buffer formula.
        """
        user = await self.session.get(User, user_id)
        if user is None:
            return CallManagementResult(success=False, message="User not found.")
        tz_name = user.timezone or "UTC"
        tz = ZoneInfo(tz_name)

        rows = await self.cls.find_today(
            user_id,
            statuses=[CallLogStatus.SCHEDULED],
            call_type=call_type,
        )
        if not rows:
            return CallManagementResult(
                success=False,
                message=f"No upcoming {call_type} call found for today.",
            )

        target = min(rows, key=lambda r: r.scheduled_time)

        # State guard
        if _is_terminal(target.status):
            return CallManagementResult(
                success=False,
                message=f"Cannot reschedule a {target.status} call.",
            )
        if target.status == CallLogStatus.IN_PROGRESS.value:
            return CallManagementResult(
                success=False,
                message="Cannot reschedule an in-progress call.",
            )

        # Compute new UTC time using DST-safe helper
        now_utc = datetime.now(timezone.utc)
        local_today = now_utc.astimezone(tz).date()
        resolved = resolve_local_time(local_today, new_time, tz_name)
        new_dt_utc = resolved.utc_dt
        new_dt_local = resolved.local_dt

        if new_dt_utc <= now_utc:
            return CallManagementResult(
                success=False,
                message="The new time must be in the future.",
            )

        # Validate against retry-buffer formula if it's a window-based type
        if call_type in (CallType.MORNING.value, CallType.AFTERNOON.value, CallType.EVENING.value):
            from app.services.call_window_service import CallWindowService

            cws = CallWindowService(self.session)
            windows = await cws.list_windows_for_user(user_id)
            matching = [w for w in windows if w.window_type == call_type]
            if matching:
                window = matching[0]
                latest = compute_latest_first_call(window.end_time, call_type)
                if new_time > latest:
                    return CallManagementResult(
                        success=False,
                        message=(
                            f"New time {new_time.strftime('%H:%M')} is too late — "
                            f"must be before {latest.strftime('%H:%M')} to allow "
                            "for retries."
                        ),
                    )

        # Two-layer cancel on old schedule
        _two_layer_cancel(target, self.twilio_client)

        # Update scheduled_time in-place via optimistic locking
        try:
            updated = await self.cls.update_scheduled_time(
                target.id,  # type: ignore[arg-type]
                new_dt_utc,
                target.version,
                occurrence_kind=OccurrenceKind.RESCHEDULED.value,
                twilio_call_sid=None,
            )
        except (StaleVersionError, ValueError) as exc:
            return CallManagementResult(success=False, message=str(exc))

        local_str = new_dt_local.strftime("%I:%M %p")
        return CallManagementResult(
            success=True,
            message=f"Your {call_type} call has been rescheduled to {local_str}.",
            call_log_id=updated.id,
        )

    # ------------------------------------------------------------------
    # get_next_call
    # ------------------------------------------------------------------

    async def get_next_call(
        self,
        user_id: int,
    ) -> CallManagementResult:
        """Return info about the next scheduled call for *user_id*."""
        call_log = await self.cls.find_next_scheduled(user_id)
        if call_log is None:
            return CallManagementResult(
                success=True,
                message="You have no upcoming calls scheduled.",
            )

        tz_name = call_log.scheduled_timezone or "UTC"
        tz = ZoneInfo(tz_name)
        local_dt = call_log.scheduled_time.astimezone(tz)

        info = NextCallInfo(
            call_type=call_log.call_type,
            date=local_dt.strftime("%A, %B %d"),
            time=local_dt.strftime("%I:%M %p"),
            timezone=tz_name,
        )
        return CallManagementResult(
            success=True,
            message=(
                f"Your next call is a {info.call_type} call on "
                f"{info.date} at {info.time} ({info.timezone})."
            ),
            call_log_id=call_log.id,
            next_call=info,
        )

    # ------------------------------------------------------------------
    # cancel_all_calls_today
    # ------------------------------------------------------------------

    async def cancel_all_calls_today(
        self,
        user_id: int,
    ) -> CallManagementResult:
        """Cancel all scheduled/ringing calls for today.

        Returns success with ``cancelled_count`` (may be 0).
        """
        rows = await self.cls.find_today(
            user_id,
            statuses=[CallLogStatus.SCHEDULED, CallLogStatus.RINGING],
        )

        cancelled = 0
        for row in rows:
            _two_layer_cancel(row, self.twilio_client)
            try:
                await self.cls.update_status(
                    row.id,  # type: ignore[arg-type]
                    CallLogStatus.CANCELLED,
                    row.version,
                )
                cancelled += 1
            except (InvalidTransitionError, StaleVersionError):
                logger.warning(
                    "Could not cancel CallLog %d (concurrent modification); skipping.",
                    row.id,
                )

        if cancelled == 0:
            return CallManagementResult(
                success=True,
                message="No calls to cancel for today.",
                cancelled_count=0,
            )

        return CallManagementResult(
            success=True,
            message=f"Cancelled {cancelled} call(s) for today.",
            cancelled_count=cancelled,
        )
