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


class FakeEmbeddingService:
    """Deterministic embedding service for task dedupe tests."""

    model = "fake-embedding-model"

    def __init__(
        self,
        vectors: dict[str, list[float]] | None = None,
        *,
        fail: bool = False,
    ) -> None:
        self.vectors = vectors or {}
        self.fail = fail
        self.calls: list[str] = []

    async def embed_text(self, text: str) -> list[float]:
        self.calls.append(text)
        if self.fail:
            raise RuntimeError("embedding provider unavailable")
        return self.vectors.get(text, [0.0, 0.0, 1.0])


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
        await svc.save_task(user.id, "File my taxes for 2025", priority=50)
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
        await svc.save_task(user.id, "File my taxes for 2025", priority=90)
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

    @pytest.mark.asyncio
    async def test_embedding_dedup_merges_semantic_paraphrase(self, session):
        user = await _create_user(session)
        embeddings = FakeEmbeddingService(
            {
                "Call Sam about invoice": [1.0, 0.0, 0.0],
                "Phone the client about billing": [1.0, 0.0, 0.0],
            }
        )
        svc = TaskService(
            session,
            embedding_service=embeddings,
            enable_embedding_dedup=True,
            embedding_similarity_threshold=0.95,
        )

        task1, created1 = await svc.save_task(
            user.id, "Call Sam about invoice", priority=40
        )
        task2, created2 = await svc.save_task(
            user.id, "Phone the client about billing", priority=90
        )

        assert created1 is True
        assert created2 is False
        assert task2.id == task1.id
        assert task2.priority == 90
        assert task2.embedding == [1.0, 0.0, 0.0]
        assert task2.embedding_model == "fake-embedding-model"

    @pytest.mark.asyncio
    async def test_embedding_dedup_backfills_existing_task_embedding(self, session):
        user = await _create_user(session)
        existing = Task(
            user_id=user.id,
            title="Submit passport renewal",
            priority=30,
        )
        session.add(existing)
        await session.commit()
        await session.refresh(existing)

        embeddings = FakeEmbeddingService(
            {
                "Submit passport renewal": [0.0, 1.0, 0.0],
                "Handle travel document application": [0.0, 1.0, 0.0],
            }
        )
        svc = TaskService(
            session,
            embedding_service=embeddings,
            enable_embedding_dedup=True,
            embedding_similarity_threshold=0.95,
        )

        task, created = await svc.save_task(
            user.id,
            "Handle travel document application",
            priority=70,
        )

        assert created is False
        assert task.id == existing.id
        assert task.priority == 70
        assert task.embedding == [0.0, 1.0, 0.0]
        assert task.embedding_model == "fake-embedding-model"
        assert task.embedding_updated_at is not None

    @pytest.mark.asyncio
    async def test_embedding_dedup_uses_new_task_when_similarity_is_low(self, session):
        user = await _create_user(session)
        embeddings = FakeEmbeddingService(
            {
                "Call Sam about invoice": [1.0, 0.0, 0.0],
                "Buy hiking boots": [0.0, 1.0, 0.0],
            }
        )
        svc = TaskService(
            session,
            embedding_service=embeddings,
            enable_embedding_dedup=True,
            embedding_similarity_threshold=0.95,
        )

        task1, created1 = await svc.save_task(user.id, "Call Sam about invoice")
        task2, created2 = await svc.save_task(user.id, "Buy hiking boots")

        assert created1 is True
        assert created2 is True
        assert task2.id != task1.id

    @pytest.mark.asyncio
    async def test_embedding_failure_falls_back_without_blocking_task_save(
        self, session
    ):
        user = await _create_user(session)
        svc = TaskService(
            session,
            embedding_service=FakeEmbeddingService(fail=True),
            enable_embedding_dedup=True,
        )

        task, created = await svc.save_task(user.id, "Plan next launch")

        assert created is True
        assert task.embedding is None
        assert task.embedding_model is None


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
        await svc.save_task(user.id, "File my taxes")

        # Complete it
        result1 = await svc.complete_task_by_title(user.id, "File taxes")
        assert result1 is not None
        assert result1.status == TaskStatus.COMPLETED.value
        first_completed_at = result1.completed_at

        result2 = await svc.complete_task_by_title(user.id, "File taxes")
        assert result2 is not None
        assert result2.id == result1.id
        assert result2.status == TaskStatus.COMPLETED.value
        assert result2.completed_at == first_completed_at

    @pytest.mark.asyncio
    async def test_scoped_to_user(self, session, svc):
        user1 = await _create_user(session, "+15551111111")
        user2 = await _create_user(session, "+15552222222")
        await svc.save_task(user1.id, "File my taxes")

        # User2 should not find user1's task
        result = await svc.complete_task_by_title(user2.id, "File taxes")
        assert result is None


