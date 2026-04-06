"""Unit tests for UserService preference persistence (task 3.1).

Tests get_or_create_by_phone, update_preferences, and hydrate_session_state.
"""

import pytest
import pytest_asyncio
from datetime import time

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.user import User
from app.models.call_window import CallWindow
from app.services.user_service import UserService, hydrate_session_state


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def user_service(session: AsyncSession) -> UserService:
    return UserService(session)


@pytest_asyncio.fixture
async def sample_user(session: AsyncSession) -> User:
    """Create a minimal user for tests that need one pre-existing."""
    user = User(phone="+14155552671")
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


# ---------------------------------------------------------------------------
# get_or_create_by_phone
# ---------------------------------------------------------------------------


class TestGetOrCreateByPhone:
    @pytest.mark.asyncio
    async def test_creates_new_user(
        self, user_service: UserService, session: AsyncSession
    ):
        user = await user_service.get_or_create_by_phone("+14155552671")
        assert user is not None
        assert user.phone == "+14155552671"
        assert user.id is not None

    @pytest.mark.asyncio
    async def test_returns_existing_user(
        self, user_service: UserService, sample_user: User
    ):
        user = await user_service.get_or_create_by_phone("+14155552671")
        assert user.id == sample_user.id

    @pytest.mark.asyncio
    async def test_normalizes_phone(
        self, user_service: UserService, session: AsyncSession
    ):
        user = await user_service.get_or_create_by_phone("+1 415 555 2671")
        assert user.phone == "+14155552671"

    @pytest.mark.asyncio
    async def test_no_firebase_uid(
        self, user_service: UserService, session: AsyncSession
    ):
        user = await user_service.get_or_create_by_phone("+14155552671")
        assert user.firebase_uid is None

    @pytest.mark.asyncio
    async def test_idempotent(self, user_service: UserService, session: AsyncSession):
        u1 = await user_service.get_or_create_by_phone("+14155552671")
        u2 = await user_service.get_or_create_by_phone("+14155552671")
        assert u1.id == u2.id
        # Verify only one row exists
        result = await session.exec(select(User).where(User.phone == "+14155552671"))
        assert len(result.all()) == 1


# ---------------------------------------------------------------------------
# update_preferences
# ---------------------------------------------------------------------------


class TestUpdatePreferences:
    @pytest.mark.asyncio
    async def test_updates_name(self, user_service: UserService, sample_user: User):
        updated = await user_service.update_preferences("+14155552671", name="Alice")
        assert updated.name == "Alice"

    @pytest.mark.asyncio
    async def test_updates_timezone(self, user_service: UserService, sample_user: User):
        updated = await user_service.update_preferences(
            "+14155552671", timezone="America/New_York"
        )
        assert updated.timezone == "America/New_York"

    @pytest.mark.asyncio
    async def test_updates_onboarding_complete(
        self, user_service: UserService, sample_user: User
    ):
        updated = await user_service.update_preferences(
            "+14155552671", onboarding_complete=True
        )
        assert updated.onboarding_complete is True

    @pytest.mark.asyncio
    async def test_partial_update(self, user_service: UserService, sample_user: User):
        """Only the supplied fields change; others remain untouched."""
        await user_service.update_preferences("+14155552671", name="Bob")
        updated = await user_service.update_preferences(
            "+14155552671", timezone="Asia/Dubai"
        )
        assert updated.name == "Bob"
        assert updated.timezone == "Asia/Dubai"

    @pytest.mark.asyncio
    async def test_idempotent_no_change(
        self, user_service: UserService, sample_user: User, session: AsyncSession
    ):
        """Setting the same value is a no-op."""
        await user_service.update_preferences("+14155552671", name="Alice")
        user_before = await user_service.get_by_phone("+14155552671")
        updated_at_before = user_before.updated_at

        await user_service.update_preferences("+14155552671", name="Alice")
        user_after = await user_service.get_by_phone("+14155552671")
        # updated_at should not change when value is the same
        assert user_after.updated_at == updated_at_before

    @pytest.mark.asyncio
    async def test_rejects_sensitive_fields(
        self, user_service: UserService, sample_user: User
    ):
        with pytest.raises(ValueError, match="restricted/unknown"):
            await user_service.update_preferences(
                "+14155552671", google_access_token_encrypted="bad"
            )

    @pytest.mark.asyncio
    async def test_rejects_unknown_fields(
        self, user_service: UserService, sample_user: User
    ):
        with pytest.raises(ValueError, match="restricted/unknown"):
            await user_service.update_preferences("+14155552671", nonexistent="x")

    @pytest.mark.asyncio
    async def test_raises_for_missing_user(self, user_service: UserService):
        with pytest.raises(ValueError, match="No user found"):
            await user_service.update_preferences("+447911123456", name="Ghost")

    @pytest.mark.asyncio
    async def test_returns_user_object(
        self, user_service: UserService, sample_user: User
    ):
        result = await user_service.update_preferences("+14155552671", name="Carol")
        assert isinstance(result, User)
        assert result.phone == "+14155552671"


