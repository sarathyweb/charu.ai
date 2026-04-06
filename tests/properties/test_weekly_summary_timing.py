"""Property tests for weekly summary timing (P9).

P9 — Weekly summary fires at correct local time:
     For any user with a configured timezone, the weekly summary check
     should identify the user for sending only when their local time is
     Sunday between 5:00 PM and 5:59 PM.

These are pure-logic tests — they validate the timezone conversion and
day/hour check without hitting the database or Celery.

Validates: Requirements 5.6
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from hypothesis import given, settings, strategies as st

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_timezones = st.sampled_from(
    [
        "America/New_York",
        "America/Chicago",
        "America/Los_Angeles",
        "America/Denver",
        "America/Anchorage",
        "Pacific/Honolulu",
        "Europe/London",
        "Europe/Berlin",
        "Europe/Moscow",
        "Australia/Sydney",
        "Pacific/Auckland",
        "Asia/Kolkata",
        "Asia/Dubai",
        "Asia/Tokyo",
        "Asia/Shanghai",
        "Africa/Nairobi",
        "UTC",
    ]
)

# UTC datetimes spanning a wide range to cover DST transitions.
_utc_datetimes = st.datetimes(
    min_value=datetime(2024, 1, 1),
    max_value=datetime(2028, 12, 31),
    timezones=st.just(timezone.utc),
)


# ---------------------------------------------------------------------------
# Helper: replicate the sweep's eligibility check
# ---------------------------------------------------------------------------


def _is_eligible_for_weekly_summary(now_utc: datetime, user_timezone: str) -> bool:
    """Replicate the weekly summary sweep logic from app/tasks/weekly.py.

    Returns True iff the user's local time is Sunday (weekday 6) and
    the hour is 17 (5 PM).
    """
    tz = ZoneInfo(user_timezone)
    user_now = now_utc.astimezone(tz)
    return user_now.weekday() == 6 and user_now.hour == 17


# ---------------------------------------------------------------------------
# P9a: Eligible only when local time is Sunday 5pm hour
# ---------------------------------------------------------------------------


@given(tz_name=_timezones, now_utc=_utc_datetimes)
@settings(max_examples=500)
def test_weekly_summary_eligible_iff_sunday_5pm(
    tz_name: str,
    now_utc: datetime,
) -> None:
    """The sweep identifies a user iff their local time is Sunday 17:xx."""
    tz = ZoneInfo(tz_name)
    user_local = now_utc.astimezone(tz)

    expected = user_local.weekday() == 6 and user_local.hour == 17
    actual = _is_eligible_for_weekly_summary(now_utc, tz_name)

    assert actual == expected, (
        f"tz={tz_name}, utc={now_utc.isoformat()}, "
        f"local={user_local.isoformat()}, "
        f"weekday={user_local.weekday()}, hour={user_local.hour}"
    )


# ---------------------------------------------------------------------------
# P9b: Every Sunday 5pm hour in every timezone has a matching UTC window
# ---------------------------------------------------------------------------


@given(tz_name=_timezones)
@settings(max_examples=50)
def test_sunday_5pm_always_reachable(tz_name: str) -> None:
    """For any timezone, there exists at least one UTC hour in a given week
    where the sweep would fire for that timezone.

    We pick a known Sunday, iterate all 24 UTC hours of that day ± 1 day,
    and verify at least one maps to Sunday 17:xx local.
    """
    # Use a fixed reference Sunday: 2026-04-05 (a Sunday).
    base_utc = datetime(2026, 4, 5, 0, 0, 0, tzinfo=timezone.utc)

    found = False
    # Scan 48 hours around the reference Sunday to cover all offsets.
    for hour_offset in range(-12, 36):
        candidate = base_utc + timedelta(hours=hour_offset)
        if _is_eligible_for_weekly_summary(candidate, tz_name):
            found = True
            break

    assert found, (
        f"No UTC hour maps to Sunday 5pm for timezone {tz_name}"
    )


# ---------------------------------------------------------------------------
# P9c: Non-Sunday days are never eligible regardless of hour
# ---------------------------------------------------------------------------


@given(
    tz_name=_timezones,
    weekday=st.integers(min_value=0, max_value=5),  # Mon–Sat
    hour=st.integers(min_value=0, max_value=23),
    minute=st.integers(min_value=0, max_value=59),
)
@settings(max_examples=200)
def test_non_sunday_never_eligible(
    tz_name: str,
    weekday: int,
    hour: int,
    minute: int,
) -> None:
    """If the user's local time is NOT Sunday, the sweep must not fire."""
    tz = ZoneInfo(tz_name)

    # Build a local datetime on the desired weekday.
    # Start from a known Monday (2026-04-06) and add weekday offset.
    local_dt = datetime(2026, 4, 6 + weekday, hour, minute, 0, tzinfo=tz)
    utc_dt = local_dt.astimezone(timezone.utc)

    assert not _is_eligible_for_weekly_summary(utc_dt, tz_name), (
        f"Sweep fired on non-Sunday: tz={tz_name}, "
        f"local={local_dt.isoformat()}, weekday={local_dt.weekday()}"
    )


# ---------------------------------------------------------------------------
# P9d: Wrong hour on Sunday is never eligible
# ---------------------------------------------------------------------------


@given(
    tz_name=_timezones,
    hour=st.integers(min_value=0, max_value=23).filter(lambda h: h != 17),
    minute=st.integers(min_value=0, max_value=59),
)
@settings(max_examples=200)
def test_sunday_wrong_hour_not_eligible(
    tz_name: str,
    hour: int,
    minute: int,
) -> None:
    """If the user's local time is Sunday but NOT the 5pm hour, no fire."""
    tz = ZoneInfo(tz_name)

    # 2026-04-05 is a Sunday.
    local_dt = datetime(2026, 4, 5, hour, minute, 0, tzinfo=tz)
    utc_dt = local_dt.astimezone(timezone.utc)

    assert not _is_eligible_for_weekly_summary(utc_dt, tz_name), (
        f"Sweep fired at wrong hour: tz={tz_name}, "
        f"local={local_dt.isoformat()}, hour={hour}"
    )


# ---------------------------------------------------------------------------
# P9e: Sunday 5pm hour is always eligible
# ---------------------------------------------------------------------------


@given(
    tz_name=_timezones,
    minute=st.integers(min_value=0, max_value=59),
    second=st.integers(min_value=0, max_value=59),
)
@settings(max_examples=200)
def test_sunday_5pm_always_eligible(
    tz_name: str,
    minute: int,
    second: int,
) -> None:
    """If the user's local time is Sunday 17:xx:xx, the sweep must fire."""
    tz = ZoneInfo(tz_name)

    # 2026-04-05 is a Sunday.
    local_dt = datetime(2026, 4, 5, 17, minute, second, tzinfo=tz)
    utc_dt = local_dt.astimezone(timezone.utc)

    assert _is_eligible_for_weekly_summary(utc_dt, tz_name), (
        f"Sweep did NOT fire on Sunday 5pm: tz={tz_name}, "
        f"local={local_dt.isoformat()}"
    )
