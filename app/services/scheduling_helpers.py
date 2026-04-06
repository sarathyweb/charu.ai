"""DST-safe scheduling helpers.

Pure functions for resolving local times to UTC across DST transitions,
call timing jitter, retry budget computation, first-call date selection,
and midday check-in timing.

No DB access — used by the daily planner, call window service, and
any code that converts a user's local time to a UTC scheduled_time.

Design references:
  - Property 5: First call scheduling respects lead time
  - Property 10: Retry timing fits within call window
  - Property 22: Call timing jitter stays within valid range
  - Property 24: Midday check-in timing respects 6pm cutoff
  - Property 39: DST-safe scheduling
  - Requirement 2.V4, 2.V5, 6.R1, 6.R2, 6.R3, 12.2, 13.1
"""

from __future__ import annotations

import enum
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Call scheduling constants (from design doc §3 — Call Scheduling)
# ---------------------------------------------------------------------------

#: How long Twilio lets the phone ring before giving up (seconds).
RING_TIMEOUT_SECONDS: int = 30

#: Delay between a missed call and the next retry attempt (seconds).
RETRY_DELAY_SECONDS: int = 600  # 10 minutes

#: Maximum number of retries after the initial call attempt.
MAX_RETRIES: int = 2  # 2 retries → 3 total attempts

#: Maximum call duration for morning/afternoon calls (seconds).
MAX_CALL_DURATION_MORNING_SECONDS: int = 300  # 5 minutes

#: Maximum call duration for evening calls (seconds).
MAX_CALL_DURATION_EVENING_SECONDS: int = 180  # 3 minutes

#: Minimum lead time (seconds) between "now" and the window's latest
#: first-call time for same-day scheduling.
FIRST_CALL_LEAD_SECONDS: int = 1800  # 30 minutes

#: Hour (in user's local timezone) at or after which midday check-ins
#: are suppressed.
MIDDAY_CHECKIN_CUTOFF_HOUR: int = 18  # 6 PM local


class DSTResolution(str, enum.Enum):
    """Describes how a local time was resolved during DST conversion."""

    NORMAL = "normal"
    """The local time existed exactly once — no DST issue."""

    NONEXISTENT_SHIFTED = "nonexistent_shifted"
    """The local time fell in a spring-forward gap and was shifted
    forward to the first valid minute after the gap."""

    AMBIGUOUS_FIRST = "ambiguous_first"
    """The local time fell in a fall-back overlap and the first
    occurrence (fold=0, pre-transition offset) was used."""


@dataclass(frozen=True, slots=True)
class ResolvedTime:
    """Result of resolving a local time to UTC."""

    utc_dt: datetime
    """The resolved UTC datetime (always timezone-aware, tzinfo=UTC)."""

    local_dt: datetime
    """The resolved local datetime (timezone-aware, with the IANA tz)."""

    resolution: DSTResolution
    """How the time was resolved."""


def _is_imaginary(dt: datetime) -> bool:
    """Return True if *dt* falls in a spring-forward gap.

    A datetime is imaginary when a UTC round-trip changes its wall time.
    """
    rt = dt.astimezone(timezone.utc).astimezone(dt.tzinfo)
    return (dt.hour, dt.minute, dt.second) != (rt.hour, rt.minute, rt.second)


def _find_transition_utc(candidate: datetime, tz: ZoneInfo) -> datetime:
    """Find the UTC instant of the DST transition that makes *candidate*
    nonexistent, using a minute-resolution binary search.

    *candidate* must be an aware datetime inside a spring-forward gap.

    Returns the transition instant as a UTC-aware datetime.  Converting
    this to the target timezone gives the first valid local time after
    the gap (the time the clocks jump TO).

    The search space is at most ~120 minutes (the largest real-world DST
    gap is 1 hour; we use 2 hours as a safety margin).  A binary search
    over 120 minutes converges in ~7 iterations — negligible cost.
    """
    # Walk back from the candidate to find a time that is NOT imaginary.
    # The gap is at most 2 hours in any real-world timezone.
    lo = candidate - timedelta(hours=2)
    hi = candidate

    # Ensure lo is actually before the gap.
    while _is_imaginary(lo):
        lo -= timedelta(hours=2)

    # Binary search to second resolution.
    while (hi - lo) > timedelta(seconds=1):
        mid = lo + (hi - lo) / 2
        if _is_imaginary(mid):
            hi = mid
        else:
            lo = mid

    # *lo* is the last valid second before the gap.  The transition
    # happens between lo and hi.  The first valid time AFTER the gap
    # is the transition instant in the post-transition offset.
    lo_utc = lo.astimezone(timezone.utc)
    # Round up to the next whole second to land exactly on the
    # transition boundary.
    transition_utc = lo_utc.replace(microsecond=0) + timedelta(seconds=1)
    return transition_utc


