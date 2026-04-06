"""Property test for onboarding resumption (P14).

P14 — Onboarding resumption skips completed steps: when a user abandons
      onboarding mid-flow and returns later, the system hydrates session
      state from the DB so that completed steps are detected and skipped.
      Specifically, ``hydrate_session_state`` must populate exactly the
      ``user:`` state keys that correspond to persisted data, and leave
      absent any keys for steps not yet completed.

Validates: Requirement 8.5

The single onboarding Agent uses state-driven instructions to check
these state keys and determine which step to handle next.  This test
validates the data layer contract: for any arbitrary subset of completed
onboarding steps, hydrate_session_state returns the correct state dict.
"""

import asyncio
import os
from datetime import time

from dotenv import load_dotenv
from hypothesis import given, settings, strategies as st, HealthCheck
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.user import User
from app.models.call_window import CallWindow
from app.models.enums import WindowType
from app.services.user_service import UserService, hydrate_session_state

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
    ]
)

_user_name = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ '-",
    min_size=1,
    max_size=30,
).filter(lambda s: s.strip())

_timezone = st.sampled_from(
    [
        "America/New_York",
        "America/Los_Angeles",
        "Europe/London",
        "Asia/Kolkata",
        "Asia/Dubai",
        "Australia/Sydney",
    ]
)

