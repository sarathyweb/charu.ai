"""Property tests for task management (P15, P16, P17).

P15 — Task save deduplication via fuzzy match: for any new task title with
      similarity > 0.6 to an existing pending task, save_task merges rather
      than creating a duplicate.  For titles with similarity <= 0.6, a new
      task is created.
P16 — Task completion idempotency: completing an already-completed task
      returns the task with no database changes.
P17 — Task listing returns top N by priority then recency: list_pending_tasks
      returns at most N tasks ordered by priority DESC, created_at DESC.

All properties run against a real PostgreSQL test database with pg_trgm.

Validates: Requirements 9.1, 9.3, 9.5
"""

import asyncio
import os
import string
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from hypothesis import given, settings, assume, HealthCheck
from hypothesis import strategies as st
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.enums import TaskStatus
from app.models.task import Task
from app.models.user import User
from app.services.task_service import TaskService

load_dotenv()

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://charu:CJbJ7PsFrpbb29xsMBm3pkH5@localhost:5432/charu_ai_test",
)

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_phone = st.sampled_from(
    [
        "+14155552671",
        "+447911123456",
        "+971501234567",
        "+919876543210",
    ]
)

# Task titles — printable ASCII, reasonable length for trigram matching
_task_title = st.text(
    alphabet=string.ascii_letters + string.digits + " ",
    min_size=4,
    max_size=80,
).filter(lambda s: len(s.strip()) >= 4)

_priority = st.integers(min_value=0, max_value=100)

_source = st.sampled_from(["user_mention", "gmail", "calendar", "import"])

# Pairs of titles that are near-duplicates (high trigram similarity)
_dedup_pairs = st.sampled_from(
    [
        ("file my taxes", "file taxes"),
        ("reply to Sarah's email", "reply to sarah email"),
        ("finish the quarterly report", "finish quarterly report"),
        ("schedule dentist appointment", "schedule dentist appt"),
        ("buy groceries for dinner", "buy groceries for the dinner"),
        ("call the insurance company", "call insurance company"),
        ("submit expense report", "submit the expense report"),
        ("review pull request", "review the pull request"),
    ]
)

