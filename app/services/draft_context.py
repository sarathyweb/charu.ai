"""Email draft approval detection for WhatsApp webhook.

Detects when an inbound WhatsApp message is a response to a pending
email draft review, classifies the user's intent (approve, revise,
abandon), and delegates to ``EmailDraftService``.

A message is considered a draft reply when the user has an active
``EmailDraftState`` in ``pending_review`` or ``revision_requested``
status.

Design references:
  - Design §EmailDraftState (approval workflow)
  - Research 32: Gmail Write Access
  - Requirements 18
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum

from sqlalchemy import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.email_draft_state import EmailDraftState
from app.models.enums import DraftStatus

logger = logging.getLogger(__name__)


class DraftIntent(str, Enum):
    """Classified user intent for a draft reply."""

    APPROVE = "approve"
    REVISE = "revise"
    ABANDON = "abandon"


@dataclass(frozen=True)
class DraftContext:
    """Context from a pending email draft for webhook handling."""

    draft_id: int
    user_id: int
    thread_id: str
    original_from: str
    original_subject: str
    draft_text: str
    status: str


# ---------------------------------------------------------------------------
# Approval / abandonment signal patterns
# ---------------------------------------------------------------------------

_APPROVAL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^send\s*it\b", re.IGNORECASE),
    re.compile(r"^send$", re.IGNORECASE),
    re.compile(r"^yes$", re.IGNORECASE),
    re.compile(r"^yep$", re.IGNORECASE),
    re.compile(r"^yeah$", re.IGNORECASE),
    re.compile(r"^yea$", re.IGNORECASE),
    re.compile(r"^looks?\s*good", re.IGNORECASE),
    re.compile(r"^go\s*ahead", re.IGNORECASE),
    re.compile(r"^approve", re.IGNORECASE),
    re.compile(r"^perfect", re.IGNORECASE),
    re.compile(r"^lgtm", re.IGNORECASE),
    re.compile(r"^ok\b", re.IGNORECASE),
    re.compile(r"^okay\b", re.IGNORECASE),
    re.compile(r"^sure\b", re.IGNORECASE),
    re.compile(r"^do\s*it\b", re.IGNORECASE),
    re.compile(r"^ship\s*it\b", re.IGNORECASE),
    re.compile(r"^👍", re.IGNORECASE),
    re.compile(r"^✅", re.IGNORECASE),
]

_ABANDON_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^cancel", re.IGNORECASE),
    re.compile(r"^never\s*mind", re.IGNORECASE),
    re.compile(r"^nevermind", re.IGNORECASE),
    re.compile(r"^skip\s*(it|this)?$", re.IGNORECASE),
    re.compile(r"^don'?t\s*send", re.IGNORECASE),
    re.compile(r"^forget\s*(it|about\s*it)?$", re.IGNORECASE),
    re.compile(r"^nah$", re.IGNORECASE),
    re.compile(r"^no$", re.IGNORECASE),
    re.compile(r"^no\s*thanks", re.IGNORECASE),
    re.compile(r"^no\s*,?\s*don'?t", re.IGNORECASE),
    re.compile(r"^nah\s*,?\s*forget", re.IGNORECASE),
    re.compile(r"^abandon", re.IGNORECASE),
    re.compile(r"^drop\s*(it)?$", re.IGNORECASE),
    re.compile(r"^❌", re.IGNORECASE),
]


def classify_draft_intent(body: str) -> DraftIntent:
    """Classify the user's intent from their WhatsApp message text.

    Returns ``APPROVE`` for approval signals, ``ABANDON`` for
    abandonment signals, and ``REVISE`` for everything else (the
    message is treated as revision instructions).
    """
    text = body.strip()

    for pattern in _APPROVAL_PATTERNS:
        if pattern.search(text):
            return DraftIntent.APPROVE

    for pattern in _ABANDON_PATTERNS:
        if pattern.search(text):
            return DraftIntent.ABANDON

    # Default: treat as revision instructions
    return DraftIntent.REVISE


async def find_pending_draft(
    user_id: int,
    session: AsyncSession,
) -> DraftContext | None:
    """Return draft context if the user has an active pending draft.

    An "active pending" draft is one in ``pending_review`` or
    ``revision_requested`` status.

    Returns ``None`` if no pending draft is found.
    """
    stmt = (
        select(EmailDraftState)
        .where(
            EmailDraftState.user_id == user_id,
            EmailDraftState.status.in_(  # type: ignore[union-attr]
                [
                    DraftStatus.PENDING_REVIEW.value,
                    DraftStatus.REVISION_REQUESTED.value,
                ]
            ),
        )
        .order_by(EmailDraftState.created_at.desc())  # type: ignore[union-attr]
        .limit(1)
    )

    result = await session.exec(stmt)
    row = result.first()

    if row is None:
        return None

    # session.exec() may return a Row wrapper — unwrap via session.get()
    if isinstance(row, EmailDraftState):
        draft = row
    else:
        draft_id: int = row[0].id if hasattr(row, '__getitem__') else row.id  # type: ignore[union-attr]
        fetched = await session.get(EmailDraftState, draft_id)
        if fetched is None:
            return None
        draft = fetched

    return DraftContext(
        draft_id=draft.id,  # type: ignore[arg-type]
        user_id=draft.user_id,
        thread_id=draft.thread_id,
        original_from=draft.original_from,
        original_subject=draft.original_subject,
        draft_text=draft.draft_text,
        status=draft.status,
    )
