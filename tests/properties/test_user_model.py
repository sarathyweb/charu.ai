"""Property tests for User model and phone normalization (P1, P2, P6a).

P1  — User data round-trip: store and retrieve by phone, all fields preserved.
P2  — Phone number uniqueness: two inserts with same phone → exactly one record.
P6a — Phone normalization consistency: same number in different formats → same E.164.

DB-backed properties (P1, P2) use @given with st.data() and run async operations
via a dedicated event loop + engine per example. This avoids the Hypothesis/asyncpg
event-loop conflict while preserving proper shrinking and replay.
"""

import asyncio
import os
import string
from datetime import datetime, timezone

import pytest
from dotenv import load_dotenv
from hypothesis import given, settings, strategies as st, HealthCheck
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.user import User
from app.utils import normalize_phone

load_dotenv()

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://charu:CJbJ7PsFrpbb29xsMBm3pkH5@localhost:5432/charu_ai_test",
)

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

e164_phones = st.builds(
    lambda cc, sub: f"+{cc}{sub}",
    cc=st.sampled_from(["1", "44", "971", "91", "61", "49", "33", "81"]),
    sub=st.text(alphabet=string.digits, min_size=8, max_size=10),
)

optional_names = st.one_of(
    st.none(),
    st.text(
        alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
        min_size=1,
        max_size=50,
    ).filter(lambda s: s.strip()),
)

optional_firebase_uids = st.one_of(
    st.none(),
    st.text(alphabet=string.ascii_letters + string.digits, min_size=10, max_size=40),
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
    import app.models  # noqa: F401
    eng = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    return eng, factory()


# ---------------------------------------------------------------------------
# P1: User data round-trip
# ---------------------------------------------------------------------------
@given(
    phone=e164_phones,
    name=optional_names,
    firebase_uid=optional_firebase_uids,
)
@settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow])
def test_user_round_trip(phone, name, firebase_uid):
    """Storing a User and retrieving by phone preserves all fields."""

    async def _test():
        eng, session = await _make_session()
        try:
            # Clean slate
            await session.exec(sa_text("DELETE FROM users WHERE phone = :p"), params={"p": phone})
            if firebase_uid is not None:
                await session.exec(
                    sa_text("DELETE FROM users WHERE firebase_uid = :u"),
                    params={"u": firebase_uid},
                )
            await session.commit()

            now = datetime.now(timezone.utc)
            user = User(
                phone=phone,
                name=name,
                firebase_uid=firebase_uid,
                created_at=now,
                last_login_at=now,
            )
            session.add(user)
            await session.commit()

            result = await session.exec(select(User).where(User.phone == phone))
            fetched = result.first()

            assert fetched is not None, f"User with phone {phone} not found"
            assert fetched.phone == phone
            assert fetched.name == name
            assert fetched.firebase_uid == firebase_uid
            assert fetched.last_login_at is not None

            # Clean up
            await session.exec(sa_text("DELETE FROM users WHERE phone = :p"), params={"p": phone})
            await session.commit()
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())


# ---------------------------------------------------------------------------
# P2: Phone number uniqueness enforcement
# ---------------------------------------------------------------------------
@given(phone=e164_phones)
@settings(max_examples=15, suppress_health_check=[HealthCheck.too_slow])
def test_phone_uniqueness(phone):
    """Inserting two users with the same phone results in exactly one record."""

    async def _test():
        eng, session = await _make_session()
        try:
            await session.exec(sa_text("DELETE FROM users WHERE phone = :p"), params={"p": phone})
            await session.commit()

            user1 = User(phone=phone)
            session.add(user1)
            await session.commit()

            duplicate_rejected = False
            try:
                async with session.begin_nested():
                    user2 = User(phone=phone)
                    session.add(user2)
                    await session.flush()
            except Exception:
                duplicate_rejected = True

            assert duplicate_rejected, f"Expected UNIQUE violation for phone {phone}"

            result = await session.exec(select(User).where(User.phone == phone))
            users = result.all()
            assert len(users) == 1, f"Expected 1 user, got {len(users)}"

            await session.exec(sa_text("DELETE FROM users WHERE phone = :p"), params={"p": phone})
            await session.commit()
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())


# ---------------------------------------------------------------------------
# P6a: Phone normalization consistency
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,region,expected",
    [
        # US variants → same E.164
        ("+14155552671", None, "+14155552671"),
        ("14155552671", "US", "+14155552671"),
        ("(415) 555-2671", "US", "+14155552671"),
        ("415-555-2671", "US", "+14155552671"),
        # UK variants → same E.164
        ("+447911123456", None, "+447911123456"),
        ("07911123456", "GB", "+447911123456"),
        ("07911 123456", "GB", "+447911123456"),
        # UAE variants → same E.164
        ("+971501234567", None, "+971501234567"),
        ("0501234567", "AE", "+971501234567"),
    ],
)
def test_normalize_phone_consistency(raw, region, expected):
    """Same phone in different formats normalizes to identical E.164."""
    assert normalize_phone(raw, region) == expected


@given(digits=st.text(alphabet="abcxyz!@#$%", min_size=1, max_size=15))
def test_normalize_phone_rejects_invalid(digits):
    """Invalid / non-numeric strings are rejected with ValueError."""
    with pytest.raises(ValueError, match="Invalid phone number"):
        normalize_phone(digits)


@given(phone=e164_phones)
@settings(max_examples=50)
def test_normalize_phone_idempotent(phone):
    """Normalizing an already-E.164 number returns the same string (if valid)."""
    try:
        result = normalize_phone(phone)
        assert normalize_phone(result) == result
    except ValueError:
        pass  # Some generated numbers may not be valid; that's fine
