"""GoalService — CRUD operations for user-owned goals."""

import logging
from datetime import date, datetime, timezone

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.enums import GoalStatus
from app.models.goal import Goal

logger = logging.getLogger(__name__)


class GoalService:
    """Manages Goal lifecycle: create, update, complete, abandon, list, delete."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_goal(
        self,
        user_id: int,
        title: str,
        description: str | None = None,
        target_date: date | None = None,
    ) -> Goal:
        """Create a new active goal for a user."""
        clean_title = self._clean_title(title)
        goal = Goal(
            user_id=user_id,
            title=clean_title,
            description=self._clean_optional_text(description),
            target_date=target_date,
        )
        self.session.add(goal)
        await self.session.commit()
        await self.session.refresh(goal)
        return goal

    async def update_goal(
        self,
        goal_id: int,
        user_id: int,
        new_title: str | None = None,
        new_description: str | None = None,
        new_target_date: date | None = None,
        update_fields: set[str] | None = None,
    ) -> Goal | None:
        """Update fields on a goal owned by *user_id*."""
        if update_fields is None:
            update_fields = {
                field
                for field, value in (
                    ("title", new_title),
                    ("description", new_description),
                    ("target_date", new_target_date),
                )
                if value is not None
            }

        if not update_fields:
            raise ValueError(
                "At least one of new_title, new_description, or new_target_date "
                "must be provided."
            )

        goal = await self._get_user_goal(goal_id, user_id)
        if goal is None:
            return None

        if "title" in update_fields:
            if new_title is None:
                raise ValueError("title cannot be empty.")
            goal.title = self._clean_title(new_title)
        if "description" in update_fields:
            goal.description = self._clean_optional_text(new_description)
        if "target_date" in update_fields:
            goal.target_date = new_target_date

        self.session.add(goal)
        await self.session.commit()
        await self.session.refresh(goal)
        return goal

    async def complete_goal(self, goal_id: int, user_id: int) -> Goal | None:
        """Mark a goal as completed and set ``completed_at``."""
        goal = await self._get_user_goal(goal_id, user_id)
        if goal is None:
            return None

        if goal.status != GoalStatus.COMPLETED.value:
            goal.status = GoalStatus.COMPLETED.value
            goal.completed_at = datetime.now(timezone.utc)
            self.session.add(goal)
            await self.session.commit()
            await self.session.refresh(goal)
        return goal

    async def abandon_goal(self, goal_id: int, user_id: int) -> Goal | None:
        """Mark a goal as abandoned and clear completion metadata."""
        goal = await self._get_user_goal(goal_id, user_id)
        if goal is None:
            return None

        goal.status = GoalStatus.ABANDONED.value
        goal.completed_at = None
        self.session.add(goal)
        await self.session.commit()
        await self.session.refresh(goal)
        return goal

    async def list_goals(
        self,
        user_id: int,
        status: str | None = None,
    ) -> list[Goal]:
        """List a user's goals, optionally filtered by lifecycle status."""
        if status is not None and status not in {s.value for s in GoalStatus}:
            raise ValueError("status must be one of: active, completed, abandoned.")

        query = select(Goal).where(Goal.user_id == user_id)
        if status is not None:
            query = query.where(Goal.status == status)

        result = await self.session.exec(
            query.order_by(
                Goal.created_at.desc(),  # type: ignore[union-attr]
            )
        )
        return list(result.all())

    async def delete_goal(self, goal_id: int, user_id: int) -> Goal | None:
        """Permanently delete a goal owned by *user_id*."""
        goal = await self._get_user_goal(goal_id, user_id)
        if goal is None:
            return None

        await self.session.delete(goal)
        await self.session.commit()
        return goal

    async def _get_user_goal(self, goal_id: int, user_id: int) -> Goal | None:
        """Return a goal only when it belongs to the given user."""
        result = await self.session.exec(
            select(Goal).where(
                Goal.id == goal_id,
                Goal.user_id == user_id,
            )
        )
        return result.first()

    @staticmethod
    def _clean_title(title: str) -> str:
        clean = title.strip()
        if not clean:
            raise ValueError("title cannot be empty.")
        return clean

    @staticmethod
    def _clean_optional_text(value: str | None) -> str | None:
        if value is None:
            return None
        clean = value.strip()
        return clean or None
