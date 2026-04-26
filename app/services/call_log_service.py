"""CallLogService — CRUD, state machine, and query methods for CallLog.

Implements:
- ``create_call_log`` — insert a new CallLog row
- ``update_status`` — transition with state machine validation + optimistic locking
- ``find_by_twilio_sid`` — lookup by Twilio CallSid
- ``find_next_scheduled`` — next scheduled call for a user
- ``find_all_scheduled_today`` — all scheduled calls for a user's local "today"

"Today" queries use ``CallLog.scheduled_timezone`` (the snapshot taken at
materialization time) to compute the user's local date — NOT the current
``User.timezone``.  This ensures timezone changes don't retroactively shift
which rows are considered "today."

Validates: Requirement 22
"""

import logging
from datetime import date, datetime, timezone

from sqlalchemy import update
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.call_log import CallLog
from app.models.enums import CallLogStatus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Status state machine
# ---------------------------------------------------------------------------

VALID_TRANSITIONS: dict[CallLogStatus, set[CallLogStatus]] = {
    CallLogStatus.SCHEDULED: {
        CallLogStatus.DISPATCHING,
        CallLogStatus.RINGING,
        CallLogStatus.MISSED,
        CallLogStatus.CANCELLED,
        CallLogStatus.SKIPPED,
        CallLogStatus.DEFERRED,
    },
    CallLogStatus.DISPATCHING: {
        CallLogStatus.RINGING,
        CallLogStatus.SCHEDULED,  # transport error → back to queue
        CallLogStatus.MISSED,  # terminal Twilio error
        CallLogStatus.CANCELLED,  # user cancels while Twilio API call in flight
    },
    CallLogStatus.RINGING: {
        CallLogStatus.IN_PROGRESS,
        CallLogStatus.MISSED,
        CallLogStatus.CANCELLED,
    },
    CallLogStatus.IN_PROGRESS: {
        CallLogStatus.COMPLETED,
        CallLogStatus.MISSED,  # early disconnect, pipeline failure, AMD machine detection
        CallLogStatus.DEFERRED,
    },
    # Terminal states — no outgoing transitions
    CallLogStatus.COMPLETED: set(),
    CallLogStatus.MISSED: set(),
    CallLogStatus.DEFERRED: set(),
    CallLogStatus.CANCELLED: set(),
    CallLogStatus.SKIPPED: set(),
}

TERMINAL_STATUSES: frozenset[CallLogStatus] = frozenset(
    {
        CallLogStatus.COMPLETED,
        CallLogStatus.MISSED,
        CallLogStatus.DEFERRED,
        CallLogStatus.CANCELLED,
        CallLogStatus.SKIPPED,
    }
)


def validate_transition(
    current: CallLogStatus | str,
    target: CallLogStatus | str,
) -> bool:
    """Return ``True`` if *current → target* is a valid state transition."""
    if isinstance(current, str):
        current = CallLogStatus(current)
    if isinstance(target, str):
        target = CallLogStatus(target)
    return target in VALID_TRANSITIONS.get(current, set())


class StaleVersionError(Exception):
    """Raised when an optimistic-locking update finds a version mismatch."""


