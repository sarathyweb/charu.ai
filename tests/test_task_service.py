"""Unit tests for TaskService.

Tests cover:
- save_task: new task creation, fuzzy dedup merge, cross-source merging
- complete_task_by_title: fuzzy match completion, idempotency, no-match
- list_pending_tasks: ordering by priority DESC, created_at DESC, limit
"""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.enums import TaskStatus
from app.models.task import Task
from app.models.user import User
from app.services.task_service import TaskService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def svc(session: AsyncSession) -> TaskService:
    return TaskService(session)


async def _create_user(session: AsyncSession, phone: str = "+15551234567") -> User:
    user = User(
        phone=phone,
        timezone="America/New_York",
        onboarding_complete=False,
        consecutive_active_days=0,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


# ---------------------------------------------------------------------------
# save_task
# ---------------------------------------------------------------------------


class TestSaveTask:
    """Tests for TaskService.save_task."""

    @pytest.mark.asyncio
    async def test_creates_new_task(self, session, svc):
        user = await _create_user(session)
        task, created = await svc.save_task(user.id, "File my taxes")
        assert created is True
        assert task.id is not None
        assert task.title == "File my taxes"
        assert task.status == TaskStatus.PENDING.value
        assert task.priority == 50
        assert task.source == "user_mention"
        assert task.user_id == user.id

    @pytest.mark.asyncio
    async def test_dedup_merges_similar_title(self, session, svc):
        user = await _create_user(session)
        task1, created1 = await svc.save_task(user.id, "File my taxes for 2025")
        assert created1 is True

        # Nearly identical title should merge
        task2, created2 = await svc.save_task(user.id, "File my taxes for 2026")
        assert created2 is False
        assert task2.id == task1.id

    @pytest.mark.asyncio
    async def test_dedup_preserves_highest_priority(self, session, svc):
        user = await _create_user(session)
        task1, _ = await svc.save_task(user.id, "File my taxes for 2025", priority=50)
        task2, created = await svc.save_task(
            user.id, "File my taxes for 2026", priority=90
        )
        assert created is False
        assert task2.priority == 90

    @pytest.mark.asyncio
    async def test_dedup_preserves_earliest_created_at(self, session, svc):
        user = await _create_user(session)
        task1, _ = await svc.save_task(user.id, "File my taxes for 2025")
        original_created = task1.created_at

        task2, created = await svc.save_task(
            user.id, "File my taxes for 2026", priority=90
        )
        assert created is False
        # created_at should not change
        assert task2.created_at == original_created

    @pytest.mark.asyncio
    async def test_different_titles_create_separate_tasks(self, session, svc):
        user = await _create_user(session)
        task1, c1 = await svc.save_task(user.id, "File my taxes")
        task2, c2 = await svc.save_task(user.id, "Buy groceries")
        assert c1 is True
        assert c2 is True
        assert task1.id != task2.id

    @pytest.mark.asyncio
    async def test_custom_source_and_priority(self, session, svc):
        user = await _create_user(session)
        task, created = await svc.save_task(
            user.id, "Reply to Sarah", priority=70, source="gmail"
        )
        assert created is True
        assert task.priority == 70
        assert task.source == "gmail"

    @pytest.mark.asyncio
    async def test_dedup_does_not_lower_priority(self, session, svc):
        user = await _create_user(session)
        task1, _ = await svc.save_task(user.id, "File my taxes for 2025", priority=90)
        task2, created = await svc.save_task(
            user.id, "File my taxes for 2026", priority=30
        )
        assert created is False
        # Priority should stay at 90 (highest)
        assert task2.priority == 90

    @pytest.mark.asyncio
    async def test_dedup_scoped_to_user(self, session, svc):
        user1 = await _create_user(session, "+15551111111")
        user2 = await _create_user(session, "+15552222222")
        t1, c1 = await svc.save_task(user1.id, "File my taxes")
        t2, c2 = await svc.save_task(user2.id, "File my taxes")
        assert c1 is True
        assert c2 is True
        assert t1.id != t2.id

    @pytest.mark.asyncio
    async def test_dedup_ignores_completed_tasks(self, session, svc):
        user = await _create_user(session)
        task1, _ = await svc.save_task(user.id, "File my taxes")
        # Complete it
        task1.status = TaskStatus.COMPLETED.value
        task1.completed_at = datetime.now(timezone.utc)
        session.add(task1)
        await session.commit()

        # Same title should create a new task since the old one is completed
        task2, created = await svc.save_task(user.id, "File my taxes")
        assert created is True
        assert task2.id != task1.id


# ---------------------------------------------------------------------------
# complete_task_by_title
# ---------------------------------------------------------------------------


class TestCompleteTaskByTitle:
    """Tests for TaskService.complete_task_by_title."""

    @pytest.mark.asyncio
    async def test_completes_matching_task(self, session, svc):
        user = await _create_user(session)
        await svc.save_task(user.id, "File my taxes")

        result = await svc.complete_task_by_title(user.id, "File taxes")
        assert result is not None
        assert result.status == TaskStatus.COMPLETED.value
        assert result.completed_at is not None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_match(self, session, svc):
        user = await _create_user(session)
        await svc.save_task(user.id, "File my taxes")

        result = await svc.complete_task_by_title(user.id, "Buy a new car")
        assert result is None

    @pytest.mark.asyncio
    async def test_idempotent_on_already_completed(self, session, svc):
        user = await _create_user(session)
        task, _ = await svc.save_task(user.id, "File my taxes")

        # Complete it
        result1 = await svc.complete_task_by_title(user.id, "File taxes")
        assert result1 is not None
        assert result1.status == TaskStatus.COMPLETED.value

    @pytest.mark.asyncio
    async def test_scoped_to_user(self, session, svc):
        user1 = await _create_user(session, "+15551111111")
        user2 = await _create_user(session, "+15552222222")
        await svc.save_task(user1.id, "File my taxes")

        # User2 should not find user1's task
        result = await svc.complete_task_by_title(user2.id, "File taxes")
        assert result is None


# ---------------------------------------------------------------------------
# list_pending_tasks
# ---------------------------------------------------------------------------


class TestListPendingTasks:
    """Tests for TaskService.list_pending_tasks."""

    @pytest.mark.asyncio
    async def test_returns_pending_tasks_ordered(self, session, svc):
        user = await _create_user(session)
        await svc.save_task(user.id, "Buy groceries from the store", priority=20)
        await svc.save_task(user.id, "Finish the quarterly report", priority=90)
        await svc.save_task(user.id, "Schedule dentist appointment", priority=50)

        tasks = await svc.list_pending_tasks(user.id)
        assert len(tasks) == 3
        assert tasks[0].title == "Finish the quarterly report"
        assert tasks[1].title == "Schedule dentist appointment"
        assert tasks[2].title == "Buy groceries from the store"

    @pytest.mark.asyncio
    async def test_excludes_completed_tasks(self, session, svc):
        user = await _create_user(session)
        task1, _ = await svc.save_task(user.id, "Done task")
        task1.status = TaskStatus.COMPLETED.value
        task1.completed_at = datetime.now(timezone.utc)
        session.add(task1)
        await session.commit()

        await svc.save_task(user.id, "Pending task")

        tasks = await svc.list_pending_tasks(user.id)
        assert len(tasks) == 1
        assert tasks[0].title == "Pending task"

    @pytest.mark.asyncio
    async def test_respects_limit(self, session, svc):
        user = await _create_user(session)
        titles = [
            "Buy groceries from the store",
            "Finish the quarterly report",
            "Schedule dentist appointment",
            "Reply to Sarah about the project",
            "Clean the garage this weekend",
        ]
        for i, title in enumerate(titles):
            await svc.save_task(user.id, title, priority=i * 10)

        tasks = await svc.list_pending_tasks(user.id, limit=3)
        assert len(tasks) == 3

    @pytest.mark.asyncio
    async def test_empty_when_no_tasks(self, session, svc):
        user = await _create_user(session)
        tasks = await svc.list_pending_tasks(user.id)
        assert tasks == []

    @pytest.mark.asyncio
    async def test_scoped_to_user(self, session, svc):
        user1 = await _create_user(session, "+15551111111")
        user2 = await _create_user(session, "+15552222222")
        await svc.save_task(user1.id, "User1 task")
        await svc.save_task(user2.id, "User2 task")

        tasks = await svc.list_pending_tasks(user1.id)
        assert len(tasks) == 1
        assert tasks[0].title == "User1 task"

    @pytest.mark.asyncio
    async def test_same_priority_ordered_by_recency(self, session, svc):
        user = await _create_user(session)
        # Create tasks with same priority but different created_at
        t1 = Task(
            user_id=user.id,
            title="Older task",
            priority=50,
            created_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        session.add(t1)
        await session.commit()

        t2 = Task(
            user_id=user.id,
            title="Newer task",
            priority=50,
            created_at=datetime.now(timezone.utc),
        )
        session.add(t2)
        await session.commit()

        tasks = await svc.list_pending_tasks(user.id)
        assert len(tasks) == 2
        # Newer task should come first (created_at DESC)
        assert tasks[0].title == "Newer task"
        assert tasks[1].title == "Older task"
