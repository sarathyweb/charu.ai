"""EmailDraftService — draft lifecycle management for Gmail reply approval.

Implements the draft-review-send pipeline state machine:
- ``pending_review`` → ``approved`` → ``sent``
- ``pending_review`` ↔ ``revision_requested``
- any non-terminal → ``abandoned``

Terminal states: ``approved``, ``sent``, ``abandoned``.

Requirements: 18
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.email_draft_state import EmailDraftState
from app.models.enums import DraftStatus
from app.models.user import User
from app.services.gmail_write_service import send_approved_reply

logger = logging.getLogger(__name__)

# Non-terminal statuses — drafts in these states are "active"
_ACTIVE_STATUSES = {
    DraftStatus.PENDING_REVIEW.value,
    DraftStatus.REVISION_REQUESTED.value,
    DraftStatus.APPROVED.value,
}

# Terminal statuses — no outgoing transitions except abandon → abandon (idempotent)
_TERMINAL_STATUSES = {
    DraftStatus.SENT.value,
    DraftStatus.ABANDONED.value,
}

# Valid state transitions (current_status → set of allowed next statuses)
_VALID_TRANSITIONS: dict[str, set[str]] = {
    DraftStatus.PENDING_REVIEW.value: {
        DraftStatus.APPROVED.value,
        DraftStatus.REVISION_REQUESTED.value,
        DraftStatus.ABANDONED.value,
    },
    DraftStatus.REVISION_REQUESTED.value: {
        DraftStatus.PENDING_REVIEW.value,
        DraftStatus.ABANDONED.value,
    },
    DraftStatus.APPROVED.value: {
        DraftStatus.SENT.value,
        DraftStatus.ABANDONED.value,
    },
    # Terminal states — no outgoing transitions
    DraftStatus.SENT.value: set(),
    DraftStatus.ABANDONED.value: set(),
}

MAX_REVISIONS = 5


class EmailDraftService:
    """Manages EmailDraftState lifecycle: create, update, approve, abandon, expire."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # create_draft
    # ------------------------------------------------------------------

    async def create_draft(
        self,
        *,
        user_id: int,
        thread_id: str,
        original_email_id: str,
        original_from: str,
        original_subject: str,
        original_message_id: str,
        draft_text: str,
    ) -> EmailDraftState:
        """Create a new email draft for user review.

        Enforces the partial unique constraint (one active draft per
        user + thread) by abandoning any existing active draft for the
        same thread before inserting.

        Sets ``expires_at = created_at + 2 hours``.
        """
        # Abandon any existing active draft for this user + thread
        existing = await self.get_active_draft(user_id, thread_id)
        if existing is not None:
            existing.status = DraftStatus.ABANDONED.value
            existing.updated_at = datetime.now(timezone.utc)
            self.session.add(existing)
            await self.session.flush()
            logger.info(
                "Abandoned existing draft %s for user %s thread %s (replaced by new draft)",
                existing.id,
                user_id,
                thread_id,
            )

        now = datetime.now(timezone.utc)
        draft = EmailDraftState(
            user_id=user_id,
            thread_id=thread_id,
            original_email_id=original_email_id,
            original_from=original_from,
            original_subject=original_subject,
            original_message_id=original_message_id,
            draft_text=draft_text,
            status=DraftStatus.PENDING_REVIEW.value,
            revision_count=0,
            expires_at=now + timedelta(hours=2),
        )
        self.session.add(draft)
        await self.session.commit()
        await self.session.refresh(draft)
        return draft

    # ------------------------------------------------------------------
    # update_draft
    # ------------------------------------------------------------------

    async def update_draft(
        self,
        draft_id: int,
        new_draft_text: str,
    ) -> EmailDraftState:
        """Update an existing draft after user requests changes.

        Only allowed when status is ``pending_review`` or
        ``revision_requested``. Increments ``revision_count`` and caps
        at ``MAX_REVISIONS`` (5).

        Raises:
            ValueError: If draft not found, in wrong state, or revision
                cap exceeded.
        """
        draft = await self._get_draft_or_raise(draft_id)

        allowed = {DraftStatus.PENDING_REVIEW.value, DraftStatus.REVISION_REQUESTED.value}
        if draft.status not in allowed:
            raise ValueError(
                f"Cannot update draft in '{draft.status}' state. "
                f"Allowed states: {allowed}"
            )

        if draft.revision_count >= MAX_REVISIONS:
            raise ValueError(
                f"Revision cap reached ({MAX_REVISIONS}). "
                "Please approve or abandon this draft."
            )

        draft.draft_text = new_draft_text
        draft.revision_count += 1
        draft.status = DraftStatus.PENDING_REVIEW.value
        draft.updated_at = datetime.now(timezone.utc)
        self.session.add(draft)
        await self.session.commit()
        await self.session.refresh(draft)
        return draft

    # ------------------------------------------------------------------
    # approve_draft
    # ------------------------------------------------------------------

    async def approve_draft(
        self,
        draft_id: int,
        user: User,
    ) -> dict:
        """Approve a draft and send it via Gmail.

        Transitions ``pending_review`` → ``approved``, then delegates to
        ``gmail_write_service.send_approved_reply`` which locks the row,
        calls the Gmail API, and transitions to ``sent`` on success.

        Returns the result dict from ``send_approved_reply``.

        Raises:
            ValueError: If draft not found or not in ``pending_review`` state.
        """
        draft = await self._get_draft_or_raise(draft_id)

        if draft.status != DraftStatus.PENDING_REVIEW.value:
            raise ValueError(
                f"Cannot approve draft in '{draft.status}' state. "
                "Only 'pending_review' drafts can be approved."
            )

        if draft.user_id != user.id:
            raise ValueError("Draft does not belong to this user.")

        # Transition to approved
        draft.status = DraftStatus.APPROVED.value
        draft.updated_at = datetime.now(timezone.utc)
        self.session.add(draft)
        await self.session.flush()

        # Delegate to gmail_write_service which handles row lock + send + sent transition
        result = await send_approved_reply(
            user=user,
            draft_id=draft.id,  # type: ignore[arg-type]
            session=self.session,
        )

        # Commit the final state (sent or error)
        await self.session.commit()
        return result

    # ------------------------------------------------------------------
    # abandon_draft
    # ------------------------------------------------------------------

    async def abandon_draft(self, draft_id: int) -> EmailDraftState:
        """Abandon a draft from any non-terminal state.

        Idempotent: abandoning an already-abandoned draft is a no-op.

        Raises:
            ValueError: If draft not found or already in a terminal state
                other than ``abandoned``.
        """
        draft = await self._get_draft_or_raise(draft_id)

        # Idempotent on already-abandoned
        if draft.status == DraftStatus.ABANDONED.value:
            return draft

        if draft.status in _TERMINAL_STATUSES:
            raise ValueError(
                f"Cannot abandon draft in terminal state '{draft.status}'."
            )

        draft.status = DraftStatus.ABANDONED.value
        draft.updated_at = datetime.now(timezone.utc)
        self.session.add(draft)
        await self.session.commit()
        await self.session.refresh(draft)
        return draft

    # ------------------------------------------------------------------
    # expire_stale_drafts
    # ------------------------------------------------------------------

    async def expire_stale_drafts(self) -> int:
        """Bulk-abandon drafts past their expiry time.

        Targets drafts where ``expires_at < now()`` and status is
        ``pending_review`` or ``revision_requested``.

        Called by Celery Beat every 15 minutes.

        Returns the number of drafts expired.
        """
        now = datetime.now(timezone.utc)
        expirable_statuses = [
            DraftStatus.PENDING_REVIEW.value,
            DraftStatus.REVISION_REQUESTED.value,
        ]

        stmt = (
            update(EmailDraftState)
            .where(
                EmailDraftState.expires_at < now,
                EmailDraftState.status.in_(expirable_statuses),  # type: ignore[union-attr]
            )
            .values(
                status=DraftStatus.ABANDONED.value,
                updated_at=now,
            )
        )
        result = await self.session.exec(stmt)  # type: ignore[arg-type]
        count = result.rowcount  # type: ignore[union-attr]
        await self.session.commit()

        if count:
            logger.info("Expired %d stale email drafts", count)
        return count

    # ------------------------------------------------------------------
    # get_active_draft
    # ------------------------------------------------------------------

    async def get_active_draft(
        self,
        user_id: int,
        thread_id: str,
    ) -> EmailDraftState | None:
        """Return the active (non-terminal) draft for a user + thread, or None."""
        stmt = select(EmailDraftState).where(
            EmailDraftState.user_id == user_id,
            EmailDraftState.thread_id == thread_id,
            EmailDraftState.status.in_(list(_ACTIVE_STATUSES)),  # type: ignore[union-attr]
        )
        result = await self.session.exec(stmt)
        row = result.first()
        if row is None:
            return None
        # session.exec() may return a Row wrapper.  Re-fetch via session.get()
        # to guarantee a proper mapped instance that supports attribute setting.
        draft_id: int = row.id if isinstance(row, EmailDraftState) else row[0].id  # type: ignore[union-attr]
        return await self.session.get(EmailDraftState, draft_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_draft_or_raise(self, draft_id: int) -> EmailDraftState:
        """Fetch a draft by ID or raise ValueError."""
        draft = await self.session.get(EmailDraftState, draft_id)
        if draft is None:
            raise ValueError(f"Email draft {draft_id} not found.")
        return draft
