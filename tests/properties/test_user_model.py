"""Property tests for User model and phone normalization (P1, P2, P6a).

P1  — User data round-trip: store and retrieve by phone, all fields preserved.
P2  — Phone number uniqueness: two inserts with same phone → exactly one record.
P6a — Phone normalization consistency: same number in different formats → same E.164.

NOTE: P1 and P2 use Hypothesis to generate test data but run DB operations in a
single async test per property. Combining @given with async fixtures that hold a
DB connection causes event-loop conflicts with asyncpg. Instead we draw examples
explicitly inside the async test body.
"""

import string
from datetime import datetime, timezone

import pytest
from hypothesis import given, settings, strategies as st, HealthCheck
from hypothesis.core import given as _  # noqa — ensure hypothesis is importable
from sqlalchemy import text as sa_text
from sqlmodel import select

from app.models.user import User
from app.utils import normalize_phone

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
    st.text(min_size=1, max_size=50).filter(lambda s: s.strip()),
)

optional_firebase_uids = st.one_of(
    st.none(),
    st.text(alphabet=string.ascii_letters + string.digits, min_size=10, max_size=40),
)


# ---------------------------------------------------------------------------
# P1: User data round-trip
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_user_round_trip(session):
    """Storing a User and retrieving by phone preserves all fields."""
    # Draw multiple examples from Hypothesis strategies
    examples = [
        (e164_phones.example(), optional_names.example(), optional_firebase_uids.example())
        for _ in range(20)
    ]

    for phone, name, firebase_uid in examples:
        # Clean slate
        await session.exec(sa_text("DELETE FROM users WHERE phone = :p"), params={"p": phone})
        if firebase_uid is not None:
            await session.exec(
                sa_text("DELETE FROM users WHERE firebase_uid = :u"),
                params={"u": firebase_uid},
            )
        await session.flush()

        now = datetime.now(timezone.utc)
        user = User(
            phone=phone,
            name=name,
            firebase_uid=firebase_uid,
            created_at=now,
            last_login_at=now,
        )
        session.add(user)
        await session.flush()

        result = await session.exec(select(User).where(User.phone == phone))
        fetched = result.first()

        assert fetched is not None, f"User with phone {phone} not found after insert"
        assert fetched.phone == phone
        assert fetched.name == name
        assert fetched.firebase_uid == firebase_uid
        assert fetched.last_login_at is not None

        # Rollback for next iteration
        await session.rollback()


# ---------------------------------------------------------------------------
# P2: Phone number uniqueness enforcement
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_phone_uniqueness(session):
    """Inserting two users with the same phone results in exactly one record."""
    examples = [e164_phones.example() for _ in range(15)]

    for phone in examples:
        # Clean slate
        await session.exec(sa_text("DELETE FROM users WHERE phone = :p"), params={"p": phone})
        await session.commit()

        # First insert — should succeed
        user1 = User(phone=phone)
        session.add(user1)
        await session.commit()

        # Second insert with same phone — use a savepoint so the IntegrityError
        # doesn't invalidate the outer transaction
        duplicate_rejected = False
        try:
            async with session.begin_nested():
                user2 = User(phone=phone)
                session.add(user2)
                await session.flush()
        except Exception:
            duplicate_rejected = True

        assert duplicate_rejected, f"Expected UNIQUE violation for phone {phone}"

        # Verify exactly one record
        result = await session.exec(select(User).where(User.phone == phone))
        users = result.all()
        assert len(users) == 1, f"Expected 1 user for phone {phone}, got {len(users)}"

        # Clean up for next iteration
        await session.exec(sa_text("DELETE FROM users WHERE phone = :p"), params={"p": phone})
        await session.commit()


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


@given(
    digits=st.text(alphabet="abcxyz!@#$%", min_size=1, max_size=15),
)
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
        # If it's valid, normalizing again should be identical
        assert normalize_phone(result) == result
    except ValueError:
        pass  # Some generated numbers may not be valid; that's fine
