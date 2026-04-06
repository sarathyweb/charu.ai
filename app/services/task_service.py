"""TaskService — fuzzy-dedup task CRUD with pg_trgm similarity.

Implements:
- ``save_task`` — create or merge a task using pg_trgm similarity (threshold 0.6)
- ``complete_task_by_title`` — fuzzy-match completion (threshold 0.4)
- ``list_pending_tasks`` — ordered by priority DESC, created_at DESC

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


class TaskService:
    """Manages Task lifecycle: creation with fuzzy dedup, completion, listing."""

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
            return None

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
    # Internal helpers
    # ------------------------------------------------------------------

    async def _find_similar_pending(
        self,
        user_id: int,
        title: str,
        threshold: float,
    ) -> Task | None:
        """Find the most similar pending task above *threshold*.

        Uses ``func.similarity`` from the ``pg_trgm`` extension.
        """
        similarity = func.similarity(Task.title, title)
        result = await self.session.exec(
            select(Task)
            .where(
                Task.user_id == user_id,
                Task.status == TaskStatus.PENDING.value,
                similarity > threshold,
            )
            .order_by(similarity.desc())
            .limit(1)
        )
        return result.first()
