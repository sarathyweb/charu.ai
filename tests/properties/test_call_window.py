"""Property tests for call window data models (Properties 3 & 4).

Property 3 — Call window validation rejects invalid inputs:
  For *any* call window where start_time >= end_time, or width < 20 min,
  or timezone is not a valid IANA identifier, ``validate_call_window``
  returns an error and no data is persisted.

Property 4 — Call window save idempotency:
  For *any* valid call window, saving it twice with the same parameters
  results in exactly one record in the database (upsert via unique
  constraint) with no duplicate entries.

Validates: Requirements 2.3, 2.4, 2.V1, 2.V2
"""

import asyncio
import os
import string
from datetime import datetime, time, timezone

from dotenv import load_dotenv
from hypothesis import given, settings, assume, HealthCheck
from hypothesis import strategies as st
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession
from zoneinfo import available_timezones

from app.models.call_window import CallWindow
from app.models.enums import WindowType
from app.services.call_window_validation import validate_call_window

load_dotenv()

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://charu:CJbJ7PsFrpbb29xsMBm3pkH5@localhost:5432/charu_ai_test",
)

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Valid IANA timezones (sample from the full set for speed)
SAMPLE_TIMEZONES = sorted(available_timezones())
valid_timezones = st.sampled_from(SAMPLE_TIMEZONES)

# Invalid timezone strings — guaranteed not in the IANA set
invalid_timezones = st.text(
    alphabet=string.ascii_letters + string.digits + "/_-",
    min_size=1,
    max_size=30,
).filter(lambda s: s not in available_timezones())

# Times as (hour, minute) pairs
time_components = st.tuples(
    st.integers(min_value=0, max_value=23),
    st.integers(min_value=0, max_value=59),
)

window_types = st.sampled_from([wt.value for wt in WindowType])

