"""TaskService — fuzzy-dedup task CRUD with pg_trgm similarity.

Implements:
- ``save_task`` — create or merge a task using pg_trgm similarity (threshold 0.6)
- ``complete_task_by_title`` — fuzzy-match completion (threshold 0.4)
- ``list_pending_tasks`` — ordered by priority DESC, created_at DESC
- ``update_task`` / ``delete_task`` — fuzzy-match pending task mutations
- ``snooze_task`` / ``unsnooze_task`` — defer and reactivate tasks

Cross-source merging preserves the earliest ``created_at`` and the highest
priority when a fuzzy duplicate is found.

Validates: Requirement 9
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.enums import TaskStatus
from app.models.task import Task

logger = logging.getLogger(__name__)

# Similarity thresholds (pg_trgm)
SAVE_SIMILARITY_THRESHOLD = 0.6
COMPLETION_SIMILARITY_THRESHOLD = 0.4
MAX_TASK_LIST_LIMIT = 50


class TaskService:
    """Manages Task lifecycle: creation, mutation, completion, and listing."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # save_task — create or merge with fuzzy dedup
    # ------------------------------------------------------------------

    async def save_task(
        self,
        user_id: int,
        title: str,
        priority: int = 50,
        source: str = "user_mention",
    ) -> tuple[Task, bool]:
        """Save a task, deduplicating against existing pending tasks.

        Uses ``pg_trgm`` similarity to detect near-duplicate titles among
        the user's pending tasks (threshold 0.6).

        Cross-source merging rules:
        - Preserve the **earliest** ``created_at``
        - Use the **highest** priority

        Returns:
            A tuple of ``(task, created)`` where *created* is ``True``
            when a new row was inserted, ``False`` when an existing task
            was merged/updated.
        """
        # Look for a similar pending task
        existing = await self._find_similar_pending(
            user_id, title, SAVE_SIMILARITY_THRESHOLD
        )

        if existing is not None:
            # Merge: keep earliest created_at, highest priority
            changed = False
            if priority > existing.priority:
                existing.priority = priority
                changed = True
            # created_at is already the earliest — don't bump it
            if changed:
                self.session.add(existing)
                await self.session.commit()
                await self.session.refresh(existing)
            return existing, False

        # No match — create new task
        task = Task(
            user_id=user_id,
            title=title,
            priority=priority,
            source=source,
        )
        self.session.add(task)
        await self.session.commit()
        await self.session.refresh(task)
        return task, True

    # ------------------------------------------------------------------
    # complete_task_by_title — fuzzy match completion
    # ------------------------------------------------------------------

    async def complete_task_by_title(
        self,
        user_id: int,
        title: str,
    ) -> Task | None:
        """Mark a pending task as completed using fuzzy title matching.

        Uses a more lenient similarity threshold (0.4) than ``save_task``
        because users often describe tasks differently when reporting
        completion.

        Idempotent: completing an already-completed task is a no-op
        (returns the task without modification).

        Returns:
            The matched ``Task`` if found, or ``None`` if no match.
        """
        match = await self._find_similar_pending(
            user_id, title, COMPLETION_SIMILARITY_THRESHOLD
        )

        if match is None:
            return await self._find_similar_by_status(
                user_id=user_id,
                title=title,
                threshold=COMPLETION_SIMILARITY_THRESHOLD,
                status=TaskStatus.COMPLETED.value,
            )

        # Idempotent: already completed → no-op
        if match.status == TaskStatus.COMPLETED.value:
            return match

        match.status = TaskStatus.COMPLETED.value
        match.completed_at = datetime.now(timezone.utc)
        self.session.add(match)
        await self.session.commit()
        await self.session.refresh(match)
        return match

    # ------------------------------------------------------------------
    # update_task — fuzzy match and edit a pending task
    # ------------------------------------------------------------------

    async def update_task(
        self,
        user_id: int,
        title: str,
        new_title: str | None = None,
        new_priority: int | None = None,
    ) -> Task | None:
        """Update a pending task's title and/or priority using fuzzy matching.

        Args:
            user_id: The owning user's id.
            title: Description to fuzzy-match against pending tasks.
            new_title: New title to set, or None to keep the current title.
            new_priority: New priority 0-100, or None to keep the current priority.

        Returns:
            The updated ``Task`` if found, or ``None`` if no pending task matches.

        Raises:
            ValueError: If no update fields are supplied or priority is invalid.
        """
        if new_title is None and new_priority is None:
            raise ValueError(
                "At least one of new_title or new_priority must be provided."
            )
        if new_title is not None and not new_title.strip():
            raise ValueError("new_title cannot be empty.")
        if new_priority is not None and not 0 <= new_priority <= 100:
            raise ValueError("new_priority must be between 0 and 100.")

        match = await self._find_similar_pending(
            user_id, title, COMPLETION_SIMILARITY_THRESHOLD
        )
        if match is None:
            return None

        if new_title is not None:
            match.title = new_title.strip()
        if new_priority is not None:
            match.priority = new_priority

        self.session.add(match)
        await self.session.commit()
        await self.session.refresh(match)
        return match

    # ------------------------------------------------------------------
    # delete_task — fuzzy match and hard-delete a pending task
    # ------------------------------------------------------------------

    async def delete_task(
        self,
        user_id: int,
        title: str,
    ) -> Task | None:
        """Permanently delete a pending task using fuzzy title matching."""
        match = await self._find_similar_pending(
            user_id, title, COMPLETION_SIMILARITY_THRESHOLD
        )
        if match is None:
            return None

        await self.session.delete(match)
        await self.session.commit()
        return match

    # ------------------------------------------------------------------
    # snooze_task — fuzzy match and defer a pending task
    # ------------------------------------------------------------------

    async def snooze_task(
        self,
        user_id: int,
        title: str,
        snooze_until: datetime,
    ) -> Task | None:
        """Snooze a pending task until a specific datetime."""
        match = await self._find_similar_pending(
            user_id, title, COMPLETION_SIMILARITY_THRESHOLD
        )
        if match is None:
            return None

        match.status = TaskStatus.SNOOZED.value
        match.snoozed_until = snooze_until
        self.session.add(match)
        await self.session.commit()
        await self.session.refresh(match)
        return match

    # ------------------------------------------------------------------
    # unsnooze_task — fuzzy match and reactivate a snoozed task
    # ------------------------------------------------------------------

    async def unsnooze_task(
        self,
        user_id: int,
        title: str,
    ) -> Task | None:
        """Return a snoozed task to pending status using fuzzy matching."""
        match = await self._find_similar_by_status(
            user_id=user_id,
            title=title,
            threshold=COMPLETION_SIMILARITY_THRESHOLD,
            status=TaskStatus.SNOOZED.value,
        )
        if match is None:
            return None

        match.status = TaskStatus.PENDING.value
        match.snoozed_until = None
        self.session.add(match)
        await self.session.commit()
        await self.session.refresh(match)
        return match

    # ------------------------------------------------------------------
    # ID-based dashboard mutations
    # ------------------------------------------------------------------

    async def update_task_by_id(
        self,
        user_id: int,
        task_id: int,
        new_title: str | None = None,
        new_priority: int | None = None,
    ) -> Task | None:
        """Update a task by ID for dashboard/API callers."""
        if new_title is None and new_priority is None:
            raise ValueError(
                "At least one of new_title or new_priority must be provided."
            )
        if new_title is not None and not new_title.strip():
            raise ValueError("new_title cannot be empty.")
        if new_priority is not None and not 0 <= new_priority <= 100:
            raise ValueError("new_priority must be between 0 and 100.")

        task = await self._get_user_task(task_id, user_id)
        if task is None:
            return None

        if new_title is not None:
            task.title = new_title.strip()
        if new_priority is not None:
            task.priority = new_priority

        self.session.add(task)
        await self.session.commit()
        await self.session.refresh(task)
        return task

    async def complete_task_by_id(self, user_id: int, task_id: int) -> Task | None:
        """Mark a task completed by ID."""
        task = await self._get_user_task(task_id, user_id)
        if task is None:
            return None

        if task.status != TaskStatus.COMPLETED.value:
            task.status = TaskStatus.COMPLETED.value
            task.completed_at = datetime.now(timezone.utc)
            task.snoozed_until = None
            self.session.add(task)
            await self.session.commit()
            await self.session.refresh(task)
        return task

    async def delete_task_by_id(self, user_id: int, task_id: int) -> Task | None:
        """Permanently delete a task by ID."""
        task = await self._get_user_task(task_id, user_id)
        if task is None:
            return None

        await self.session.delete(task)
        await self.session.commit()
        return task

    async def snooze_task_by_id(
        self,
        user_id: int,
        task_id: int,
        snooze_until: datetime,
    ) -> Task | None:
        """Snooze a task by ID until a timezone-aware datetime."""
        if snooze_until.tzinfo is None or snooze_until.utcoffset() is None:
            raise ValueError("snooze_until must include a timezone offset.")

        task = await self._get_user_task(task_id, user_id)
        if task is None:
            return None

        task.status = TaskStatus.SNOOZED.value
        task.snoozed_until = snooze_until
        task.completed_at = None
        self.session.add(task)
        await self.session.commit()
        await self.session.refresh(task)
        return task

    async def unsnooze_task_by_id(self, user_id: int, task_id: int) -> Task | None:
        """Reactivate a snoozed task by ID."""
        task = await self._get_user_task(task_id, user_id)
        if task is None:
            return None

        task.status = TaskStatus.PENDING.value
        task.snoozed_until = None
        self.session.add(task)
        await self.session.commit()
        await self.session.refresh(task)
        return task

    # ------------------------------------------------------------------
    # list_pending_tasks
    # ------------------------------------------------------------------

    async def list_pending_tasks(
        self,
        user_id: int,
        limit: int = 10,
    ) -> list[Task]:
        """Return pending tasks ordered by priority DESC, created_at DESC.

        Args:
            user_id: The owning user's id.
            limit: Maximum number of tasks to return (default 10).
        """
        limit = self._normalize_limit(limit, default=10)
        await self._reactivate_due_snoozed_tasks(user_id)

        result = await self.session.exec(
            select(Task)
            .where(
                Task.user_id == user_id,
                Task.status == TaskStatus.PENDING.value,
            )
            .order_by(
                Task.priority.desc(),  # type: ignore[union-attr]
                Task.created_at.desc(),  # type: ignore[union-attr]
            )
            .limit(limit)
        )
        return list(result.all())

    # ------------------------------------------------------------------
    # list_completed_tasks
    # ------------------------------------------------------------------

    async def list_completed_tasks(
        self,
        user_id: int,
        limit: int = 50,
    ) -> list[Task]:
        """Return completed tasks ordered by completed_at DESC."""
        result = await self.session.exec(
            select(Task)
            .where(
                Task.user_id == user_id,
                Task.status == TaskStatus.COMPLETED.value,
            )
            .order_by(
                Task.completed_at.desc(),  # type: ignore[union-attr]
            )
            .limit(limit)
        )
        return list(result.all())

    async def list_snoozed_tasks(
        self,
        user_id: int,
        limit: int = 50,
    ) -> list[Task]:
        """Return snoozed tasks ordered by snooze time then priority."""
        limit = self._normalize_limit(limit, default=50)
        await self._reactivate_due_snoozed_tasks(user_id)

        result = await self.session.exec(
            select(Task)
            .where(
                Task.user_id == user_id,
                Task.status == TaskStatus.SNOOZED.value,
            )
            .order_by(
                Task.snoozed_until.asc(),  # type: ignore[union-attr]
                Task.priority.desc(),  # type: ignore[union-attr]
                Task.created_at.desc(),  # type: ignore[union-attr]
            )
            .limit(limit)
        )
        return list(result.all())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _find_similar_by_status(
        self,
        user_id: int,
        title: str,
        threshold: float,
        status: str,
    ) -> Task | None:
        """Find the most similar task with *status* above *threshold*.

        Uses ``func.similarity`` from the ``pg_trgm`` extension.
        """
        similarity = func.similarity(Task.title, title)
        result = await self.session.exec(
            select(Task)
            .where(
                Task.user_id == user_id,
                Task.status == status,
                similarity > threshold,
            )
            .order_by(
                similarity.desc(),
                Task.priority.desc(),  # type: ignore[union-attr]
                Task.created_at.desc(),  # type: ignore[union-attr]
            )
            .limit(1)
        )
        return result.first()

    async def _get_user_task(self, task_id: int, user_id: int) -> Task | None:
        """Return a task only when it belongs to the given user."""
        result = await self.session.exec(
            select(Task).where(
                Task.id == task_id,
                Task.user_id == user_id,
            )
        )
        return result.first()

    async def _find_similar_pending(
        self,
        user_id: int,
        title: str,
        threshold: float,
    ) -> Task | None:
        """Find the most similar pending task above *threshold*."""
        await self._reactivate_due_snoozed_tasks(user_id)
        return await self._find_similar_by_status(
            user_id=user_id,
            title=title,
            threshold=threshold,
            status=TaskStatus.PENDING.value,
        )

    async def _reactivate_due_snoozed_tasks(self, user_id: int) -> list[Task]:
        """Move due snoozed tasks back to pending before pending lookups."""
        now = datetime.now(timezone.utc)
        result = await self.session.exec(
            select(Task).where(
                Task.user_id == user_id,
                Task.status == TaskStatus.SNOOZED.value,
                Task.snoozed_until <= now,  # type: ignore[operator]
            )
        )
        due_tasks = list(result.all())
        if not due_tasks:
            return []

        for task in due_tasks:
            task.status = TaskStatus.PENDING.value
            task.snoozed_until = None
            self.session.add(task)

        await self.session.commit()
        for task in due_tasks:
            await self.session.refresh(task)
        return due_tasks

    @staticmethod
    def _normalize_limit(limit: int | None, *, default: int) -> int:
        """Validate and cap task-list limits from users and model tools."""
        if limit is None:
            return default
        if limit < 1:
            raise ValueError(f"limit must be between 1 and {MAX_TASK_LIST_LIMIT}.")
        return min(limit, MAX_TASK_LIST_LIMIT)
