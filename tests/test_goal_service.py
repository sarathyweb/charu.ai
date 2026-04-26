"""Unit tests for GoalService."""

from datetime import date, datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.enums import GoalStatus
from app.models.goal import Goal
from app.models.user import User
from app.services.goal_service import GoalService


@pytest_asyncio.fixture
async def svc(session: AsyncSession) -> GoalService:
    return GoalService(session)


async def _create_user(session: AsyncSession, phone: str = "+15551234567") -> User:
    user = User(
        phone=phone,
        timezone="America/New_York",
        onboarding_complete=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


class TestCreateGoal:
    @pytest.mark.asyncio
    async def test_creates_active_goal(self, session, svc):
        user = await _create_user(session)
        target = date(2026, 5, 15)

        goal = await svc.create_goal(
            user.id,
            "  Finish tax filing  ",
            description="  Pull documents together  ",
            target_date=target,
        )

        assert goal.id is not None
        assert goal.user_id == user.id
        assert goal.title == "Finish tax filing"
        assert goal.description == "Pull documents together"
        assert goal.status == GoalStatus.ACTIVE.value
        assert goal.target_date == target
        assert goal.completed_at is None

    @pytest.mark.asyncio
    async def test_rejects_empty_title(self, svc):
        with pytest.raises(ValueError, match="title cannot be empty"):
            await svc.create_goal(1, "   ")


class TestUpdateGoal:
    @pytest.mark.asyncio
    async def test_updates_goal_fields(self, session, svc):
        user = await _create_user(session)
        goal = await svc.create_goal(user.id, "Finish tax filing")

        updated = await svc.update_goal(
            goal.id,
            user.id,
            new_title="Finish quarterly tax filing",
            new_description="Collect forms and submit",
            new_target_date=date(2026, 5, 20),
        )

        assert updated is not None
        assert updated.id == goal.id
        assert updated.title == "Finish quarterly tax filing"
        assert updated.description == "Collect forms and submit"
        assert updated.target_date == date(2026, 5, 20)

    @pytest.mark.asyncio
    async def test_explicit_update_fields_can_clear_optional_fields(self, session, svc):
        user = await _create_user(session)
        goal = await svc.create_goal(
            user.id,
            "Finish tax filing",
            description="Collect forms",
            target_date=date(2026, 5, 20),
        )

        updated = await svc.update_goal(
            goal.id,
            user.id,
            new_description=None,
            new_target_date=None,
            update_fields={"description", "target_date"},
        )

        assert updated is not None
        assert updated.description is None
        assert updated.target_date is None

    @pytest.mark.asyncio
    async def test_rejects_empty_update(self, session, svc):
        user = await _create_user(session)
        goal = await svc.create_goal(user.id, "Finish tax filing")

        with pytest.raises(ValueError, match="At least one"):
            await svc.update_goal(goal.id, user.id)

    @pytest.mark.asyncio
    async def test_returns_none_for_wrong_user(self, session, svc):
        user1 = await _create_user(session, "+15551111111")
        user2 = await _create_user(session, "+15552222222")
        goal = await svc.create_goal(user1.id, "Finish tax filing")

        result = await svc.update_goal(
            goal.id,
            user2.id,
            new_title="Steal this goal",
        )

        assert result is None


class TestCompleteAndAbandonGoal:
    @pytest.mark.asyncio
    async def test_completes_goal_idempotently(self, session, svc):
        user = await _create_user(session)
        goal = await svc.create_goal(user.id, "Finish tax filing")

        first = await svc.complete_goal(goal.id, user.id)
        assert first is not None
        assert first.status == GoalStatus.COMPLETED.value
        assert first.completed_at is not None
        first_completed_at = first.completed_at

        second = await svc.complete_goal(goal.id, user.id)
        assert second is not None
        assert second.completed_at == first_completed_at

    @pytest.mark.asyncio
    async def test_abandons_goal_and_clears_completed_at(self, session, svc):
        user = await _create_user(session)
        goal = await svc.create_goal(user.id, "Finish tax filing")
        await svc.complete_goal(goal.id, user.id)

        abandoned = await svc.abandon_goal(goal.id, user.id)

        assert abandoned is not None
        assert abandoned.status == GoalStatus.ABANDONED.value
        assert abandoned.completed_at is None

    @pytest.mark.asyncio
    async def test_lifecycle_methods_are_scoped_to_user(self, session, svc):
        user1 = await _create_user(session, "+15551111111")
        user2 = await _create_user(session, "+15552222222")
        goal = await svc.create_goal(user1.id, "Finish tax filing")

        assert await svc.complete_goal(goal.id, user2.id) is None
        assert await svc.abandon_goal(goal.id, user2.id) is None


class TestListGoals:
    @pytest.mark.asyncio
    async def test_lists_goals_ordered_by_created_at_desc(self, session, svc):
        user = await _create_user(session)
        older = Goal(
            user_id=user.id,
            title="Older goal",
            created_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        newer = Goal(
            user_id=user.id,
            title="Newer goal",
            created_at=datetime.now(timezone.utc),
        )
        session.add(older)
        session.add(newer)
        await session.commit()

        goals = await svc.list_goals(user.id)

        assert [goal.title for goal in goals] == ["Newer goal", "Older goal"]

    @pytest.mark.asyncio
    async def test_filters_by_status(self, session, svc):
        user = await _create_user(session)
        active = await svc.create_goal(user.id, "Active goal")
        completed = await svc.create_goal(user.id, "Completed goal")
        await svc.complete_goal(completed.id, user.id)

        goals = await svc.list_goals(user.id, status=GoalStatus.ACTIVE.value)

        assert [goal.id for goal in goals] == [active.id]

    @pytest.mark.asyncio
    async def test_rejects_invalid_status_filter(self, session, svc):
        user = await _create_user(session)

        with pytest.raises(ValueError, match="active, completed, abandoned"):
            await svc.list_goals(user.id, status="stuck")


class TestDeleteGoal:
    @pytest.mark.asyncio
    async def test_deletes_goal(self, session, svc):
        user = await _create_user(session)
        goal = await svc.create_goal(user.id, "Finish tax filing")
        goal_id = goal.id

        deleted = await svc.delete_goal(goal_id, user.id)

        assert deleted is not None
        assert deleted.id == goal_id
        assert await session.get(Goal, goal_id) is None

    @pytest.mark.asyncio
    async def test_delete_is_scoped_to_user(self, session, svc):
        user1 = await _create_user(session, "+15551111111")
        user2 = await _create_user(session, "+15552222222")
        goal = await svc.create_goal(user1.id, "Finish tax filing")

        deleted = await svc.delete_goal(goal.id, user2.id)

        assert deleted is None
        assert await session.get(Goal, goal.id) is not None