e164_phones = st.builds(
    lambda cc, sub: f"+{cc}{sub}",
    cc=st.sampled_from(["1", "44", "971", "91", "61"]),
    sub=st.text(alphabet=string.digits, min_size=8, max_size=10),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_async(coro):
    """Run an async coroutine in a new event loop (safe for Hypothesis)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _make_session():
    """Create engine, ensure tables exist, return (engine, session)."""
    import app.models  # noqa: F401 — register all models

    eng = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    return eng, factory()


# ===================================================================
# Property 3: Call window validation rejects invalid inputs
# ===================================================================


@given(
    start_hm=time_components,
    end_hm=time_components,
    tz=valid_timezones,
)
@settings(max_examples=100)
def test_rejects_start_at_or_after_end(start_hm, end_hm, tz):
    """Windows where start >= end are rejected."""
    start_minutes = start_hm[0] * 60 + start_hm[1]
    end_minutes = end_hm[0] * 60 + end_hm[1]
    assume(end_minutes <= start_minutes)

    ok, err = validate_call_window(
        time(start_hm[0], start_hm[1]),
        time(end_hm[0], end_hm[1]),
        tz,
    )
    assert not ok
    assert err is not None
    assert "after start time" in err.lower() or "cross-midnight" in err.lower()


@given(
    start_hm=time_components,
    gap=st.integers(min_value=1, max_value=19),
    tz=valid_timezones,
)
@settings(max_examples=100)
def test_rejects_window_narrower_than_20_minutes(start_hm, gap, tz):
    """Windows narrower than 20 minutes are rejected."""
    start_minutes = start_hm[0] * 60 + start_hm[1]
    end_minutes = start_minutes + gap
    # Ensure end doesn't wrap past midnight
    assume(end_minutes < 24 * 60)
    # Ensure start < end (so we don't hit the ordering check first)
    assume(end_minutes > start_minutes)

    ok, err = validate_call_window(
        time(start_hm[0], start_hm[1]),
        time(end_minutes // 60, end_minutes % 60),
        tz,
    )
    assert not ok
    assert err is not None
    assert "20 minutes" in err or "minutes wide" in err.lower()


@given(
    start_hm=time_components,
    gap=st.integers(min_value=20, max_value=120),
    tz=invalid_timezones,
)
@settings(max_examples=50)
def test_rejects_invalid_timezone(start_hm, gap, tz):
    """Invalid IANA timezone strings are rejected even with valid times."""
    start_minutes = start_hm[0] * 60 + start_hm[1]
    end_minutes = start_minutes + gap
    assume(end_minutes < 24 * 60)

    ok, err = validate_call_window(
        time(start_hm[0], start_hm[1]),
        time(end_minutes // 60, end_minutes % 60),
        tz,
    )
    assert not ok
    assert err is not None
    assert "timezone" in err.lower() or "iana" in err.lower()


@given(
    start_hm=time_components,
    gap=st.integers(min_value=20, max_value=300),
    tz=valid_timezones,
)
@settings(max_examples=100)
def test_accepts_valid_window(start_hm, gap, tz):
    """Valid windows (width >= 20 min, start < end, valid tz) are accepted."""
    start_minutes = start_hm[0] * 60 + start_hm[1]
    end_minutes = start_minutes + gap
    assume(end_minutes < 24 * 60)

    ok, err = validate_call_window(
        time(start_hm[0], start_hm[1]),
        time(end_minutes // 60, end_minutes % 60),
        tz,
    )
    assert ok
    assert err is None


# ===================================================================
# Property 4: Call window save idempotency
# ===================================================================


@given(
    phone=e164_phones,
    wtype=window_types,
    start_hm=time_components,
    gap=st.integers(min_value=20, max_value=120),
    tz=valid_timezones,
)
@settings(max_examples=15, suppress_health_check=[HealthCheck.too_slow])
def test_call_window_save_idempotent(phone, wtype, start_hm, gap, tz):
    """Saving the same call window twice yields exactly one DB record."""
    start_minutes = start_hm[0] * 60 + start_hm[1]
    end_minutes = start_minutes + gap
    assume(end_minutes < 24 * 60)

    start = time(start_hm[0], start_hm[1])
    end = time(end_minutes // 60, end_minutes % 60)

    async def _test():
        eng, session = await _make_session()
        try:
            # Ensure a user exists for the FK
            await session.exec(
                sa_text(
                    "INSERT INTO users (phone, onboarding_complete, consecutive_active_days, created_at) "
                    "VALUES (:p, false, 0, now()) ON CONFLICT (phone) DO NOTHING"
                ),
                params={"p": phone},
            )
            await session.commit()

            # Fetch user id
            row = (
                await session.exec(
                    sa_text("SELECT id FROM users WHERE phone = :p"),
                    params={"p": phone},
                )
            ).first()
            user_id = row[0]

            # Clean any prior windows for this user+type
            await session.exec(
                sa_text(
                    "DELETE FROM call_windows "
                    "WHERE user_id = :uid AND window_type = :wt"
                ),
                params={"uid": user_id, "wt": wtype},
            )
            await session.commit()

            now = datetime.now(timezone.utc)

            # First save
            cw1 = CallWindow(
                user_id=user_id,
                window_type=wtype,
                start_time=start,
                end_time=end,
                is_active=True,
                created_at=now,
            )
            session.add(cw1)
            await session.commit()

            # Second save — should violate unique constraint
            duplicate_rejected = False
            try:
                async with session.begin_nested():
                    cw2 = CallWindow(
                        user_id=user_id,
                        window_type=wtype,
                        start_time=start,
                        end_time=end,
                        is_active=True,
                        created_at=now,
                    )
                    session.add(cw2)
                    await session.flush()
            except Exception:
                duplicate_rejected = True

            assert duplicate_rejected, (
                f"Expected UNIQUE violation for user_id={user_id}, window_type={wtype}"
            )

            # Verify exactly one record
            result = await session.exec(
                select(CallWindow).where(
                    CallWindow.user_id == user_id,
                    CallWindow.window_type == wtype,
                )
            )
            windows = result.all()
            assert len(windows) == 1, f"Expected 1 window, got {len(windows)}"
            assert windows[0].start_time == start
            assert windows[0].end_time == end

            # Cleanup
            await session.exec(
                sa_text(
                    "DELETE FROM call_windows "
                    "WHERE user_id = :uid AND window_type = :wt"
                ),
                params={"uid": user_id, "wt": wtype},
            )
            await session.commit()
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())


@given(
    phone=e164_phones,
    start_hm=time_components,
    gap=st.integers(min_value=20, max_value=120),
    tz=valid_timezones,
)
@settings(max_examples=10, suppress_health_check=[HealthCheck.too_slow])
def test_different_window_types_coexist(phone, start_hm, gap, tz):
    """Different window types for the same user can coexist independently."""
    start_minutes = start_hm[0] * 60 + start_hm[1]
    end_minutes = start_minutes + gap
    assume(end_minutes < 24 * 60)

    start = time(start_hm[0], start_hm[1])
    end = time(end_minutes // 60, end_minutes % 60)

    async def _test():
        eng, session = await _make_session()
        try:
            # Ensure user
            await session.exec(
                sa_text(
                    "INSERT INTO users (phone, onboarding_complete, consecutive_active_days, created_at) "
                    "VALUES (:p, false, 0, now()) ON CONFLICT (phone) DO NOTHING"
                ),
                params={"p": phone},
            )
            await session.commit()

            row = (
                await session.exec(
                    sa_text("SELECT id FROM users WHERE phone = :p"),
                    params={"p": phone},
                )
            ).first()
            user_id = row[0]

            # Clean slate
            await session.exec(
                sa_text("DELETE FROM call_windows WHERE user_id = :uid"),
                params={"uid": user_id},
            )
            await session.commit()

            now = datetime.now(timezone.utc)

            # Insert all three window types
            for wt in [
                WindowType.MORNING.value,
                WindowType.AFTERNOON.value,
                WindowType.EVENING.value,
            ]:
                cw = CallWindow(
                    user_id=user_id,
                    window_type=wt,
                    start_time=start,
                    end_time=end,
                    is_active=True,
                    created_at=now,
                )
                session.add(cw)
            await session.commit()

            # Verify three distinct records
            result = await session.exec(
                select(CallWindow).where(CallWindow.user_id == user_id)
            )
            windows = result.all()
            assert len(windows) == 3, f"Expected 3 windows, got {len(windows)}"

            types_found = {w.window_type for w in windows}
            assert types_found == {"morning", "afternoon", "evening"}

            # Cleanup
            await session.exec(
                sa_text("DELETE FROM call_windows WHERE user_id = :uid"),
                params={"uid": user_id},
            )
            await session.commit()
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())
