"""CallWindowService — CRUD and lifecycle management for call windows.

Handles validation, upsert, listing, updating, and deactivation of
call windows.  On window edits, hard-deletes future scheduled planned
CallLog entries for that window type and leaves a TODO for
rematerialization (implemented in task 6.2).
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import delete
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.call_log import CallLog
from app.models.call_window import CallWindow
from app.models.enums import CallLogStatus, OccurrenceKind
from app.models.user import User
from app.services.call_window_validation import validate_call_window

logger = logging.getLogger(__name__)


class CallWindowService:
    """Manages CallWindow CRUD with validation and schedule side-effects."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # save_call_window — upsert with validation
    # ------------------------------------------------------------------

    async def save_call_window(
        self,
        user_id: int,
        window_type: str,
        start_time: "time",
        end_time: "time",
    ) -> CallWindow:
        """Validate and upsert a call window for the given user.

        Reads the user's timezone from ``User.timezone``.  If the window
        already exists (same user + window_type), it is updated in place;
        otherwise a new row is created.

        When an existing window's times change, future ``scheduled`` planned
        CallLog entries for that window type are hard-deleted so the daily
        planner can rematerialize them with the new times.

        Returns the persisted ``CallWindow``.

        Raises:
            ValueError: If the user does not exist, has no timezone, or the
                window parameters fail validation.
        """
        # 1. Look up user to get timezone
        user = await self._get_user_or_raise(user_id)

        # 2. Validate using the shared validation helper
        ok, err = validate_call_window(start_time, end_time, user.timezone)
        if not ok:
            raise ValueError(err)

        # 3. Check if a window already exists for (user_id, window_type)
        result = await self.session.exec(
            select(CallWindow).where(
                CallWindow.user_id == user_id,
                CallWindow.window_type == window_type,
            )
        )
        existing = result.first()

        if existing is not None:
            # Detect whether times actually changed
            times_changed = (
                existing.start_time != start_time or existing.end_time != end_time
            )

            existing.start_time = start_time
            existing.end_time = end_time
            existing.is_active = True
            self.session.add(existing)
            await self.session.flush()

            if times_changed:
                await self._hard_delete_future_planned(user_id, window_type)

            await self.session.commit()
            await self.session.refresh(existing)
            return existing

        # 4. Create new window
        window = CallWindow(
            user_id=user_id,
            window_type=window_type,
            start_time=start_time,
            end_time=end_time,
            is_active=True,
        )
        self.session.add(window)
        await self.session.commit()
        await self.session.refresh(window)
        return window

    # ------------------------------------------------------------------
    # list_windows_for_user
    # ------------------------------------------------------------------

    async def list_windows_for_user(self, user_id: int) -> list[CallWindow]:
        """Return all active call windows for the user."""
        result = await self.session.exec(
            select(CallWindow).where(
                CallWindow.user_id == user_id,
                CallWindow.is_active == True,  # noqa: E712
            )
        )
        return list(result.all())

    # ------------------------------------------------------------------
    # update_window
    # ------------------------------------------------------------------

    async def update_window(
        self,
        window_id: int,
        start_time: "time | None" = None,
        end_time: "time | None" = None,
    ) -> CallWindow:
        """Update an existing call window's times.

        At least one of ``start_time`` or ``end_time`` must be provided.
        Validates the resulting window against the user's timezone.
        Hard-deletes future scheduled planned CallLog entries for that
        window type so the planner can rematerialize them.

        Returns the updated ``CallWindow``.

        Raises:
            ValueError: If the window does not exist, or the new times
                fail validation.
        """
        window = await self.session.get(CallWindow, window_id)
        if window is None:
            raise ValueError(f"CallWindow {window_id} not found")

        new_start = start_time if start_time is not None else window.start_time
        new_end = end_time if end_time is not None else window.end_time

        # Validate with user's timezone
        user = await self._get_user_or_raise(window.user_id)
        ok, err = validate_call_window(new_start, new_end, user.timezone)
        if not ok:
            raise ValueError(err)

        times_changed = window.start_time != new_start or window.end_time != new_end

        window.start_time = new_start
        window.end_time = new_end
        self.session.add(window)
        await self.session.flush()

        if times_changed:
            await self._hard_delete_future_planned(window.user_id, window.window_type)

        await self.session.commit()
        await self.session.refresh(window)
        return window

    # ------------------------------------------------------------------
    # deactivate_window
    # ------------------------------------------------------------------

    async def deactivate_window(self, window_id: int) -> CallWindow:
        """Soft-deactivate a call window and hard-delete its future schedule.

        Sets ``is_active = False`` and removes all future ``scheduled``
        planned CallLog entries for that window type.

        Returns the deactivated ``CallWindow``.

        Raises:
            ValueError: If the window does not exist.
        """
        window = await self.session.get(CallWindow, window_id)
        if window is None:
            raise ValueError(f"CallWindow {window_id} not found")

        window.is_active = False
        self.session.add(window)
        await self.session.flush()

        await self._hard_delete_future_planned(window.user_id, window.window_type)

        await self.session.commit()
        await self.session.refresh(window)
        return window

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_user_or_raise(self, user_id: int) -> User:
        """Fetch the user and ensure they have a timezone configured."""
        user = await self.session.get(User, user_id)
        if user is None:
            raise ValueError(f"User {user_id} not found")
        if not user.timezone:
            raise ValueError(
                f"User {user_id} has no timezone configured. "
                "Set the timezone before saving a call window."
            )
        return user

    async def _hard_delete_future_planned(self, user_id: int, window_type: str) -> int:
        """Hard-delete future ``scheduled`` planned CallLog entries.

        Deletes rows where:
        - ``user_id`` matches
        - ``call_type`` matches the window type
        - ``occurrence_kind = 'planned'``
        - ``status = 'scheduled'``
        - ``scheduled_time > now()``

        Hard-delete (not status change) is used to free the partial unique
        index slot ``(user_id, call_type, call_date) WHERE
        occurrence_kind='planned'`` so the daily planner can insert
        replacement rows.

        Returns the number of deleted rows.
        """
        now_utc = datetime.now(timezone.utc)

        stmt = delete(CallLog).where(
            CallLog.user_id == user_id,
            CallLog.call_type == window_type,
            CallLog.occurrence_kind == OccurrenceKind.PLANNED.value,
            CallLog.status == CallLogStatus.SCHEDULED.value,
            CallLog.scheduled_time > now_utc,
        )
        result = await self.session.exec(stmt)  # type: ignore[call-overload]
        deleted = result.rowcount  # type: ignore[union-attr]

        if deleted:
            logger.info(
                "Hard-deleted %d future planned CallLog entries for "
                "user_id=%d, call_type=%s",
                deleted,
                user_id,
                window_type,
            )

        # TODO (task 6.2): Rematerialize CallLog entries with the new
        # window times after the daily planner is implemented.

        return deleted