class InvalidTransitionError(Exception):
    """Raised when a status transition violates the state machine."""


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class CallLogService:
    """Manages CallLog lifecycle: creation, status transitions, and queries."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # create_call_log
    # ------------------------------------------------------------------

    async def create_call_log(self, call_log: CallLog) -> CallLog:
        """Insert a new ``CallLog`` row and return it with a generated id.

        The caller is responsible for populating required fields
        (``user_id``, ``call_type``, ``call_date``, ``scheduled_time``,
        ``scheduled_timezone``).
        """
        self.session.add(call_log)
        await self.session.commit()
        await self.session.refresh(call_log)
        try:
            from app.tasks.prefetch import enqueue_call_context_prefetch

            await enqueue_call_context_prefetch(
                call_log.id,  # type: ignore[arg-type]
                call_log.scheduled_time,
            )
        except Exception:
            logger.warning(
                "Failed to enqueue context prefetch for CallLog %s",
                call_log.id,
                exc_info=True,
            )
        return call_log

    # ------------------------------------------------------------------
    # update_status — state machine + optimistic locking
    # ------------------------------------------------------------------

    async def update_status(
        self,
        call_log_id: int,
        new_status: CallLogStatus | str,
        expected_version: int,
        **extra_fields: object,
    ) -> CallLog:
        """Atomically transition a CallLog to *new_status*.

        Uses optimistic locking (``WHERE version = expected_version``) and
        validates the transition against ``VALID_TRANSITIONS``.

        Extra keyword arguments are set on the row alongside the status
        change (e.g. ``twilio_call_sid``, ``actual_start_time``).

        Returns the refreshed ``CallLog``.

        Raises:
            InvalidTransitionError: If the transition is not allowed.
            StaleVersionError: If the row was modified concurrently.
            ValueError: If the CallLog does not exist.
        """
        if isinstance(new_status, str):
            new_status = CallLogStatus(new_status)

        # Fetch current row to validate transition
        call_log = await self.session.get(CallLog, call_log_id)
        if call_log is None:
            raise ValueError(f"CallLog {call_log_id} not found")

        current = CallLogStatus(call_log.status)

        if not validate_transition(current, new_status):
            raise InvalidTransitionError(
                f"Cannot transition CallLog {call_log_id} "
                f"from {current.value!r} to {new_status.value!r}"
            )

        # Build the values dict
        values: dict[str, object] = {
            "status": new_status.value,
            "version": expected_version + 1,
            "updated_at": datetime.now(timezone.utc),
        }
        values.update(extra_fields)

        stmt = (
            update(CallLog)
            .where(
                CallLog.id == call_log_id,
                CallLog.version == expected_version,
            )
            .values(**values)
        )
        result = await self.session.exec(stmt)  # type: ignore[call-overload]
        if result.rowcount == 0:  # type: ignore[union-attr]
            raise StaleVersionError(
                f"CallLog {call_log_id} was modified concurrently "
                f"(expected version {expected_version})"
            )

        await self.session.commit()

        # Refresh to return the updated object
        call_log = await self.session.get(CallLog, call_log_id)
        if call_log is not None:
            await self.session.refresh(call_log)

        logger.info(
            "CallLog %d: %s → %s (v%d → v%d)",
            call_log_id,
            current.value,
            new_status.value,
            expected_version,
            expected_version + 1,
        )
        return call_log  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    async def find_by_twilio_sid(self, twilio_call_sid: str) -> CallLog | None:
        """Return the CallLog matching the given Twilio CallSid, or ``None``."""
        result = await self.session.exec(
            select(CallLog).where(CallLog.twilio_call_sid == twilio_call_sid)
        )
        return result.first()

    async def find_next_scheduled(self, user_id: int) -> CallLog | None:
        """Return the next ``scheduled`` CallLog for *user_id*.

        Orders by ``scheduled_time ASC`` and returns the earliest future
        scheduled entry.
        """
        now_utc = datetime.now(timezone.utc)
        result = await self.session.exec(
            select(CallLog)
            .where(
                CallLog.user_id == user_id,
                CallLog.status == CallLogStatus.SCHEDULED.value,
                CallLog.scheduled_time >= now_utc,
            )
            .order_by(CallLog.scheduled_time.asc())  # type: ignore[union-attr]
            .limit(1)
        )
        return result.first()

    async def find_all_scheduled_today(self, user_id: int) -> list[CallLog]:
        """Return all ``scheduled`` CallLog entries whose *local* date is today.

        "Today" is computed per-row using ``CallLog.scheduled_timezone``
        (the snapshot taken at materialization time), NOT the current
        ``User.timezone``.  This ensures timezone changes don't
        retroactively shift which rows are considered "today."
        """
        return await self.find_today(user_id, statuses=[CallLogStatus.SCHEDULED])

    async def find_today(
        self,
        user_id: int,
        statuses: list[CallLogStatus] | None = None,
        call_type: str | None = None,
    ) -> list[CallLog]:
        """Return CallLog entries for *user_id* whose local date is today.

        Args:
            user_id: The user to query.
            statuses: Filter by these statuses.  Defaults to ``[SCHEDULED]``.
            call_type: Optional filter by call type (e.g. ``"morning"``).

        "Today" is computed per-row using ``CallLog.scheduled_timezone``.
        """
        if statuses is None:
            statuses = [CallLogStatus.SCHEDULED]

        now_utc = datetime.now(timezone.utc)
        status_values = [s.value for s in statuses]

        stmt = select(CallLog).where(
            CallLog.user_id == user_id,
            CallLog.status.in_(status_values),  # type: ignore[union-attr]
        )
        if call_type is not None:
            stmt = stmt.where(CallLog.call_type == call_type)

        result = await self.session.exec(stmt)
        rows = result.all()

        from zoneinfo import ZoneInfo

        today_rows: list[CallLog] = []
        for row in rows:
            try:
                tz = ZoneInfo(row.scheduled_timezone)
            except (KeyError, Exception):
                tz = timezone.utc  # type: ignore[assignment]
            local_now = now_utc.astimezone(tz)
            local_today: date = local_now.date()
            if row.call_date == local_today:
                today_rows.append(row)

        return today_rows

    async def find_active_on_demand(self, user_id: int) -> CallLog | None:
        """Return the active (non-terminal) on-demand CallLog for *user_id*, or None."""
        terminal_values = [s.value for s in TERMINAL_STATUSES]
        result = await self.session.exec(
            select(CallLog)
            .where(
                CallLog.user_id == user_id,
                CallLog.call_type == "on_demand",
                CallLog.status.notin_(terminal_values),  # type: ignore[union-attr]
            )
            .limit(1)
        )
        return result.first()

    async def update_scheduled_time(
        self,
        call_log_id: int,
        new_time: datetime,
        expected_version: int,
        **extra_fields: object,
    ) -> CallLog:
        """Update a CallLog's scheduled_time in-place with optimistic locking.

        Used by reschedule_call to change the time without a status transition.
        Only allowed on ``scheduled`` entries.

        Raises:
            StaleVersionError: If the row was modified concurrently.
            ValueError: If the CallLog does not exist or is not scheduled.
        """
        call_log = await self.session.get(CallLog, call_log_id)
        if call_log is None:
            raise ValueError(f"CallLog {call_log_id} not found")

        if call_log.status != CallLogStatus.SCHEDULED.value:
            raise ValueError(
                f"CallLog {call_log_id} is {call_log.status!r}, "
                "can only reschedule 'scheduled' entries"
            )

        values: dict[str, object] = {
            "scheduled_time": new_time,
            "version": expected_version + 1,
            "updated_at": datetime.now(timezone.utc),
        }
        values.update(extra_fields)

        stmt = (
            update(CallLog)
            .where(
                CallLog.id == call_log_id,
                CallLog.version == expected_version,
            )
            .values(**values)
        )
        result = await self.session.exec(stmt)  # type: ignore[call-overload]
        if result.rowcount == 0:  # type: ignore[union-attr]
            raise StaleVersionError(
                f"CallLog {call_log_id} was modified concurrently "
                f"(expected version {expected_version})"
            )

        await self.session.commit()
        call_log = await self.session.get(CallLog, call_log_id)
        if call_log is not None:
            await self.session.refresh(call_log)
            try:
                from app.config import get_settings
                from app.services.call_context_cache import delete_call_context
                from app.tasks.prefetch import enqueue_call_context_prefetch
            except ImportError:
                pass
            else:
                try:
                    if get_settings().VOICE_CONTEXT_PREFETCH_ENABLED:
                        await delete_call_context(call_log_id)
                        await enqueue_call_context_prefetch(
                            call_log.id,  # type: ignore[arg-type]
                            call_log.scheduled_time,
                        )
                except Exception:
                    logger.warning(
                        "Failed to refresh context prefetch for CallLog %d",
                        call_log_id,
                        exc_info=True,
                    )

        logger.info(
            "CallLog %d: rescheduled to %s (v%d → v%d)",
            call_log_id,
            new_time.isoformat(),
            expected_version,
            expected_version + 1,
        )
        return call_log  # type: ignore[return-value]
