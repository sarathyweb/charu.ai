"""Midday check-in reply detection.

Detects when an inbound WhatsApp message is likely a response to a
recently-sent midday check-in, and returns the relevant call context
(goal, next_action) so the agent can respond appropriately.

A message is considered a check-in reply when:
  - A midday check-in was sent to the user (``CallLog.checkin_sent_at`` is set)
  - The check-in was sent within the last 60 minutes
  - The call has a ``next_action`` (the check-in referenced it)

Design references:
  - Design §5: WhatsApp Messaging
  - Research 27: Midday Check-In
  - Requirements 13
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.call_log import CallLog
from app.models.enums import CallLogStatus, CallType
from app.models.user import User

logger = logging.getLogger(__name__)

#: Window (in minutes) after a check-in is sent during which an inbound
#: message is considered a reply to that check-in.
CHECKIN_REPLY_WINDOW_MINUTES: int = 60


@dataclass(frozen=True)
class CheckinContext:
    """Context from a pending midday check-in for agent injection."""

    call_log_id: int
    goal: str | None
    next_action: str


async def find_pending_checkin(
    user_id: int,
    session: AsyncSession,
) -> CheckinContext | None:
    """Return check-in context if the user has a recent pending check-in.

    A "pending" check-in is one where ``checkin_sent_at`` is set and
    falls within the reply window (default 60 minutes).

    Returns ``None`` if no pending check-in is found.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=CHECKIN_REPLY_WINDOW_MINUTES,
    )

    stmt = (
        select(CallLog)
        .where(
            CallLog.user_id == user_id,
            CallLog.status == CallLogStatus.COMPLETED.value,
            CallLog.call_type.in_(  # type: ignore[union-attr]
                [CallType.MORNING.value, CallType.AFTERNOON.value],
            ),
            CallLog.checkin_sent_at.isnot(None),  # type: ignore[union-attr]
            CallLog.checkin_sent_at >= cutoff,  # type: ignore[operator]
            CallLog.checkin_replied_at.is_(None),  # type: ignore[union-attr]
            CallLog.next_action.isnot(None),  # type: ignore[union-attr]
        )
        .order_by(CallLog.checkin_sent_at.desc())  # type: ignore[union-attr]
        .limit(1)
    )

    result = await session.exec(stmt)
    call_log = result.first()

    if call_log is None:
        return None

    return CheckinContext(
        call_log_id=call_log.id,  # type: ignore[arg-type]
        goal=call_log.goal,
        next_action=call_log.next_action,  # type: ignore[arg-type]
    )


def build_checkin_reply_prefix(ctx: CheckinContext) -> str:
    """Build a context prefix to prepend to the user's message.

    This gives the agent awareness that the user is responding to a
    midday check-in, along with the goal/next_action they were asked
    about.
    """
    parts = [
        "[SYSTEM: The user is responding to a midday check-in.",
    ]
    if ctx.goal:
        parts.append(f"Their morning goal was: {ctx.goal}.")
    parts.append(f"The next action they committed to was: {ctx.next_action}.")
    parts.append(
        "Respond according to the Midday Check-In Response Guidelines: "
        "acknowledge progress positively if they started, help find a "
        "smaller task if they haven't, or accept a pivot neutrally. "
        "Keep it brief — 1-2 messages max.]"
    )
    return " ".join(parts)


async def mark_checkin_replied(
    call_log_id: int,
    session: AsyncSession,
) -> None:
    """Mark a check-in as consumed so it won't match again."""
    call_log = await session.get(CallLog, call_log_id)
    if call_log is not None and call_log.checkin_replied_at is None:
        call_log.checkin_replied_at = datetime.now(timezone.utc)
        session.add(call_log)
        await session.flush()