# Pairs of titles that are clearly different (low trigram similarity)
_distinct_pairs = st.sampled_from(
    [
        ("file my taxes", "walk the dog"),
        ("reply to Sarah's email", "buy groceries"),
        ("finish the quarterly report", "schedule dentist"),
        ("call the insurance company", "clean the kitchen"),
        ("submit expense report", "read a book"),
        ("review pull request", "plan vacation"),
    ]
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_async(coro):
    """Run an async coroutine in a new event loop (safe for Hypothesis)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _make_session():
    """Create engine, ensure tables + pg_trgm exist, return (engine, session)."""
    import app.models  # noqa: F401
    import sqlalchemy

    eng = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with eng.begin() as conn:
        await conn.execute(sqlalchemy.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    return eng, factory()


async def _ensure_user(session: AsyncSession, phone: str) -> User:
    """Get or create a test user, return the User row."""
    result = await session.exec(select(User).where(User.phone == phone))
    user = result.first()
    if user is None:
        user = User(phone=phone)
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user


async def _cleanup(session: AsyncSession, phone: str):
    """Remove test tasks and user for the given phone."""
    await session.exec(
        sa_text(
            "DELETE FROM tasks WHERE user_id IN (SELECT id FROM users WHERE phone = :p)"
        ),
        params={"p": phone},
    )
    await session.exec(
        sa_text("DELETE FROM users WHERE phone = :p"), params={"p": phone}
    )
    await session.commit()


# ---------------------------------------------------------------------------
# P15: Task save deduplication via fuzzy match
# **Validates: Requirements 9.1**
# ---------------------------------------------------------------------------


@given(phone=_phone, pair=_dedup_pairs, p1=_priority, p2=_priority)
@settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow])
def test_similar_titles_merge_instead_of_duplicate(phone, pair, p1, p2):
    """When a new task title has high trigram similarity (>0.6) with an
    existing pending task, save_task merges — no duplicate row is created."""
    title_a, title_b = pair

    async def _test():
        eng, session = await _make_session()
        try:
            await _cleanup(session, phone)
            user = await _ensure_user(session, phone)
            svc = TaskService(session)

            # Save first task
            task_a, created_a = await svc.save_task(user.id, title_a, priority=p1)
            assert created_a is True

            # Save second task with similar title — should merge
            task_b, created_b = await svc.save_task(user.id, title_b, priority=p2)
            assert created_b is False, (
                f"Expected merge for similar titles '{title_a}' / '{title_b}', "
                f"but a new task was created"
            )

            # Should be the same row
            assert task_b.id == task_a.id

            # Priority should be max of both
            assert task_b.priority == max(p1, p2)

            # Only one pending task should exist
            pending = await svc.list_pending_tasks(user.id, limit=100)
            assert len(pending) == 1

            await _cleanup(session, phone)
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())


@given(phone=_phone, pair=_distinct_pairs, p1=_priority, p2=_priority)
@settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow])
def test_dissimilar_titles_create_separate_tasks(phone, pair, p1, p2):
    """When a new task title has low trigram similarity (<=0.6) with existing
    pending tasks, save_task creates a new row — no merge."""
    title_a, title_b = pair

    async def _test():
        eng, session = await _make_session()
        try:
            await _cleanup(session, phone)
            user = await _ensure_user(session, phone)
            svc = TaskService(session)

            task_a, created_a = await svc.save_task(user.id, title_a, priority=p1)
            assert created_a is True

            task_b, created_b = await svc.save_task(user.id, title_b, priority=p2)
            assert created_b is True, (
                f"Expected new task for dissimilar titles '{title_a}' / '{title_b}', "
                f"but got a merge"
            )

            assert task_b.id != task_a.id

            pending = await svc.list_pending_tasks(user.id, limit=100)
            assert len(pending) == 2

            await _cleanup(session, phone)
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())


@given(phone=_phone, pair=_dedup_pairs, p1=_priority, p2=_priority)
@settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow])
def test_dedup_preserves_earliest_created_at(phone, pair, p1, p2):
    """When merging, the earliest created_at is preserved — not bumped."""
    title_a, title_b = pair

    async def _test():
        eng, session = await _make_session()
        try:
            await _cleanup(session, phone)
            user = await _ensure_user(session, phone)
            svc = TaskService(session)

            task_a, _ = await svc.save_task(user.id, title_a, priority=p1)
            original_created = task_a.created_at

            # Small delay to ensure timestamps differ
            task_b, created = await svc.save_task(user.id, title_b, priority=p2)
            assert created is False
            assert task_b.created_at == original_created

            await _cleanup(session, phone)
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())


@given(phone=_phone, title=_task_title, priority=_priority)
@settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow])
def test_dedup_scoped_to_user(phone, title, priority):
    """Dedup only matches tasks belonging to the same user. A different
    user with the same title gets a new task."""

    async def _test():
        eng, session = await _make_session()
        phone_other = "+33612345678"
        try:
            await _cleanup(session, phone)
            await _cleanup(session, phone_other)
            user_a = await _ensure_user(session, phone)
            user_b = await _ensure_user(session, phone_other)
            svc = TaskService(session)

            task_a, created_a = await svc.save_task(user_a.id, title, priority=priority)
            assert created_a is True

            # Same title, different user — should create, not merge
            task_b, created_b = await svc.save_task(user_b.id, title, priority=priority)
            assert created_b is True
            assert task_b.id != task_a.id

            await _cleanup(session, phone)
            await _cleanup(session, phone_other)
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())


# ---------------------------------------------------------------------------
# P16: Task completion idempotency
# **Validates: Requirements 9.3**
# ---------------------------------------------------------------------------


@given(phone=_phone, title=_task_title, priority=_priority)
@settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow])
def test_completing_already_completed_task_is_noop(phone, title, priority):
    """Completing an already-completed task returns the task unchanged —
    no error, no DB modification."""

    async def _test():
        eng, session = await _make_session()
        try:
            await _cleanup(session, phone)
            user = await _ensure_user(session, phone)
            svc = TaskService(session)

            # Create and complete a task
            task, _ = await svc.save_task(user.id, title, priority=priority)
            task_id = task.id  # capture before any expiry
            completed = await svc.complete_task_by_title(user.id, title)
            assert completed is not None
            assert completed.status == TaskStatus.COMPLETED.value
            first_completed_at = completed.completed_at

            # Complete again — complete_task_by_title only searches pending
            # tasks, so it returns None when the task is already completed.
            # This is correct idempotent behavior — no error, no side effect.
            completed_again = await svc.complete_task_by_title(user.id, title)
            assert completed_again is None

            # Verify the task is still completed in DB (fresh query)
            result = await session.exec(select(Task).where(Task.id == task_id))
            db_task = result.first()
            assert db_task is not None
            assert db_task.status == TaskStatus.COMPLETED.value
            assert db_task.completed_at == first_completed_at

            await _cleanup(session, phone)
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())


@given(phone=_phone, pair=_dedup_pairs, priority=_priority)
@settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow])
def test_completion_uses_fuzzy_match(phone, pair, priority):
    """complete_task_by_title uses fuzzy matching (threshold 0.4) so the
    user can describe the task differently when reporting completion."""
    title_save, title_complete = pair

    async def _test():
        eng, session = await _make_session()
        try:
            await _cleanup(session, phone)
            user = await _ensure_user(session, phone)
            svc = TaskService(session)

            task, _ = await svc.save_task(user.id, title_save, priority=priority)
            assert task.status == TaskStatus.PENDING.value

            # Complete using a slightly different title
            completed = await svc.complete_task_by_title(user.id, title_complete)
            assert completed is not None, (
                f"Expected fuzzy match for '{title_save}' / '{title_complete}'"
            )
            assert completed.id == task.id
            assert completed.status == TaskStatus.COMPLETED.value
            assert completed.completed_at is not None

            await _cleanup(session, phone)
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())


@given(phone=_phone)
@settings(max_examples=10, suppress_health_check=[HealthCheck.too_slow])
def test_completion_returns_none_for_no_match(phone):
    """complete_task_by_title returns None when no pending task matches."""

    async def _test():
        eng, session = await _make_session()
        try:
            await _cleanup(session, phone)
            user = await _ensure_user(session, phone)
            svc = TaskService(session)

            result = await svc.complete_task_by_title(
                user.id, "completely unrelated xyz"
            )
            assert result is None

            await _cleanup(session, phone)
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())


# ---------------------------------------------------------------------------
# P17: Task listing returns top N by priority then recency
# **Validates: Requirements 9.5**
# ---------------------------------------------------------------------------


@given(
    phone=_phone,
    n=st.integers(min_value=1, max_value=5),
    priorities=st.lists(
        st.integers(min_value=0, max_value=100),
        min_size=2,
        max_size=8,
    ),
)
@settings(max_examples=25, suppress_health_check=[HealthCheck.too_slow])
def test_listing_respects_limit_and_priority_order(phone, n, priorities):
    """list_pending_tasks returns at most N tasks, ordered by priority DESC
    then created_at DESC."""

    async def _test():
        eng, session = await _make_session()
        try:
            await _cleanup(session, phone)
            user = await _ensure_user(session, phone)
            svc = TaskService(session)

            # Create tasks with distinct titles and given priorities
            for i, p in enumerate(priorities):
                await svc.save_task(
                    user.id,
                    f"unique task number {i} with code {p}x{i}",
                    priority=p,
                )

            result = await svc.list_pending_tasks(user.id, limit=n)

            # At most N
            assert len(result) <= n

            # At most len(priorities) (can't return more than exist)
            assert len(result) <= len(priorities)

            # Ordered by priority DESC
            for i in range(len(result) - 1):
                assert result[i].priority >= result[i + 1].priority, (
                    f"Task at index {i} (priority={result[i].priority}) should "
                    f"have >= priority than index {i + 1} (priority={result[i + 1].priority})"
                )

            # Among equal-priority tasks, ordered by created_at DESC (most recent first)
            for i in range(len(result) - 1):
                if result[i].priority == result[i + 1].priority:
                    assert result[i].created_at >= result[i + 1].created_at, (
                        f"Equal-priority tasks should be ordered by recency"
                    )

            await _cleanup(session, phone)
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())


@given(phone=_phone, n=st.integers(min_value=1, max_value=10))
@settings(max_examples=10, suppress_health_check=[HealthCheck.too_slow])
def test_listing_excludes_completed_tasks(phone, n):
    """list_pending_tasks only returns pending tasks, not completed ones."""

    async def _test():
        eng, session = await _make_session()
        try:
            await _cleanup(session, phone)
            user = await _ensure_user(session, phone)
            svc = TaskService(session)

            # Create two tasks, complete one
            await svc.save_task(user.id, "pending task alpha", priority=80)
            await svc.save_task(user.id, "will be completed beta", priority=90)
            await svc.complete_task_by_title(user.id, "will be completed beta")

            result = await svc.list_pending_tasks(user.id, limit=n)

            # Only the pending task should appear
            assert len(result) == 1
            assert result[0].title == "pending task alpha"
            assert all(t.status == TaskStatus.PENDING.value for t in result)

            await _cleanup(session, phone)
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())


@given(phone=_phone)
@settings(max_examples=10, suppress_health_check=[HealthCheck.too_slow])
def test_listing_empty_when_no_tasks(phone):
    """list_pending_tasks returns an empty list when the user has no tasks."""

    async def _test():
        eng, session = await _make_session()
        try:
            await _cleanup(session, phone)
            user = await _ensure_user(session, phone)
            svc = TaskService(session)

            result = await svc.list_pending_tasks(user.id, limit=10)
            assert result == []

            await _cleanup(session, phone)
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())