# ---------------------------------------------------------------------------
# update_task
# ---------------------------------------------------------------------------


class TestUpdateTask:
    """Tests for TaskService.update_task."""

    @pytest.mark.asyncio
    async def test_updates_title_and_priority(self, session, svc):
        user = await _create_user(session)
        task, _ = await svc.save_task(user.id, "File my taxes", priority=40)

        result = await svc.update_task(
            user.id,
            "file taxes",
            new_title="File quarterly taxes",
            new_priority=90,
        )

        assert result is not None
        assert result.id == task.id
        assert result.title == "File quarterly taxes"
        assert result.priority == 90
        assert result.status == TaskStatus.PENDING.value

    @pytest.mark.asyncio
    async def test_updates_only_priority(self, session, svc):
        user = await _create_user(session)
        task, _ = await svc.save_task(
            user.id, "Schedule dentist appointment", priority=20
        )

        result = await svc.update_task(
            user.id,
            "dentist appointment",
            new_priority=80,
        )

        assert result is not None
        assert result.id == task.id
        assert result.title == "Schedule dentist appointment"
        assert result.priority == 80

    @pytest.mark.asyncio
    async def test_returns_none_when_no_pending_match(self, session, svc):
        user = await _create_user(session)
        await svc.save_task(user.id, "File my taxes")

        result = await svc.update_task(user.id, "buy a new car", new_priority=90)

        assert result is None

    @pytest.mark.asyncio
    async def test_rejects_empty_update(self, session, svc):
        user = await _create_user(session)
        await svc.save_task(user.id, "File my taxes")

        with pytest.raises(ValueError, match="At least one"):
            await svc.update_task(user.id, "file taxes")

    @pytest.mark.asyncio
    async def test_rejects_invalid_priority(self, session, svc):
        user = await _create_user(session)
        await svc.save_task(user.id, "File my taxes")

        with pytest.raises(ValueError, match="between 0 and 100"):
            await svc.update_task(user.id, "file taxes", new_priority=101)

    @pytest.mark.asyncio
    async def test_does_not_update_completed_or_snoozed_tasks(self, session, svc):
        user = await _create_user(session)
        completed, _ = await svc.save_task(user.id, "Done tax task")
        completed.status = TaskStatus.COMPLETED.value
        completed.completed_at = datetime.now(timezone.utc)
        snoozed, _ = await svc.save_task(user.id, "Deferred tax task")
        snoozed.status = TaskStatus.SNOOZED.value
        snoozed.snoozed_until = datetime.now(timezone.utc) + timedelta(days=1)
        session.add(completed)
        session.add(snoozed)
        await session.commit()

        result = await svc.update_task(user.id, "tax task", new_priority=90)

        assert result is None

    @pytest.mark.asyncio
    async def test_update_by_id_refreshes_embedding_for_new_title(self, session):
        user = await _create_user(session)
        embeddings = FakeEmbeddingService(
            {
                "Draft partner memo": [1.0, 0.0, 0.0],
                "Draft client memo": [0.0, 1.0, 0.0],
            }
        )
        svc = TaskService(
            session,
            embedding_service=embeddings,
            enable_embedding_dedup=True,
        )
        task, _ = await svc.save_task(user.id, "Draft partner memo")

        updated = await svc.update_task_by_id(
            user.id,
            task.id,
            new_title="Draft client memo",
        )

        assert updated is not None
        assert updated.embedding == [0.0, 1.0, 0.0]
        assert updated.embedding_model == "fake-embedding-model"
        assert updated.embedding_updated_at is not None


# ---------------------------------------------------------------------------
# delete_task
# ---------------------------------------------------------------------------


class TestDeleteTask:
    """Tests for TaskService.delete_task."""

    @pytest.mark.asyncio
    async def test_deletes_matching_pending_task(self, session, svc):
        user = await _create_user(session)
        task, _ = await svc.save_task(user.id, "Buy groceries")
        task_id = task.id

        result = await svc.delete_task(user.id, "groceries")

        assert result is not None
        assert result.id == task_id
        assert result.title == "Buy groceries"
        assert await session.get(Task, task_id) is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_pending_match(self, session, svc):
        user = await _create_user(session)
        await svc.save_task(user.id, "Buy groceries")

        result = await svc.delete_task(user.id, "file taxes")

        assert result is None

    @pytest.mark.asyncio
    async def test_does_not_delete_completed_task(self, session, svc):
        user = await _create_user(session)
        task, _ = await svc.save_task(user.id, "Buy groceries")
        task.status = TaskStatus.COMPLETED.value
        task.completed_at = datetime.now(timezone.utc)
        session.add(task)
        await session.commit()

        result = await svc.delete_task(user.id, "groceries")

        assert result is None
        assert await session.get(Task, task.id) is not None

    @pytest.mark.asyncio
    async def test_scoped_to_user(self, session, svc):
        user1 = await _create_user(session, "+15551111111")
        user2 = await _create_user(session, "+15552222222")
        task, _ = await svc.save_task(user1.id, "Buy groceries")

        result = await svc.delete_task(user2.id, "groceries")

        assert result is None
        assert await session.get(Task, task.id) is not None


