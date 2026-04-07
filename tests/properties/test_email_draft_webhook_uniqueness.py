"""Property tests for email draft uniqueness via the webhook integration path (Property 41).

Property 41 — Active email draft uniqueness and expiry:
  Covered by task 8.14 at the service layer; this task validates the
  **webhook integration path** — specifically that when the WhatsApp
  webhook processes email draft approval/abandon/revise flows, the
  draft uniqueness invariant is maintained.

Validates: Requirements 18
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from hypothesis import HealthCheck, assume, given, settings, strategies as st
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.email_draft_state import EmailDraftState
from app.models.enums import DraftStatus
from app.models.user import User
from app.services.draft_context import (
    DraftContext,
    DraftIntent,
    classify_draft_intent,
    find_pending_draft,
)
from app.services.email_draft_service import EmailDraftService

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_thread_ids = st.text(min_size=1, max_size=30, alphabet="abcdefghijklmnop0123456789_")
_draft_texts = st.text(
    min_size=1,
    max_size=500,
    alphabet=st.characters(categories=("L", "N", "P", "Z"), exclude_characters="\x00"),
)

_approval_signals = st.sampled_from([
    "send it", "send", "yes", "yep", "yeah", "yea",
    "looks good", "look good", "go ahead", "approve",
    "perfect", "lgtm", "ok", "okay", "sure", "do it",
    "ship it", "👍", "✅",
])

_abandon_signals = st.sampled_from([
    "cancel", "never mind", "nevermind", "skip", "skip it",
    "don't send", "dont send", "forget it", "forget about it",
    "nah", "no", "abandon", "drop it", "drop", "❌",
])

_revise_signals = st.text(
    min_size=5,
    max_size=100,
    alphabet=st.characters(categories=("L", "N", "Z"), exclude_characters="\x00"),
).filter(lambda t: not any(
    t.strip().lower().startswith(prefix)
    for prefix in [
        "send", "yes", "yep", "yeah", "yea", "look", "go ahead",
        "approve", "perfect", "lgtm", "ok", "okay", "sure", "do it",
        "ship it", "👍", "✅",
        "cancel", "never", "nevermind", "skip", "don't", "dont",
        "forget", "nah", "no", "abandon", "drop", "❌",
    ]
))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_phone_counter = 0


async def _create_user(session: AsyncSession) -> User:
    global _phone_counter
    _phone_counter += 1
    user = User(
        phone=f"+1555900{_phone_counter:04d}",
        timezone="America/New_York",
        onboarding_complete=True,
        consecutive_active_days=0,
    )
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return user


async def _count_active_drafts(
    session: AsyncSession,
    user_id: int,
    thread_id: str,
) -> int:
    """Count non-terminal drafts for a user+thread."""
    active = [
        DraftStatus.PENDING_REVIEW.value,
        DraftStatus.REVISION_REQUESTED.value,
        DraftStatus.APPROVED.value,
    ]
    result = await session.exec(
        select(EmailDraftState).where(
            EmailDraftState.user_id == user_id,
            EmailDraftState.thread_id == thread_id,
            EmailDraftState.status.in_(active),  # type: ignore[union-attr]
        )
    )
    return len(result.all())


async def _create_draft_via_service(
    session: AsyncSession,
    user_id: int,
    thread_id: str,
    draft_text: str = "Test draft",
) -> EmailDraftState:
    """Create a draft using the service (enforces uniqueness invariant)."""
    svc = EmailDraftService(session)
    return await svc.create_draft(
        user_id=user_id,
        thread_id=thread_id,
        original_email_id="msg_test",
        original_from="test@example.com",
        original_subject="Test Subject",
        original_message_id="<test@example.com>",
        draft_text=draft_text,
    )


# ---------------------------------------------------------------------------
# P41-WH-a: find_pending_draft returns at most one draft
# ---------------------------------------------------------------------------


@given(
    thread_id=_thread_ids,
    draft_text_1=_draft_texts,
    draft_text_2=_draft_texts,
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=20,
)
@pytest.mark.asyncio
async def test_find_pending_draft_returns_at_most_one(
    thread_id: str,
    draft_text_1: str,
    draft_text_2: str,
    session: AsyncSession,
):
    """For any user, find_pending_draft returns at most one pending draft
    (the most recent one), ensuring the webhook always operates on the
    correct draft.

    **Validates: Requirements 18**
    """
    user = await _create_user(session)

    # Create two drafts for the same user+thread — second should abandon first
    await _create_draft_via_service(session, user.id, thread_id, draft_text_1)
    second = await _create_draft_via_service(session, user.id, thread_id, draft_text_2)

    ctx = await find_pending_draft(user.id, session)

    # find_pending_draft must return exactly one result (the latest)
    assert ctx is not None, "Should find a pending draft"
    assert ctx.draft_id == second.id, "Should return the most recent draft"
    assert ctx.draft_text == draft_text_2


# ---------------------------------------------------------------------------
# P41-WH-b: classify_draft_intent correctly classifies messages
# ---------------------------------------------------------------------------


@given(signal=_approval_signals)
@settings(max_examples=30)
@pytest.mark.asyncio
async def test_classify_draft_intent_approve(signal: str):
    """Approval signals map to DraftIntent.APPROVE.

    **Validates: Requirements 18**
    """
    assert classify_draft_intent(signal) == DraftIntent.APPROVE


@given(signal=_abandon_signals)
@settings(max_examples=30)
@pytest.mark.asyncio
async def test_classify_draft_intent_abandon(signal: str):
    """Abandonment signals map to DraftIntent.ABANDON.

    **Validates: Requirements 18**
    """
    assert classify_draft_intent(signal) == DraftIntent.ABANDON


@given(signal=_revise_signals)
@settings(max_examples=30)
@pytest.mark.asyncio
async def test_classify_draft_intent_revise(signal: str):
    """Other text maps to DraftIntent.REVISE.

    **Validates: Requirements 18**
    """
    assert classify_draft_intent(signal) == DraftIntent.REVISE


# ---------------------------------------------------------------------------
# P41-WH-c: Approve via webhook path maintains uniqueness
# ---------------------------------------------------------------------------


@given(
    thread_id=_thread_ids,
    draft_text=_draft_texts,
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=20,
)
@pytest.mark.asyncio
async def test_approve_via_webhook_path_maintains_uniqueness(
    thread_id: str,
    draft_text: str,
    session: AsyncSession,
):
    """After approving a draft through the webhook path
    (find_pending_draft → classify_draft_intent → approve_draft),
    no active drafts remain for that user+thread.

    **Validates: Requirements 18**
    """
    user = await _create_user(session)
    draft = await _create_draft_via_service(session, user.id, thread_id, draft_text)

    # Simulate the webhook path: find → classify → approve
    ctx = await find_pending_draft(user.id, session)
    assert ctx is not None
    intent = classify_draft_intent("send it")
    assert intent == DraftIntent.APPROVE

    svc = EmailDraftService(session)

    async def _fake_send_approved_reply(*, user, draft_id, session):
        """Mock that simulates send_approved_reply: transitions draft to SENT."""
        d = await session.get(EmailDraftState, draft_id)
        if d is not None:
            d.status = DraftStatus.SENT.value
            d.updated_at = datetime.now(timezone.utc)
            session.add(d)
            await session.flush()
        return {"status": "sent"}

    with patch("app.services.email_draft_service.send_approved_reply", side_effect=_fake_send_approved_reply):
        await svc.approve_draft(ctx.draft_id, user)

    # After approval, no active drafts should remain
    count = await _count_active_drafts(session, user.id, thread_id)
    assert count == 0, f"Expected 0 active drafts after approval, got {count}"


# ---------------------------------------------------------------------------
# P41-WH-d: Abandon via webhook path maintains uniqueness
# ---------------------------------------------------------------------------


@given(
    thread_id=_thread_ids,
    draft_text=_draft_texts,
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=20,
)
@pytest.mark.asyncio
async def test_abandon_via_webhook_path_maintains_uniqueness(
    thread_id: str,
    draft_text: str,
    session: AsyncSession,
):
    """After abandoning a draft through the webhook path
    (find_pending_draft → classify_draft_intent → abandon_draft),
    no active drafts remain for that user+thread.

    **Validates: Requirements 18**
    """
    user = await _create_user(session)
    await _create_draft_via_service(session, user.id, thread_id, draft_text)

    # Simulate the webhook path: find → classify → abandon
    ctx = await find_pending_draft(user.id, session)
    assert ctx is not None
    intent = classify_draft_intent("cancel")
    assert intent == DraftIntent.ABANDON

    svc = EmailDraftService(session)
    await svc.abandon_draft(ctx.draft_id)

    # After abandonment, no active drafts should remain
    count = await _count_active_drafts(session, user.id, thread_id)
    assert count == 0, f"Expected 0 active drafts after abandon, got {count}"


# ---------------------------------------------------------------------------
# P41-WH-e: Sequential webhook operations maintain at-most-one active draft
# ---------------------------------------------------------------------------


@given(
    thread_id=_thread_ids,
    num_drafts=st.integers(min_value=2, max_value=5),
    action=st.sampled_from(["approve", "abandon"]),
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=15,
)
@pytest.mark.asyncio
async def test_sequential_webhook_ops_maintain_at_most_one(
    thread_id: str,
    num_drafts: int,
    action: str,
    session: AsyncSession,
):
    """Creating multiple drafts for the same user+thread, then processing
    a webhook action, always results in at most one active draft before
    the action and zero after.

    **Validates: Requirements 18**
    """
    user = await _create_user(session)
    svc = EmailDraftService(session)

    # Create multiple drafts sequentially — each should abandon the previous
    for i in range(num_drafts):
        await svc.create_draft(
            user_id=user.id,
            thread_id=thread_id,
            original_email_id=f"msg_{i}",
            original_from=f"sender{i}@example.com",
            original_subject=f"Subject {i}",
            original_message_id=f"<{i}@example.com>",
            draft_text=f"Draft text {i}",
        )

    # Before action: exactly one active draft
    count_before = await _count_active_drafts(session, user.id, thread_id)
    assert count_before == 1, f"Expected 1 active draft before action, got {count_before}"

    # Find the pending draft via webhook path
    ctx = await find_pending_draft(user.id, session)
    assert ctx is not None

    # Process the action
    if action == "approve":
        async def _fake_send(*, user, draft_id, session):
            d = await session.get(EmailDraftState, draft_id)
            if d is not None:
                d.status = DraftStatus.SENT.value
                d.updated_at = datetime.now(timezone.utc)
                session.add(d)
                await session.flush()
            return {"status": "sent"}

        with patch("app.services.email_draft_service.send_approved_reply", side_effect=_fake_send):
            await svc.approve_draft(ctx.draft_id, user)
    else:
        await svc.abandon_draft(ctx.draft_id)

    # After action: zero active drafts
    count_after = await _count_active_drafts(session, user.id, thread_id)
    assert count_after == 0, f"Expected 0 active drafts after {action}, got {count_after}"
