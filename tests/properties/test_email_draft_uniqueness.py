"""Property tests for email draft uniqueness and expiry (Property 41).

Property 41 — Active email draft uniqueness and expiry:
  *For any* user and thread, at most one email draft is in a non-terminal
  state (``pending_review``, ``revision_requested``, or ``approved``) at a
  time.  Creating a new draft for the same user+thread abandons the
  existing active draft.  Drafts older than 2 hours in a non-terminal
  state are automatically marked ``abandoned``.

Validates: Requirements 18
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.email_draft_state import EmailDraftState
from app.models.enums import DraftStatus
from app.models.user import User
from app.services.email_draft_service import EmailDraftService

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_thread_ids = st.text(min_size=1, max_size=30, alphabet="abcdefghijklmnop0123456789_")
_email_ids = st.text(min_size=1, max_size=30, alphabet="abcdefghijklmnop0123456789_")
_subjects = st.text(min_size=1, max_size=100, alphabet=st.characters(categories=("L", "N", "P", "Z"), exclude_characters="\x00"))
_draft_texts = st.text(min_size=1, max_size=500, alphabet=st.characters(categories=("L", "N", "P", "Z"), exclude_characters="\x00"))
_from_addrs = st.builds(
    lambda name: f"{name}@example.com",
    name=st.text(min_size=1, max_size=20, alphabet="abcdefghijklmnop"),
)
_message_ids = st.builds(
    lambda tag: f"<{tag}@mail.example.com>",
    tag=st.text(min_size=5, max_size=20, alphabet="abcdefghijklmnop0123456789"),
)

_active_statuses = st.sampled_from([
    DraftStatus.PENDING_REVIEW.value,
    DraftStatus.REVISION_REQUESTED.value,
    DraftStatus.APPROVED.value,
])

_non_terminal_statuses = st.sampled_from([
    DraftStatus.PENDING_REVIEW.value,
    DraftStatus.REVISION_REQUESTED.value,
])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_phone_counter = 0


async def _create_user(session: AsyncSession) -> User:
    global _phone_counter
    _phone_counter += 1
    user = User(
        phone=f"+1555800{_phone_counter:04d}",
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


async def _create_draft_raw(
    session: AsyncSession,
    user_id: int,
    thread_id: str,
    status: str,
    created_at: datetime,
    expires_at: datetime | None = None,
) -> EmailDraftState:
    """Insert a draft directly (bypassing service) for setup purposes."""
    draft = EmailDraftState(
        user_id=user_id,
        thread_id=thread_id,
        original_email_id="msg_raw",
        original_from="raw@example.com",
        original_subject="Raw Subject",
        original_message_id="<raw@example.com>",
        draft_text="Raw draft text",
        status=status,
        revision_count=0,
    )
    draft.created_at = created_at
    draft.expires_at = expires_at
    session.add(draft)
    await session.flush()
    await session.refresh(draft)
    return draft


# ---------------------------------------------------------------------------
# P41a: Creating a draft yields exactly one active draft per user+thread
# ---------------------------------------------------------------------------


@given(
    thread_id=_thread_ids,
    email_id=_email_ids,
    subject=_subjects,
    draft_text=_draft_texts,
    from_addr=_from_addrs,
    message_id=_message_ids,
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=30,
)
@pytest.mark.asyncio
async def test_create_draft_yields_one_active(
    thread_id: str,
    email_id: str,
    subject: str,
    draft_text: str,
    from_addr: str,
    message_id: str,
    session: AsyncSession,
):
    """After creating a draft, exactly one active draft exists for that
    user+thread combination."""
    user = await _create_user(session)
    svc = EmailDraftService(session)

    draft = await svc.create_draft(
        user_id=user.id,
        thread_id=thread_id,
        original_email_id=email_id,
        original_from=from_addr,
        original_subject=subject,
        original_message_id=message_id,
        draft_text=draft_text,
    )

    assert draft.status == DraftStatus.PENDING_REVIEW.value
    count = await _count_active_drafts(session, user.id, thread_id)
    assert count == 1, f"Expected 1 active draft, got {count}"


# ---------------------------------------------------------------------------
# P41b: Creating a second draft abandons the first
# ---------------------------------------------------------------------------


@given(
    thread_id=_thread_ids,
    draft_text_1=_draft_texts,
    draft_text_2=_draft_texts,
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=30,
)
@pytest.mark.asyncio
async def test_second_draft_abandons_first(
    thread_id: str,
    draft_text_1: str,
    draft_text_2: str,
    session: AsyncSession,
):
    """Creating a new draft for the same user+thread abandons the previous
    active draft, leaving exactly one active."""
    user = await _create_user(session)
    svc = EmailDraftService(session)

    first = await svc.create_draft(
        user_id=user.id,
        thread_id=thread_id,
        original_email_id="msg_1",
        original_from="a@example.com",
        original_subject="Subject 1",
        original_message_id="<1@example.com>",
        draft_text=draft_text_1,
    )
    first_id = first.id

    second = await svc.create_draft(
        user_id=user.id,
        thread_id=thread_id,
        original_email_id="msg_2",
        original_from="b@example.com",
        original_subject="Subject 2",
        original_message_id="<2@example.com>",
        draft_text=draft_text_2,
    )

    # Refresh the first draft to see its updated status
    await session.refresh(first)
    refreshed_first = await session.get(EmailDraftState, first_id)

    assert refreshed_first is not None
    assert refreshed_first.status == DraftStatus.ABANDONED.value, (
        f"First draft should be abandoned, got {refreshed_first.status}"
    )
    assert second.status == DraftStatus.PENDING_REVIEW.value

    count = await _count_active_drafts(session, user.id, thread_id)
    assert count == 1, f"Expected 1 active draft after replacement, got {count}"


# ---------------------------------------------------------------------------
# P41c: Different threads can each have one active draft
# ---------------------------------------------------------------------------


@given(
    thread_id_a=_thread_ids,
    thread_id_b=_thread_ids,
    draft_text=_draft_texts,
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=20,
)
@pytest.mark.asyncio
async def test_different_threads_independent(
    thread_id_a: str,
    thread_id_b: str,
    draft_text: str,
    session: AsyncSession,
):
    """Active drafts on different threads are independent — creating a
    draft on thread B does not affect thread A's active draft."""
    from hypothesis import assume

    assume(thread_id_a != thread_id_b)

    user = await _create_user(session)
    svc = EmailDraftService(session)

    draft_a = await svc.create_draft(
        user_id=user.id,
        thread_id=thread_id_a,
        original_email_id="msg_a",
        original_from="a@example.com",
        original_subject="Subject A",
        original_message_id="<a@example.com>",
        draft_text=draft_text,
    )

    draft_b = await svc.create_draft(
        user_id=user.id,
        thread_id=thread_id_b,
        original_email_id="msg_b",
        original_from="b@example.com",
        original_subject="Subject B",
        original_message_id="<b@example.com>",
        draft_text=draft_text,
    )

    # Both should be active
    await session.refresh(draft_a)
    assert draft_a.status == DraftStatus.PENDING_REVIEW.value
    assert draft_b.status == DraftStatus.PENDING_REVIEW.value

    count_a = await _count_active_drafts(session, user.id, thread_id_a)
    count_b = await _count_active_drafts(session, user.id, thread_id_b)
    assert count_a == 1, f"Thread A should have 1 active draft, got {count_a}"
    assert count_b == 1, f"Thread B should have 1 active draft, got {count_b}"