# ---------------------------------------------------------------------------
# snooze_task / unsnooze_task
# ---------------------------------------------------------------------------


class TestSnoozeTask:
    """Tests for TaskService.snooze_task and unsnooze_task."""

    @pytest.mark.asyncio
    async def test_snoozes_matching_pending_task(self, session, svc):
        user = await _create_user(session)
        task, _ = await svc.save_task(user.id, "Buy groceries")
        snooze_until = datetime.now(timezone.utc) + timedelta(days=1)

        result = await svc.snooze_task(user.id, "groceries", snooze_until)

        assert result is not None
        assert result.id == task.id
        assert result.status == TaskStatus.SNOOZED.value
        assert result.snoozed_until == snooze_until

    @pytest.mark.asyncio
    async def test_snooze_returns_none_when_no_pending_match(self, session, svc):
        user = await _create_user(session)
        await svc.save_task(user.id, "Buy groceries")
        snooze_until = datetime.now(timezone.utc) + timedelta(days=1)

        result = await svc.snooze_task(user.id, "file taxes", snooze_until)

        assert result is None

    @pytest.mark.asyncio
    async def test_snoozed_tasks_excluded_from_pending_list(self, session, svc):
        user = await _create_user(session)
        await svc.save_task(user.id, "Pending task")
        await svc.save_task(user.id, "Snooze me")
        snooze_until = datetime.now(timezone.utc) + timedelta(days=1)

        await svc.snooze_task(user.id, "snooze me", snooze_until)
        tasks = await svc.list_pending_tasks(user.id)

        assert [task.title for task in tasks] == ["Pending task"]

    @pytest.mark.asyncio
    async def test_due_snoozed_tasks_reappear_in_pending_list(self, session, svc):
        user = await _create_user(session)
        task, _ = await svc.save_task(user.id, "Snooze me briefly")
        snooze_until = datetime.now(timezone.utc) - timedelta(minutes=1)

        await svc.snooze_task(user.id, "snooze me briefly", snooze_until)
        tasks = await svc.list_pending_tasks(user.id)

        assert len(tasks) == 1
        assert tasks[0].id == task.id
        assert tasks[0].status == TaskStatus.PENDING.value
        assert tasks[0].snoozed_until is None

    @pytest.mark.asyncio
    async def test_unsnoozes_matching_snoozed_task(self, session, svc):
        user = await _create_user(session)
        task, _ = await svc.save_task(user.id, "Buy groceries")
        snooze_until = datetime.now(timezone.utc) + timedelta(days=1)
        await svc.snooze_task(user.id, "groceries", snooze_until)

        result = await svc.unsnooze_task(user.id, "groceries")

        assert result is not None
        assert result.id == task.id
        assert result.status == TaskStatus.PENDING.value
        assert result.snoozed_until is None

    @pytest.mark.asyncio
    async def test_unsnooze_returns_none_for_pending_task(self, session, svc):
        user = await _create_user(session)
        await svc.save_task(user.id, "Buy groceries")

        result = await svc.unsnooze_task(user.id, "groceries")

        assert result is None

    @pytest.mark.asyncio
    async def test_fuzzy_tie_break_prefers_priority_then_recency(self, session, svc):
        user = await _create_user(session)
        older = Task(
            user_id=user.id,
            title="Review tax documents alpha",
            priority=30,
            created_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        newer_low = Task(
            user_id=user.id,
            title="Review tax documents alpha",
            priority=30,
            created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        high_priority = Task(
            user_id=user.id,
            title="Review tax documents alpha",
            priority=90,
            created_at=datetime.now(timezone.utc) - timedelta(hours=3),
        )
        session.add(older)
        session.add(newer_low)
        session.add(high_priority)
        await session.commit()

        result = await svc.update_task(
            user.id,
            "review tax documents alpha",
            new_title="Review tax packet",
        )

        assert result is not None
        assert result.id == high_priority.id


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
    async def test_rejects_non_positive_limit(self, session, svc):
        user = await _create_user(session)

        with pytest.raises(ValueError, match="limit must be between 1 and 50"):
            await svc.list_pending_tasks(user.id, limit=0)

    @pytest.mark.asyncio
    async def test_caps_large_limit(self, session, svc):
        user = await _create_user(session)
        for index in range(55):
            session.add(
                Task(
                    user_id=user.id,
                    title=f"Unique limit task {index}",
                    priority=index % 100,
                )
            )
        await session.commit()

        tasks = await svc.list_pending_tasks(user.id, limit=999)

        assert len(tasks) == 50

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
