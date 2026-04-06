"""Property tests for UserService (P3, P4, P5, P5a, P15).

P3  — User creation by channel: web auth → non-null firebase_uid;
      WhatsApp → firebase_uid=None.
P4  — Cross-channel identity linking: WhatsApp user + web auth with same
      phone → single record with UID linked.
P5  — Last login timestamp monotonicity: successive auths produce
      non-decreasing last_login_at.
P5a — Firebase UID conflict detection: user with UID-A, ensure_from_firebase
      with same phone + UID-B → rejection, UID-A unchanged.
P15 — Verified auth ensures user record exists: after successful Firebase
      auth, user record exists in DB.

All properties run against a real PostgreSQL test database using the same
async-per-example pattern as test_user_model.py.
"""

import asyncio
import os
import string
from datetime import datetime, timezone

import pytest
from dotenv import load_dotenv
from fastapi import HTTPException
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

# Valid E.164 phones that phonenumbers will accept
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

_firebase_uid = st.text(
    alphabet=string.ascii_letters + string.digits,
    min_size=10,
    max_size=40,
)


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


async def _cleanup(session, phone: str, firebase_uid: str | None = None):
    """Remove test data for the given phone (and optionally firebase_uid)."""
    await session.exec(
        sa_text("DELETE FROM users WHERE phone = :p"), params={"p": phone}
    )
    if firebase_uid is not None:
        await session.exec(
            sa_text("DELETE FROM users WHERE firebase_uid = :u"),
            params={"u": firebase_uid},
        )
    await session.commit()


# ---------------------------------------------------------------------------
# P3: User creation by channel
# **Validates: Requirements 2.4, 2.6**
# ---------------------------------------------------------------------------


@given(phone=_e164_phone, uid=_firebase_uid)
@settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow])
def test_web_auth_creates_user_with_firebase_uid(phone, uid):
    """Web auth (ensure_from_firebase) creates a user with non-null firebase_uid."""

    async def _test():
        eng, session = await _make_session()
        try:
            await _cleanup(session, phone, uid)
            svc = UserService(session)
            user, created = await svc.ensure_from_firebase(phone, uid)

            assert user is not None
            assert user.phone == phone
            assert user.firebase_uid == uid
            assert user.firebase_uid is not None
            assert created is True

            await _cleanup(session, phone, uid)
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())


@given(phone=_e164_phone)
@settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow])
def test_whatsapp_creates_user_without_firebase_uid(phone):
    """WhatsApp (ensure_from_whatsapp) creates a user with firebase_uid=None."""

    async def _test():
        eng, session = await _make_session()
        try:
            await _cleanup(session, phone)
            svc = UserService(session)
            user = await svc.ensure_from_whatsapp(phone)

            assert user is not None
            assert user.phone == phone
            assert user.firebase_uid is None

            await _cleanup(session, phone)
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())


# ---------------------------------------------------------------------------
# WhatsApp 24-hour window: ensure_from_whatsapp sets timestamp
# **Validates: Bug fix — last_user_whatsapp_message_at must be written**
# ---------------------------------------------------------------------------


@given(phone=_e164_phone)
@settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow])
def test_whatsapp_sets_last_message_timestamp_on_create(phone):
    """ensure_from_whatsapp sets last_user_whatsapp_message_at for new users."""

    async def _test():
        eng, session = await _make_session()
        try:
            await _cleanup(session, phone)
            before = datetime.now(timezone.utc)
            svc = UserService(session)
            user = await svc.ensure_from_whatsapp(phone)

            assert user.last_user_whatsapp_message_at is not None
            assert user.last_user_whatsapp_message_at >= before

            await _cleanup(session, phone)
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())


@given(phone=_e164_phone)
@settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow])
def test_whatsapp_updates_last_message_timestamp_on_existing(phone):
    """ensure_from_whatsapp updates last_user_whatsapp_message_at for existing users."""

    async def _test():
        eng, session = await _make_session()
        try:
            await _cleanup(session, phone)
            svc = UserService(session)

            # First call — creates user
            user1 = await svc.ensure_from_whatsapp(phone)
            ts1 = user1.last_user_whatsapp_message_at
            assert ts1 is not None

            # Second call — updates timestamp
            user2 = await svc.ensure_from_whatsapp(phone)
            ts2 = user2.last_user_whatsapp_message_at
            assert ts2 is not None
            assert ts2 >= ts1, (
                f"last_user_whatsapp_message_at went backwards: {ts2} < {ts1}"
            )

            await _cleanup(session, phone)
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())


# ---------------------------------------------------------------------------
# P4: Cross-channel identity linking
# **Validates: Requirements 2.7**
# ---------------------------------------------------------------------------