# ---------------------------------------------------------------------------
# P41d: Expiry abandons stale non-terminal drafts (2-hour threshold)
# ---------------------------------------------------------------------------


@given(
    status=_non_terminal_statuses,
    hours_extra=st.integers(min_value=1, max_value=48),
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=30,
)
@pytest.mark.asyncio
async def test_expiry_abandons_stale_drafts(
    status: str,
    hours_extra: int,
    session: AsyncSession,
):
    """Non-terminal drafts whose expires_at has passed are abandoned by
    expire_stale_drafts."""
    now = datetime.now(timezone.utc)
    user = await _create_user(session)

    expired_at = now - timedelta(hours=hours_extra)
    created = expired_at - timedelta(hours=2)

    draft = await _create_draft_raw(
        session, user.id, "thread_stale", status,
        created_at=created, expires_at=expired_at,
    )
    draft_id = draft.id

    svc = EmailDraftService(session)
    count = await svc.expire_stale_drafts()

    assert count >= 1, "At least one draft should have been expired"

    row = await session.get(EmailDraftState, draft_id)
    assert row is not None
    assert row.status == DraftStatus.ABANDONED.value, (
        f"Stale {status} draft should be abandoned, got {row.status}"
    )


# ---------------------------------------------------------------------------
# P41e: Expiry preserves fresh non-terminal drafts
# ---------------------------------------------------------------------------