# Strategy for a valid call window (start < end, ≥20 min wide)
_call_window_times = st.tuples(
    st.integers(min_value=0, max_value=22),   # start hour
    st.integers(min_value=0, max_value=59),   # start minute
).flatmap(
    lambda start: st.tuples(
        st.just(time(start[0], start[1])),
        # end must be ≥20 min after start and within same day
        st.integers(
            min_value=start[0] * 60 + start[1] + 20,
            max_value=23 * 60 + 59,
        ).map(lambda m: time(m // 60, m % 60)),
    )
)

# Which onboarding steps are "completed" — each is independently toggled
_completed_steps = st.fixed_dictionaries(
    {
        "name": st.one_of(st.none(), _user_name),
        "timezone": st.one_of(st.none(), _timezone),
        "morning_window": st.one_of(st.none(), _call_window_times),
        "afternoon_window": st.one_of(st.none(), _call_window_times),
        "evening_window": st.one_of(st.none(), _call_window_times),
        "calendar_connected": st.booleans(),
        "gmail_connected": st.booleans(),
        "onboarding_complete": st.booleans(),
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _make_session():
    import app.models  # noqa: F401

    eng = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    return eng, factory()


async def _cleanup(session: AsyncSession, phone: str):
    """Remove all test data for the given phone."""
    # Delete call windows first (FK constraint)
    await session.exec(
        sa_text(
            "DELETE FROM call_windows WHERE user_id IN "
            "(SELECT id FROM users WHERE phone = :p)"
        ),
        params={"p": phone},
    )
    await session.exec(
        sa_text("DELETE FROM users WHERE phone = :p"),
        params={"p": phone},
    )
    await session.commit()


def _build_scopes(calendar: bool, gmail: bool) -> str | None:
    """Build a google_granted_scopes string from connection flags."""
    parts = []
    if calendar:
        parts.append("https://www.googleapis.com/auth/calendar")
    if gmail:
        parts.append("https://www.googleapis.com/auth/gmail.modify")
    return " ".join(parts) if parts else None


# ---------------------------------------------------------------------------
# P14: Onboarding resumption skips completed steps
# **Validates: Requirements 8.5**
# ---------------------------------------------------------------------------


@given(phone=_e164_phone, steps=_completed_steps)
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_hydrate_state_reflects_completed_steps(phone, steps):
    """For any subset of completed onboarding steps persisted in the DB,
    hydrate_session_state returns exactly the state keys that the
    onboarding agent's instruction checks to determine the next step.

    Completed steps → corresponding ``user:`` key is present and correct.
    Incomplete steps → corresponding ``user:`` key is absent.
    """

    async def _test():
        eng, session = await _make_session()
        try:
            await _cleanup(session, phone)
            svc = UserService(session)

            # --- Arrange: create user with the given completed steps ---
            user = await svc.get_or_create_by_phone(phone)

            # Name
            if steps["name"] is not None:
                user.name = steps["name"]

            # Timezone (required before windows can be created)
            if steps["timezone"] is not None:
                user.timezone = steps["timezone"]

            # Google scopes
            user.google_granted_scopes = _build_scopes(
                steps["calendar_connected"],
                steps["gmail_connected"],
            )

            # Onboarding complete flag
            user.onboarding_complete = steps["onboarding_complete"]

            session.add(user)
            await session.commit()
            await session.refresh(user)

            # Call windows — only create if timezone is set (DB FK needs user_id)
            window_types = {
                "morning_window": WindowType.MORNING,
                "afternoon_window": WindowType.AFTERNOON,
                "evening_window": WindowType.EVENING,
            }
            for step_key, wtype in window_types.items():
                if steps[step_key] is not None and steps["timezone"] is not None:
                    start_t, end_t = steps[step_key]
                    cw = CallWindow(
                        user_id=user.id,
                        window_type=wtype.value,
                        start_time=start_t,
                        end_time=end_t,
                        is_active=True,
                    )
                    session.add(cw)
            await session.commit()

            # --- Act: hydrate session state ---
            state = await hydrate_session_state(phone, session)

            # --- Assert: state keys match completed steps ---

            # Phone is always present
            assert state["phone"] == phone

            # Step 1: name
            if steps["name"] is not None:
                assert state.get("user:name") == steps["name"], (
                    f"Expected user:name={steps['name']!r}, got {state.get('user:name')!r}"
                )
            else:
                assert "user:name" not in state, (
                    f"user:name should be absent but got {state.get('user:name')!r}"
                )

            # Step 2: timezone
            if steps["timezone"] is not None:
                assert state.get("user:timezone") == steps["timezone"]
            else:
                assert "user:timezone" not in state

            # Step 3-5: call windows
            for step_key, wtype in window_types.items():
                wt = wtype.value  # "morning", "afternoon", "evening"
                start_key = f"user:{wt}_call_start"
                end_key = f"user:{wt}_call_end"

                if steps[step_key] is not None and steps["timezone"] is not None:
                    start_t, end_t = steps[step_key]
                    assert state.get(start_key) == start_t.strftime("%H:%M"), (
                        f"Expected {start_key}={start_t.strftime('%H:%M')}, "
                        f"got {state.get(start_key)!r}"
                    )
                    assert state.get(end_key) == end_t.strftime("%H:%M"), (
                        f"Expected {end_key}={end_t.strftime('%H:%M')}, "
                        f"got {state.get(end_key)!r}"
                    )
                else:
                    assert start_key not in state, (
                        f"{start_key} should be absent but got {state.get(start_key)!r}"
                    )
                    assert end_key not in state, (
                        f"{end_key} should be absent but got {state.get(end_key)!r}"
                    )

            # Step 6: Google Calendar connected
            assert state.get("user:google_calendar_connected") == steps["calendar_connected"]

            # Step 7: Gmail connected
            assert state.get("user:google_gmail_connected") == steps["gmail_connected"]

            # Step 8: onboarding complete
            assert state.get("user:onboarding_complete") == steps["onboarding_complete"]

            await _cleanup(session, phone)
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())


@given(phone=_e164_phone, name=_user_name, tz=_timezone)
@settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow])
def test_partial_onboarding_resumes_at_correct_step(phone, name, tz):
    """Simulates a user who completed name + timezone + morning window
    but abandoned before afternoon window.  On resumption, the hydrated
    state should have keys for the completed steps and be missing keys
    for the incomplete steps — proving the onboarding agent's instruction
    would skip steps 1-3 and resume at step 4."""

    async def _test():
        eng, session = await _make_session()
        try:
            await _cleanup(session, phone)
            svc = UserService(session)

            # Create user with partial onboarding
            user = await svc.get_or_create_by_phone(phone)
            user.name = name
            user.timezone = tz
            user.onboarding_complete = False
            session.add(user)
            await session.commit()
            await session.refresh(user)

            # Only morning window completed
            cw = CallWindow(
                user_id=user.id,
                window_type=WindowType.MORNING.value,
                start_time=time(7, 0),
                end_time=time(8, 0),
                is_active=True,
            )
            session.add(cw)
            await session.commit()

            # Hydrate state
            state = await hydrate_session_state(phone, session)

            # Steps 1-3 completed → keys present
            assert state.get("user:name") == name
            assert state.get("user:timezone") == tz
            assert "user:morning_call_start" in state
            assert "user:morning_call_end" in state

            # Steps 4-5 not completed → keys absent
            assert "user:afternoon_call_start" not in state
            assert "user:afternoon_call_end" not in state
            assert "user:evening_call_start" not in state
            assert "user:evening_call_end" not in state

            # Steps 6-7 not completed → flags false
            assert state.get("user:google_calendar_connected") is False
            assert state.get("user:google_gmail_connected") is False

            # Step 8 not completed
            assert state.get("user:onboarding_complete") is False

            await _cleanup(session, phone)
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())


@given(phone=_e164_phone)
@settings(max_examples=10, suppress_health_check=[HealthCheck.too_slow])
def test_brand_new_user_has_no_completed_steps(phone):
    """A brand-new user (just created, no data) should have no onboarding
    state keys set — the onboarding agent would start from step 1."""

    async def _test():
        eng, session = await _make_session()
        try:
            await _cleanup(session, phone)
            svc = UserService(session)

            # Create bare user
            await svc.get_or_create_by_phone(phone)

            state = await hydrate_session_state(phone, session)

            # Only phone and default flags should be present
            assert state["phone"] == phone
            assert "user:name" not in state
            assert "user:timezone" not in state
            assert "user:morning_call_start" not in state
            assert "user:afternoon_call_start" not in state
            assert "user:evening_call_start" not in state
            assert state.get("user:google_calendar_connected") is False
            assert state.get("user:google_gmail_connected") is False
            assert state.get("user:onboarding_complete") is False

            await _cleanup(session, phone)
        finally:
            await session.close()
            await eng.dispose()

    _run_async(_test())
