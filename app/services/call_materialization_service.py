"""Shared call-log materialization helpers."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.call_log import CallLog
from app.models.call_window import CallWindow
from app.models.enums import CallLogStatus, OccurrenceKind
from app.models.user import User
from app.services.scheduling_helpers import (
    compute_first_call_date,
    compute_jittered_call_time,
    resolve_local_time,
)

logger = logging.getLogger(__name__)


async def rematerialize_future_calls(
    session: AsyncSession,
    user: User,
    window_type_filter: str | None = None,
) -> int:
    """Materialize today/tomorrow planned calls for active windows.

    The helper is idempotent: duplicate planned rows are skipped by the
    partial unique index and do not abort the caller's transaction.
    """
    if not user.id or not user.timezone:
        return 0

    tz = ZoneInfo(user.timezone)
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)
    today = now_local.date()
    tomorrow = today + timedelta(days=1)

    stmt = select(CallWindow).where(
        CallWindow.user_id == user.id,
        CallWindow.is_active == True,  # noqa: E712
    )
    if window_type_filter:
        stmt = stmt.where(CallWindow.window_type == window_type_filter)

    result = await session.exec(stmt)
    windows = list(result.all())

    created = 0
    for window in windows:
        target_dates = [tomorrow]
        first_date = compute_first_call_date(
            now_utc=now_utc,
            window_start=window.start_time,
            window_end=window.end_time,
            call_type=window.window_type,
            tz_name=user.timezone,
        )
        if first_date == today:
            target_dates.insert(0, today)

        for target_date in target_dates:
            local_time = compute_jittered_call_time(
                window_start=window.start_time,
                window_end=window.end_time,
                call_type=window.window_type,
            )
            resolved = resolve_local_time(
                target_date=target_date,
                local_time=local_time,
                tz_name=user.timezone,
            )
            call_log = CallLog(
                user_id=user.id,
                call_type=window.window_type,
                call_date=target_date,
                scheduled_time=resolved.utc_dt,
                scheduled_timezone=user.timezone,
                status=CallLogStatus.SCHEDULED.value,
                occurrence_kind=OccurrenceKind.PLANNED.value,
                attempt_number=1,
                origin_window_id=window.id,
            )
            try:
                async with session.begin_nested():
                    session.add(call_log)
                    await session.flush()
                created += 1
            except IntegrityError:
                logger.debug(
                    "Skipped duplicate planned call for user_id=%d type=%s date=%s",
                    user.id,
                    window.window_type,
                    target_date,
                )

    if created:
        logger.info(
            "Rematerialized %d planned calls for user_id=%d type_filter=%s",
            created,
            user.id,
            window_type_filter,
        )
    return created