@given(status=_non_terminal_statuses)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=15,
)
@pytest.mark.asyncio
async def test_expiry_preserves_fresh_drafts(
    status: str,
    session: AsyncSession,
):
    """Non-terminal drafts whose expires_at is in the future are not
    affected by expire_stale_drafts."""
    now = datetime.now(timezone.utc)
    user = await _create_user(session)

    draft = await _create_draft_raw(
        session, user.id, "thread_fresh", status,
        created_at=now - timedelta(minutes=30),
        expires_at=now + timedelta(hours=1),
    )
    draft_id = draft.id

    svc = EmailDraftService(session)
    await svc.expire_stale_drafts()

    row = await session.get(EmailDraftState, draft_id)
    assert row is not None
    assert row.status == status, (
        f"Fresh {status} draft should be preserved, got {row.status}"
    )


# ---------------------------------------------------------------------------
# P41f: Expiry never touches terminal drafts
# ---------------------------------------------------------------------------


@given(
    status=st.sampled_from([DraftStatus.SENT.value, DraftStatus.ABANDONED.value]),
    hours_extra=st.integers(min_value=1, max_value=48),
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=15,
)
@pytest.mark.asyncio
async def test_expiry_ignores_terminal_drafts(
    status: str,
    hours_extra: int,
    session: AsyncSession,
):
    """Terminal drafts (sent, abandoned) are never modified by expiry,
    even if they are old."""
    now = datetime.now(timezone.utc)
    user = await _create_user(session)

    old_time = now - timedelta(hours=hours_extra + 2)
    draft = await _create_draft_raw(
        session, user.id, "thread_terminal", status,
        created_at=old_time, expires_at=old_time,
    )
    draft_id = draft.id

    svc = EmailDraftService(session)
    await svc.expire_stale_drafts()

    row = await session.get(EmailDraftState, draft_id)
    assert row is not None
    assert row.status == status, (
        f"Terminal {status} draft should not be modified, got {row.status}"
    )


# ---------------------------------------------------------------------------
# P41g: get_active_draft returns None after expiry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_active_draft_after_expiry(session: AsyncSession):
    """After expiring a stale draft, get_active_draft returns None for
    that user+thread."""
    now = datetime.now(timezone.utc)
    user = await _create_user(session)
    thread_id = "thread_expired_check"

    await _create_draft_raw(
        session, user.id, thread_id,
        DraftStatus.PENDING_REVIEW.value,
        created_at=now - timedelta(hours=3),
        expires_at=now - timedelta(hours=1),
    )

    svc = EmailDraftService(session)
    await svc.expire_stale_drafts()

    active = await svc.get_active_draft(user.id, thread_id)
    assert active is None, "No active draft should remain after expiry"


# ---------------------------------------------------------------------------
# P41h: create_draft sets expires_at to ~2 hours from creation
# ---------------------------------------------------------------------------


@given(draft_text=_draft_texts)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=10,
)
@pytest.mark.asyncio
async def test_create_draft_sets_expiry(
    draft_text: str,
    session: AsyncSession,
):
    """Newly created drafts have expires_at set to approximately 2 hours
    from creation time."""
    user = await _create_user(session)
    svc = EmailDraftService(session)

    before = datetime.now(timezone.utc)
    draft = await svc.create_draft(
        user_id=user.id,
        thread_id="thread_expiry_check",
        original_email_id="msg_exp",
        original_from="exp@example.com",
        original_subject="Expiry Check",
        original_message_id="<exp@example.com>",
        draft_text=draft_text,
    )
    after = datetime.now(timezone.utc)

    assert draft.expires_at is not None, "expires_at should be set"
    expected_min = before + timedelta(hours=2) - timedelta(seconds=5)
    expected_max = after + timedelta(hours=2) + timedelta(seconds=5)
    assert expected_min <= draft.expires_at <= expected_max, (
        f"expires_at {draft.expires_at} should be ~2h from creation "
        f"(expected between {expected_min} and {expected_max})"
    )
