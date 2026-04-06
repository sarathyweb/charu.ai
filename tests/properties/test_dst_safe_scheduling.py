"""Property tests for DST-safe scheduling (P39).

P39 — DST-safe scheduling: For any call window and date, the scheduled
      UTC time correctly reflects the user's local time even across DST
      transitions.  For nonexistent local times (spring-forward gap), the
      scheduled time is shifted to the first valid minute after the gap.
      For ambiguous local times (fall-back), the first occurrence
      (fold=0) is used.

These are pure-function tests — no database required.

Validates: Requirements 2.V5
"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from hypothesis import assume, given, settings, strategies as st

from app.services.scheduling_helpers import (
    DSTResolution,
    ResolvedTime,
    resolve_local_time,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# A representative set of IANA timezones that observe DST transitions.
_dst_timezones = st.sampled_from(
    [
        "America/New_York",
        "America/Chicago",
        "America/Denver",
        "America/Los_Angeles",
        "Europe/London",
        "Europe/Berlin",
        "Europe/Paris",
        "Australia/Sydney",
        "Australia/Lord_Howe",  # 30-min DST shift — unusual
        "Pacific/Auckland",
        "America/Sao_Paulo",
        "America/Santiago",
        "Asia/Amman",
        "Africa/Cairo",
    ]
)

# Timezones that do NOT observe DST — always normal resolution.
_no_dst_timezones = st.sampled_from(
    [
        "UTC",
        "Asia/Kolkata",
        "Asia/Dubai",
        "Asia/Tokyo",
        "America/Phoenix",
    ]
)

_all_test_timezones = st.one_of(_dst_timezones, _no_dst_timezones)

# Dates spanning a wide range to hit various DST transition dates.
_dates = st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 12, 31))

# Arbitrary local times (hour 0-23, minute 0-59).
_times = st.builds(
    time,
    hour=st.integers(min_value=0, max_value=23),
    minute=st.integers(min_value=0, max_value=59),
)


# ---------------------------------------------------------------------------
# P39a: UTC round-trip — converting back to local yields the resolved local
# ---------------------------------------------------------------------------


@given(target_date=_dates, local_time=_times, tz_name=_all_test_timezones)
@settings(max_examples=500)
def test_utc_round_trip(target_date: date, local_time: time, tz_name: str):
    """The UTC result, when converted back to the user's timezone, must
    equal the ``local_dt`` reported by the resolver."""
    result = resolve_local_time(target_date, local_time, tz_name)

    round_tripped = result.utc_dt.astimezone(ZoneInfo(tz_name))
    assert round_tripped == result.local_dt, (
        f"UTC round-trip mismatch: utc_dt={result.utc_dt} → "
        f"local={round_tripped}, expected local_dt={result.local_dt}"
    )


# ---------------------------------------------------------------------------
# P39b: UTC result is always timezone-aware UTC
# ---------------------------------------------------------------------------


@given(target_date=_dates, local_time=_times, tz_name=_all_test_timezones)
@settings(max_examples=300)
def test_utc_result_is_utc(target_date: date, local_time: time, tz_name: str):
    """``utc_dt`` must always be timezone-aware with UTC offset."""
    result = resolve_local_time(target_date, local_time, tz_name)

    assert result.utc_dt.tzinfo is not None, "utc_dt must be timezone-aware"
    assert result.utc_dt.utcoffset() == timedelta(0), (
        f"utc_dt offset should be 0, got {result.utc_dt.utcoffset()}"
    )


# ---------------------------------------------------------------------------
# P39c: local_dt is always timezone-aware with the requested tz
# ---------------------------------------------------------------------------


@given(target_date=_dates, local_time=_times, tz_name=_all_test_timezones)
@settings(max_examples=300)
def test_local_dt_has_correct_tz(target_date: date, local_time: time, tz_name: str):
    """``local_dt`` must carry the requested IANA timezone."""
    result = resolve_local_time(target_date, local_time, tz_name)

    assert result.local_dt.tzinfo is not None, "local_dt must be timezone-aware"
    # The key name of the tzinfo should match the requested tz.
    assert str(result.local_dt.tzinfo) == tz_name, (
        f"local_dt timezone is {result.local_dt.tzinfo}, expected {tz_name}"
    )


# ---------------------------------------------------------------------------
# P39d: Non-DST timezones always produce NORMAL resolution
# ---------------------------------------------------------------------------


@given(target_date=_dates, local_time=_times, tz_name=_no_dst_timezones)
@settings(max_examples=200)
def test_no_dst_always_normal(target_date: date, local_time: time, tz_name: str):
    """Timezones without DST should always resolve as NORMAL."""
    result = resolve_local_time(target_date, local_time, tz_name)
    assert result.resolution == DSTResolution.NORMAL, (
        f"Expected NORMAL for {tz_name}, got {result.resolution}"
    )


# ---------------------------------------------------------------------------
# P39e: Normal resolution preserves the requested time exactly
# ---------------------------------------------------------------------------


@given(target_date=_dates, local_time=_times, tz_name=_all_test_timezones)
@settings(max_examples=500)
def test_normal_preserves_time(target_date: date, local_time: time, tz_name: str):
    """When resolution is NORMAL, the local_dt must have the exact
    requested date and time."""
    result = resolve_local_time(target_date, local_time, tz_name)

    if result.resolution == DSTResolution.NORMAL:
        assert result.local_dt.date() == target_date
        assert result.local_dt.hour == local_time.hour
        assert result.local_dt.minute == local_time.minute


# ---------------------------------------------------------------------------
# P39f: Nonexistent times are shifted forward (spring-forward)
# ---------------------------------------------------------------------------


@given(target_date=_dates, local_time=_times, tz_name=_dst_timezones)
@settings(max_examples=500)
def test_nonexistent_shifted_forward(target_date: date, local_time: time, tz_name: str):
    """When resolution is NONEXISTENT_SHIFTED, the local_dt must be at or
    after the requested time (shifted forward past the gap), and the
    resolved local time must actually exist."""
    result = resolve_local_time(target_date, local_time, tz_name)

    if result.resolution == DSTResolution.NONEXISTENT_SHIFTED:
        # The resolved local time should be >= the requested time
        # (it was shifted forward past the gap).
        requested = datetime.combine(target_date, local_time, tzinfo=ZoneInfo(tz_name))
        # The resolved time should be on the same date or the next
        # (for gaps near midnight, though extremely rare).
        assert result.local_dt >= requested.replace(fold=0), (
            f"Shifted time {result.local_dt} should be >= requested {requested}"
        )

        # The resolved local time must actually exist (not be imaginary).
        # Verify via UTC round-trip.
        rt = result.utc_dt.astimezone(ZoneInfo(tz_name))
        assert (rt.hour, rt.minute) == (result.local_dt.hour, result.local_dt.minute), (
            f"Resolved local time {result.local_dt} is still imaginary"
        )


# ---------------------------------------------------------------------------
# P39g: Ambiguous times use fold=0 (first occurrence)
# ---------------------------------------------------------------------------


@given(target_date=_dates, local_time=_times, tz_name=_dst_timezones)
@settings(max_examples=500)
def test_ambiguous_uses_first_occurrence(
    target_date: date,
    local_time: time,
    tz_name: str,
):
    """When resolution is AMBIGUOUS_FIRST, the UTC offset should match
    fold=0 (the pre-transition / first occurrence)."""
    result = resolve_local_time(target_date, local_time, tz_name)

    if result.resolution == DSTResolution.AMBIGUOUS_FIRST:
        tz = ZoneInfo(tz_name)
        fold0 = datetime.combine(target_date, local_time, tzinfo=tz).replace(fold=0)
        fold1 = datetime.combine(target_date, local_time, tzinfo=tz).replace(fold=1)

        # Confirm the time is actually ambiguous (two different offsets).
        assert fold0.utcoffset() != fold1.utcoffset(), (
            f"Time {local_time} on {target_date} in {tz_name} is not ambiguous"
        )

        # The resolved UTC should match fold=0.
        expected_utc = fold0.astimezone(timezone.utc)
        assert result.utc_dt == expected_utc, (
            f"Expected fold=0 UTC {expected_utc}, got {result.utc_dt}"
        )

        # The local_dt should preserve the requested time exactly.
        assert result.local_dt.hour == local_time.hour
        assert result.local_dt.minute == local_time.minute
        assert result.local_dt.date() == target_date


# ---------------------------------------------------------------------------
# P39h: Known DST transitions — concrete examples
# ---------------------------------------------------------------------------


def test_spring_forward_us_eastern_2024():
    """US Eastern spring-forward: 2024-03-10 at 2:00 AM clocks jump to 3:00 AM.
    Scheduling at 2:30 AM should shift to 3:00 AM."""
    result = resolve_local_time(date(2024, 3, 10), time(2, 30), "America/New_York")
    assert result.resolution == DSTResolution.NONEXISTENT_SHIFTED
    # Should land at 3:00 AM EDT (the first valid minute after the gap).
    assert result.local_dt.hour == 3
    assert result.local_dt.minute == 0


def test_fall_back_us_eastern_2024():
    """US Eastern fall-back: 2024-11-03 at 2:00 AM clocks fall back to 1:00 AM.
    Scheduling at 1:30 AM should use the first occurrence (EDT, fold=0)."""
    result = resolve_local_time(date(2024, 11, 3), time(1, 30), "America/New_York")
    assert result.resolution == DSTResolution.AMBIGUOUS_FIRST
    # fold=0 is EDT (UTC-4), fold=1 is EST (UTC-5).
    assert result.local_dt.utcoffset() == timedelta(hours=-4)


def test_spring_forward_europe_berlin_2025():
    """Europe/Berlin spring-forward: 2025-03-30 at 2:00 AM clocks jump to 3:00 AM.
    Scheduling at 2:15 AM should shift to 3:00 AM."""
    result = resolve_local_time(date(2025, 3, 30), time(2, 15), "Europe/Berlin")
    assert result.resolution == DSTResolution.NONEXISTENT_SHIFTED
    assert result.local_dt.hour == 3
    assert result.local_dt.minute == 0


def test_fall_back_europe_london_2025():
    """Europe/London fall-back: 2025-10-26 at 2:00 AM clocks fall back to 1:00 AM.
    Scheduling at 1:00 AM should use the first occurrence (BST, fold=0)."""
    result = resolve_local_time(date(2025, 10, 26), time(1, 0), "Europe/London")
    assert result.resolution == DSTResolution.AMBIGUOUS_FIRST
    # fold=0 is BST (UTC+1), fold=1 is GMT (UTC+0).
    assert result.local_dt.utcoffset() == timedelta(hours=1)


def test_normal_time_no_dst_issue():
    """A normal time (no DST transition) should resolve as NORMAL with
    the exact requested time preserved."""
    result = resolve_local_time(date(2025, 6, 15), time(7, 30), "America/New_York")
    assert result.resolution == DSTResolution.NORMAL
    assert result.local_dt.hour == 7
    assert result.local_dt.minute == 30
    assert result.local_dt.date() == date(2025, 6, 15)


def test_australia_lord_howe_30min_shift():
    """Lord Howe Island has a 30-minute DST shift (unique).
    Spring-forward: first Sunday in October, 2:00 AM → 2:30 AM.
    Scheduling at 2:15 AM should shift to 2:30 AM."""
    result = resolve_local_time(date(2024, 10, 6), time(2, 15), "Australia/Lord_Howe")
    assert result.resolution == DSTResolution.NONEXISTENT_SHIFTED
    assert result.local_dt.hour == 2
    assert result.local_dt.minute == 30


def test_utc_never_has_dst():
    """UTC has no DST transitions — always NORMAL."""
    result = resolve_local_time(date(2025, 3, 10), time(2, 30), "UTC")
    assert result.resolution == DSTResolution.NORMAL
    assert result.local_dt.hour == 2
    assert result.local_dt.minute == 30