def _is_ambiguous(dt: datetime) -> bool:
    """Return True if *dt* falls in a fall-back overlap."""
    return dt.utcoffset() != dt.replace(fold=1).utcoffset()


def resolve_local_time(
    target_date: date,
    local_time: time,
    tz_name: str,
) -> ResolvedTime:
    """Convert a local date + time + IANA timezone to UTC, handling DST.

    This is the single entry point for all local-to-UTC conversions in
    the scheduling layer.  It explicitly handles:

    - **Nonexistent times** (spring-forward gap): shifted to the first
      valid minute after the gap.
    - **Ambiguous times** (fall-back overlap): resolved to the first
      occurrence (``fold=0``).
    - **Normal times**: passed through unchanged.

    Args:
        target_date: The calendar date in the user's local timezone.
        local_time: The desired wall-clock time (naive).
        tz_name: IANA timezone identifier (e.g. ``"America/New_York"``).

    Returns:
        A ``ResolvedTime`` with the UTC datetime, the actual local
        datetime used, and a ``DSTResolution`` tag.
    """
    tz = ZoneInfo(tz_name)

    # Build candidate local datetime with fold=0 (first occurrence).
    candidate = datetime.combine(target_date, local_time, tzinfo=tz)

    # --- Detect nonexistent (spring-forward) FIRST ---
    # Must check before ambiguity — nonexistent times also show
    # different offsets for fold=0 vs fold=1.
    if _is_imaginary(candidate):
        transition_utc = _find_transition_utc(candidate, tz)
        first_valid_local = transition_utc.astimezone(tz)

        return ResolvedTime(
            utc_dt=transition_utc,
            local_dt=first_valid_local,
            resolution=DSTResolution.NONEXISTENT_SHIFTED,
        )

    # --- Detect ambiguous (fall-back) ---
    # fold=0 (first occurrence) per the design.
    if _is_ambiguous(candidate):
        utc_dt = candidate.astimezone(timezone.utc)
        return ResolvedTime(
            utc_dt=utc_dt,
            local_dt=candidate,
            resolution=DSTResolution.AMBIGUOUS_FIRST,
        )

    # --- Normal case ---
    utc_dt = candidate.astimezone(timezone.utc)
    return ResolvedTime(
        utc_dt=utc_dt,
        local_dt=candidate,
        resolution=DSTResolution.NORMAL,
    )


# ---------------------------------------------------------------------------
# Call timing helpers
# ---------------------------------------------------------------------------


def _max_call_duration_seconds(call_type: str) -> int:
    """Return the max call duration in seconds for the given call type."""
    if call_type == "evening":
        return MAX_CALL_DURATION_EVENING_SECONDS
    return MAX_CALL_DURATION_MORNING_SECONDS


def _time_to_minutes(t: time) -> int:
    """Convert a ``datetime.time`` to minutes since midnight."""
    return t.hour * 60 + t.minute


