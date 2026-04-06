"""Property tests for preference persistence (P1, P2, P13).

P1  — Preference persistence round-trip: writing a preference via
      update_preferences and reading it back via get_by_phone returns
      the same value.
P2  — Name save idempotency: calling update_preferences with the same
      name that is already stored produces no DB write.
P13 — Write-through fails safely on DB error: update_preferences raises
      ValueError for non-existent users and restricted fields, and the
      DB is not modified in error cases.

All properties run against a real PostgreSQL test database using the same
async-per-example pattern as test_user_service.py.
"""

import asyncio
import os
import string

from dotenv import load_dotenv
from hypothesis import given, settings, strategies as st, HealthCheck
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.user import User
from app.services.user_service import UserService

load_dotenv()

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://charu:CJbJ7PsFrpbb29xsMBm3pkH5@localhost:5432/charu_ai_test",
)

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_e164_phone = st.sampled_from(
    [
        "+14155552671",
        "+447911123456",
        "+971501234567",
        "+919876543210",
        "+61412345678",
        "+4915112345678",
        "+33612345678",
        "+818012345678",
    ]
)

_user_name = st.text(
    alphabet=string.ascii_letters + string.digits + " -'",
    min_size=1,
    max_size=50,
).filter(lambda s: s.strip())

_timezone = st.sampled_from(
    [
        "America/New_York",
        "America/Los_Angeles",
        "Europe/London",
        "Asia/Kolkata",
        "Asia/Dubai",
        "Australia/Sydney",
        "Pacific/Auckland",
        "America/Chicago",
    ]
)

_onboarding_complete = st.booleans()


# ---------------------------------------------------------------------------
# Helper: run async DB operation in a fresh loop per Hypothesis example
# ---------------------------------------------------------------------------


def _run_async(coro):
    """Run an async coroutine in a new event loop (safe for Hypothesis @given)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _make_session():
    """Create engine, ensure tables exist, return (engine, session)."""
    import app.models  # noqa: F401 — register all table classes

    eng = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    return eng, factory()


async def _cleanup(session, phone: str):
    """Remove test data for the given phone."""
    await session.exec(
        sa_text("DELETE FROM users WHERE phone = :p"), params={"p": phone}
    )
    await session.commit()


# ---------------------------------------------------------------------------
# P1: Preference persistence round-trip
# **Validates: Requirements 1.2, 1.4, 7.1, 7.2**
# ---------------------------------------------------------------------------


@given(phone=_e164_phone, name=_user_name)
@settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow])
def test_name_round_trip(phone, name):
    """Writing name via update_preferences and reading back via get_by_phone
    returns the same value."""

    async def _test():
        eng, session = await _make_session()
        try:
            await _cleanup(session, phone)
            svc = UserService(session)

            # Create user first
            await svc.get_or_create_by_phone(phone)

            # Write preference
            await svc.update_preferences(phone, name=name)

            # Read back
            user = await svc.get_by_phone(phone)
            assert user is not None
            assert user.name == name

            await _cleanup(session, phone)
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())


@given(phone=_e164_phone, tz=_timezone)
@settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow])
def test_timezone_round_trip(phone, tz):
    """Writing timezone via update_preferences and reading back via get_by_phone
    returns the same value."""

    async def _test():
        eng, session = await _make_session()
        try:
            await _cleanup(session, phone)
            svc = UserService(session)

            await svc.get_or_create_by_phone(phone)
            await svc.update_preferences(phone, timezone=tz)

            user = await svc.get_by_phone(phone)
            assert user is not None
            assert user.timezone == tz

            await _cleanup(session, phone)
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())


@given(phone=_e164_phone, onboarding=_onboarding_complete)
@settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow])
def test_onboarding_complete_round_trip(phone, onboarding):
    """Writing onboarding_complete via update_preferences and reading back
    returns the same value."""

    async def _test():
        eng, session = await _make_session()
        try:
            await _cleanup(session, phone)
            svc = UserService(session)

            await svc.get_or_create_by_phone(phone)
            await svc.update_preferences(phone, onboarding_complete=onboarding)

            user = await svc.get_by_phone(phone)
            assert user is not None
            assert user.onboarding_complete == onboarding

            await _cleanup(session, phone)
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())


# ---------------------------------------------------------------------------
# P2: Name save idempotency
# **Validates: Requirements 1.5**
# ---------------------------------------------------------------------------


@given(phone=_e164_phone, name=_user_name)
@settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow])
def test_name_save_idempotency(phone, name):
    """Calling update_preferences with the same name that is already stored
    produces no DB write — the returned user has the same name and the
    updated_at timestamp does not change."""

    async def _test():
        eng, session = await _make_session()
        try:
            await _cleanup(session, phone)
            svc = UserService(session)

            # Create user and set name
            await svc.get_or_create_by_phone(phone)
            user1 = await svc.update_preferences(phone, name=name)
            assert user1.name == name
            updated_at_1 = user1.updated_at

            # Call again with the same name — should be idempotent
            user2 = await svc.update_preferences(phone, name=name)
            assert user2.name == name
            # updated_at should NOT have changed (no DB write)
            assert user2.updated_at == updated_at_1

            await _cleanup(session, phone)
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())


# ---------------------------------------------------------------------------
# P13: Write-through fails safely on DB error
# **Validates: Requirements 7.4**
# ---------------------------------------------------------------------------


@given(phone=_e164_phone)
@settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow])
def test_update_preferences_nonexistent_user_raises(phone):
    """Calling update_preferences with a phone that doesn't exist raises
    ValueError and the DB is not modified."""

    async def _test():
        eng, session = await _make_session()
        try:
            await _cleanup(session, phone)
            svc = UserService(session)

            # Ensure user does NOT exist
            user = await svc.get_by_phone(phone)
            assert user is None

            # Attempt to update preferences for non-existent user
            raised = False
            try:
                await svc.update_preferences(phone, name="ShouldFail")
            except ValueError:
                raised = True

            assert raised, "Expected ValueError for non-existent user"

            # Verify DB was not modified — no user was created
            user = await svc.get_by_phone(phone)
            assert user is None

            await _cleanup(session, phone)
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())


@given(
    phone=_e164_phone,
    restricted_field=st.sampled_from(
        [
            "firebase_uid",
            "google_access_token_encrypted",
            "google_refresh_token_encrypted",
            "last_login_at",
            "id",
            "last_active_date",
            "nonexistent_field",
        ]
    ),
)
@settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow])
def test_update_preferences_restricted_field_raises(phone, restricted_field):
    """Calling update_preferences with a restricted or unknown field raises
    ValueError and the DB is not modified."""

    async def _test():
        eng, session = await _make_session()
        try:
            await _cleanup(session, phone)
            svc = UserService(session)

            # Create user
            await svc.get_or_create_by_phone(phone)
            user_before = await svc.get_by_phone(phone)
            assert user_before is not None
            name_before = user_before.name
            tz_before = user_before.timezone

            # Attempt to update a restricted field
            raised = False
            try:
                await svc.update_preferences(phone, **{restricted_field: "bad_value"})
            except ValueError:
                raised = True

            assert raised, (
                f"Expected ValueError for restricted field '{restricted_field}'"
            )

            # Verify DB was not modified
            session.expire_all()
            user_after = await svc.get_by_phone(phone)
            assert user_after is not None
            assert user_after.name == name_before
            assert user_after.timezone == tz_before

            await _cleanup(session, phone)
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())