@given(phone=_e164_phone, uid=_firebase_uid)
@settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow])
def test_cross_channel_identity_linking(phone, uid):
    """WhatsApp user + web auth with same phone → single record with UID linked."""

    async def _test():
        eng, session = await _make_session()
        try:
            await _cleanup(session, phone, uid)
            svc = UserService(session)

            # Step 1: WhatsApp creates user (no UID)
            wa_user = await svc.ensure_from_whatsapp(phone)
            assert wa_user.firebase_uid is None

            # Step 2: Web auth links UID to same phone
            web_user, _ = await svc.ensure_from_firebase(phone, uid)
            assert web_user.firebase_uid == uid
            assert web_user.phone == phone

            # Step 3: Verify only one record exists
            result = await session.exec(select(User).where(User.phone == phone))
            users = result.all()
            assert len(users) == 1, (
                f"Expected 1 user for phone {phone}, got {len(users)}"
            )
            assert users[0].firebase_uid == uid

            await _cleanup(session, phone, uid)
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())


# ---------------------------------------------------------------------------
# P5: Last login timestamp monotonicity
# **Validates: Requirements 2.5**
# ---------------------------------------------------------------------------


@given(phone=_e164_phone, uid=_firebase_uid)
@settings(max_examples=15, suppress_health_check=[HealthCheck.too_slow])
def test_last_login_timestamp_monotonicity(phone, uid):
    """Successive auths produce non-decreasing last_login_at."""

    async def _test():
        eng, session = await _make_session()
        try:
            await _cleanup(session, phone, uid)
            svc = UserService(session)

            # First auth — establishes baseline
            user1, _ = await svc.ensure_from_firebase(phone, uid)
            ts1 = user1.last_login_at
            assert ts1 is not None

            # Second auth — should be >= first
            user2, _ = await svc.ensure_from_firebase(phone, uid)
            ts2 = user2.last_login_at
            assert ts2 is not None
            assert ts2 >= ts1, f"last_login_at went backwards: {ts2} < {ts1}"

            # Third auth — should be >= second
            user3, _ = await svc.ensure_from_firebase(phone, uid)
            ts3 = user3.last_login_at
            assert ts3 is not None
            assert ts3 >= ts2, f"last_login_at went backwards: {ts3} < {ts2}"

            await _cleanup(session, phone, uid)
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())


# ---------------------------------------------------------------------------
# P5a: Firebase UID conflict detection
# **Validates: Requirements 2.7 (edge case)**
# ---------------------------------------------------------------------------


@given(
    phone=_e164_phone,
    uid_a=_firebase_uid,
    uid_b=_firebase_uid,
)
@settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow])
def test_firebase_uid_conflict_detection(phone, uid_a, uid_b):
    """User with UID-A, ensure_from_firebase with same phone + UID-B → rejection."""
    # Only test when UIDs are actually different
    if uid_a == uid_b:
        return

    async def _test():
        eng, session = await _make_session()
        try:
            await _cleanup(session, phone, uid_a)
            # Also clean uid_b in case it's linked to another phone
            await session.exec(
                sa_text("DELETE FROM users WHERE firebase_uid = :u"),
                params={"u": uid_b},
            )
            await session.commit()

            svc = UserService(session)

            # Create user with UID-A
            user, _ = await svc.ensure_from_firebase(phone, uid_a)
            assert user.firebase_uid == uid_a

            # Attempt with UID-B on same phone → should raise HTTP 409
            with pytest.raises(HTTPException) as exc_info:
                await svc.ensure_from_firebase(phone, uid_b)

            assert exc_info.value.status_code == 409

            # Verify original UID is unchanged
            session.expire_all()
            result = await session.exec(select(User).where(User.phone == phone))
            unchanged_user = result.first()
            assert unchanged_user is not None
            assert unchanged_user.firebase_uid == uid_a, (
                f"UID was changed from {uid_a} to {unchanged_user.firebase_uid}"
            )

            await _cleanup(session, phone, uid_a)
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())


# ---------------------------------------------------------------------------
# P15: Verified auth ensures user record exists
# **Validates: Requirements 9.7**
# ---------------------------------------------------------------------------


@given(phone=_e164_phone, uid=_firebase_uid)
@settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow])
def test_verified_auth_ensures_user_exists(phone, uid):
    """After successful Firebase auth, a user record with that phone exists in DB."""

    async def _test():
        eng, session = await _make_session()
        try:
            await _cleanup(session, phone, uid)
            svc = UserService(session)

            # Simulate what the API handler does after JWT verification:
            # call ensure_from_firebase
            await svc.ensure_from_firebase(phone, uid)

            # Verify user record exists
            result = await session.exec(select(User).where(User.phone == phone))
            user = result.first()
            assert user is not None, (
                f"No user record found for phone {phone} after auth"
            )
            assert user.phone == phone

            await _cleanup(session, phone, uid)
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())