# ---------------------------------------------------------------------------
# hydrate_session_state
# ---------------------------------------------------------------------------


class TestHydrateSessionState:
    @pytest.mark.asyncio
    async def test_unknown_phone_returns_phone_only(self, session: AsyncSession):
        state = await hydrate_session_state("+447911123456", session)
        assert state == {"phone": "+447911123456"}

    @pytest.mark.asyncio
    async def test_includes_name_and_timezone(
        self, session: AsyncSession, sample_user: User
    ):
        sample_user.name = "Sarathy"
        sample_user.timezone = "Asia/Dubai"
        session.add(sample_user)
        await session.commit()

        state = await hydrate_session_state("+14155552671", session)
        assert state["user:name"] == "Sarathy"
        assert state["user:timezone"] == "Asia/Dubai"
        assert state["phone"] == "+14155552671"

    @pytest.mark.asyncio
    async def test_onboarding_complete_flag(
        self, session: AsyncSession, sample_user: User
    ):
        state = await hydrate_session_state("+14155552671", session)
        assert state["user:onboarding_complete"] is False

        sample_user.onboarding_complete = True
        session.add(sample_user)
        await session.commit()

        state = await hydrate_session_state("+14155552671", session)
        assert state["user:onboarding_complete"] is True

    @pytest.mark.asyncio
    async def test_google_calendar_connected(
        self, session: AsyncSession, sample_user: User
    ):
        sample_user.google_granted_scopes = "https://www.googleapis.com/auth/calendar"
        session.add(sample_user)
        await session.commit()

        state = await hydrate_session_state("+14155552671", session)
        assert state["user:google_calendar_connected"] is True
        assert state["user:gmail_connected"] is False

    @pytest.mark.asyncio
    async def test_gmail_connected(self, session: AsyncSession, sample_user: User):
        sample_user.google_granted_scopes = (
            "https://www.googleapis.com/auth/gmail.modify"
        )
        session.add(sample_user)
        await session.commit()

        state = await hydrate_session_state("+14155552671", session)
        assert state["user:google_calendar_connected"] is False
        assert state["user:gmail_connected"] is True

    @pytest.mark.asyncio
    async def test_both_google_services_connected(
        self, session: AsyncSession, sample_user: User
    ):
        sample_user.google_granted_scopes = (
            "https://www.googleapis.com/auth/calendar "
            "https://www.googleapis.com/auth/gmail.modify"
        )
        session.add(sample_user)
        await session.commit()

        state = await hydrate_session_state("+14155552671", session)
        assert state["user:google_calendar_connected"] is True
        assert state["user:gmail_connected"] is True

    @pytest.mark.asyncio
    async def test_call_windows_hydrated(
        self, session: AsyncSession, sample_user: User
    ):
        # Create call windows for the user
        morning = CallWindow(
            user_id=sample_user.id,
            window_type="morning",
            start_time=time(7, 0),
            end_time=time(8, 0),
            is_active=True,
        )
        evening = CallWindow(
            user_id=sample_user.id,
            window_type="evening",
            start_time=time(21, 0),
            end_time=time(22, 0),
            is_active=True,
        )
        session.add(morning)
        session.add(evening)
        await session.commit()

        state = await hydrate_session_state("+14155552671", session)
        assert state["user:morning_call_start"] == "07:00"
        assert state["user:morning_call_end"] == "08:00"
        assert state["user:evening_call_start"] == "21:00"
        assert state["user:evening_call_end"] == "22:00"

    @pytest.mark.asyncio
    async def test_inactive_windows_excluded(
        self, session: AsyncSession, sample_user: User
    ):
        inactive = CallWindow(
            user_id=sample_user.id,
            window_type="afternoon",
            start_time=time(13, 0),
            end_time=time(14, 0),
            is_active=False,
        )
        session.add(inactive)
        await session.commit()

        state = await hydrate_session_state("+14155552671", session)
        assert "user:afternoon_call_start" not in state

    @pytest.mark.asyncio
    async def test_omits_none_name(self, session: AsyncSession, sample_user: User):
        """When name is None, the key should not be in state."""
        state = await hydrate_session_state("+14155552671", session)
        assert "user:name" not in state