def compute_latest_first_call(
    window_end: time,
    call_type: str,
    *,
    max_retries: int = MAX_RETRIES,
    ring_timeout: int = RING_TIMEOUT_SECONDS,
    retry_delay: int = RETRY_DELAY_SECONDS,
) -> time:
    """Compute the latest possible first-call time within a window.

    The formula (from design doc §3 — Retry Timing Constraints):

        latest ≤ window_end
                  - (max_retries × (ring_timeout + retry_delay))
                  - max_call_duration

    All arithmetic is in *minutes* (integer) for simplicity; sub-minute
    precision is not needed for call scheduling.

    Returns a ``datetime.time`` clamped to ``00:00`` if the buffer
    exceeds the window end.
    """
    max_duration = _max_call_duration_seconds(call_type)
    total_retry_buffer = max_retries * (ring_timeout + retry_delay)
    buffer_minutes = (total_retry_buffer + max_duration + 59) // 60  # ceil

    end_minutes = _time_to_minutes(window_end)
    latest_minutes = end_minutes - buffer_minutes

    if latest_minutes < 0:
        latest_minutes = 0

    return time(latest_minutes // 60, latest_minutes % 60)


def compute_jittered_call_time(
    window_start: time,
    window_end: time,
    call_type: str,
    *,
    max_retries: int = MAX_RETRIES,
    ring_timeout: int = RING_TIMEOUT_SECONDS,
    retry_delay: int = RETRY_DELAY_SECONDS,
    _rng: random.Random | None = None,
) -> time:
    """Pick a random call time within the window, respecting retry budget.

    The returned time is guaranteed to be:
      - ≥ ``window_start``
      - ≤ ``compute_latest_first_call(window_end, call_type, ...)``

    If the window is too narrow for any jitter (latest ≤ start), the
    ``window_start`` is returned as-is.

    An optional ``_rng`` parameter accepts a seeded ``random.Random``
    instance for deterministic testing.
    """
    rng = _rng or random.Random()

    latest = compute_latest_first_call(
        window_end,
        call_type,
        max_retries=max_retries,
        ring_timeout=ring_timeout,
        retry_delay=retry_delay,
    )

    start_min = _time_to_minutes(window_start)
    latest_min = _time_to_minutes(latest)

    if latest_min <= start_min:
        return window_start

    chosen = rng.randint(start_min, latest_min)
    return time(chosen // 60, chosen % 60)


def compute_first_call_date(
    now_utc: datetime,
    window_start: time,
    window_end: time,
    call_type: str,
    tz_name: str,
    *,
    lead_seconds: int = FIRST_CALL_LEAD_SECONDS,
    max_retries: int = MAX_RETRIES,
    ring_timeout: int = RING_TIMEOUT_SECONDS,
    retry_delay: int = RETRY_DELAY_SECONDS,
) -> date:
    """Determine the date for the first call after onboarding.

    Returns *today* (in the user's local timezone) if there is still
    enough time to place the call — specifically, if ``now_local`` plus
    ``lead_seconds`` is before the window's latest first-call time.
    Otherwise returns *tomorrow*.

    Property 5 formalises this: for any onboarding completion time and
    any call window, if the remaining time until the latest valid
    first-call time is less than ``lead_seconds``, the function returns
    tomorrow.
    """
    tz = ZoneInfo(tz_name)
    now_local = now_utc.astimezone(tz)

    latest = compute_latest_first_call(
        window_end,
        call_type,
        max_retries=max_retries,
        ring_timeout=ring_timeout,
        retry_delay=retry_delay,
    )

    # Build the latest first-call time as a local datetime for today.
    latest_local_dt = datetime.combine(now_local.date(), latest, tzinfo=tz)

    # The user must have at least ``lead_seconds`` of margin.
    deadline = now_local + timedelta(seconds=lead_seconds)

    if deadline <= latest_local_dt:
        return now_local.date()

    return now_local.date() + timedelta(days=1)


def compute_midday_checkin_time(
    call_end_utc: datetime,
    user_timezone: str,
    *,
    _rng: random.Random | None = None,
) -> datetime | None:
    """Compute the midday check-in send time in UTC.

    Returns a UTC datetime 4–5 hours after ``call_end_utc`` (randomised
    for anti-habituation), or ``None`` if the result would be at or
    after 6 PM in the user's local timezone.

    This is a pure function with no DB access — used by the Celery task
    in 6.9 and tested directly in 3.11.

    An optional ``_rng`` parameter accepts a seeded ``random.Random``
    instance for deterministic testing.
    """
    rng = _rng or random.Random()

    delay_hours = rng.uniform(4.0, 5.0)
    checkin_utc = call_end_utc + timedelta(hours=delay_hours)

    tz = ZoneInfo(user_timezone)
    checkin_local = checkin_utc.astimezone(tz)

    if checkin_local.hour >= MIDDAY_CHECKIN_CUTOFF_HOUR:
        return None

    return checkin_utc
