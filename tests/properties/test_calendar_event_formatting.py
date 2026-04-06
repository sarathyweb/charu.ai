"""Property tests for calendar event formatting (P18).

Property 18 — Calendar event formatting includes all events:
  For any list of calendar events, the formatted agent context string
  should contain the summary and time of every non-cancelled event in
  the input list.

These are pure-function tests — no database or Google API required.

Validates: Requirements 10.2
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from hypothesis import given, settings, strategies as st, HealthCheck, assume

from app.services.google_calendar_read_service import format_events_for_agent

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Sample of real IANA timezones to keep tests fast and meaningful.
_TIMEZONES = [
    "America/New_York",
    "America/Los_Angeles",
    "America/Chicago",
    "Europe/London",
    "Europe/Berlin",
    "Asia/Kolkata",
    "Asia/Tokyo",
    "Australia/Sydney",
    "Pacific/Auckland",
    "UTC",
]
_tz_strategy = st.sampled_from(_TIMEZONES)

# Event summaries — non-empty printable text.
_summary_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")),
    min_size=1,
    max_size=80,
).filter(lambda s: s.strip())

# Hours 0-23, minutes 0-59 for timed events.
_hour = st.integers(min_value=0, max_value=23)
_minute = st.integers(min_value=0, max_value=59)


def _make_timed_event(
    summary: str,
    tz_name: str,
    hour: int,
    minute: int,
    duration_minutes: int = 60,
) -> dict:
    """Build a Google Calendar API-style timed event dict."""
    tz = ZoneInfo(tz_name)
    start_dt = datetime(2026, 4, 6, hour, minute, tzinfo=tz)
    end_dt = start_dt + timedelta(minutes=duration_minutes)
    return {
        "summary": summary,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": tz_name},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": tz_name},
        "status": "confirmed",
    }


def _make_allday_event(summary: str) -> dict:
    """Build a Google Calendar API-style all-day event dict."""
    return {
        "summary": summary,
        "start": {"date": "2026-04-06"},
        "end": {"date": "2026-04-07"},
        "status": "confirmed",
    }


# Composite strategy: a single timed event.
_timed_event = st.builds(
    _make_timed_event,
    summary=_summary_strategy,
    tz_name=_tz_strategy,
    hour=_hour,
    minute=_minute,
    duration_minutes=st.integers(min_value=15, max_value=480),
)

# Composite strategy: a single all-day event.
_allday_event = st.builds(_make_allday_event, summary=_summary_strategy)

# A mixed list of events (1-10 items).
_event_list = st.lists(
    st.one_of(_timed_event, _allday_event),
    min_size=1,
    max_size=10,
)


# ---------------------------------------------------------------------------
# P18a: Every event summary appears in the formatted output
# ---------------------------------------------------------------------------


@given(events=_event_list, tz=_tz_strategy)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_all_event_summaries_present(events: list[dict], tz: str):
    """The formatted string must contain the summary of every input event."""
    result = format_events_for_agent(events, tz)
    for event in events:
        summary = event.get("summary", "Untitled event")
        assert summary in result, (
            f"Summary '{summary}' missing from formatted output:\n{result}"
        )


# ---------------------------------------------------------------------------
# P18b: Timed events include a time string in the output
# ---------------------------------------------------------------------------


@given(events=st.lists(_timed_event, min_size=1, max_size=5), tz=_tz_strategy)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_timed_events_include_time(events: list[dict], tz: str):
    """Each timed event line must contain AM or PM (12-hour format)."""
    result = format_events_for_agent(events, tz)
    lines = [l for l in result.splitlines() if l.startswith("- ")]
    # There should be one line per event.
    assert len(lines) == len(events), (
        f"Expected {len(events)} event lines, got {len(lines)}:\n{result}"
    )
    for line in lines:
        assert "AM" in line or "PM" in line, (
            f"Timed event line missing AM/PM indicator: {line}"
        )


# ---------------------------------------------------------------------------
# P18c: All-day events are labelled "All day"
# ---------------------------------------------------------------------------


@given(events=st.lists(_allday_event, min_size=1, max_size=5), tz=_tz_strategy)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_allday_events_labelled(events: list[dict], tz: str):
    """Each all-day event line must contain 'All day'."""
    result = format_events_for_agent(events, tz)
    lines = [l for l in result.splitlines() if l.startswith("- ")]
    assert len(lines) == len(events)
    for line in lines:
        assert "All day" in line, f"All-day event line missing label: {line}"


# ---------------------------------------------------------------------------
# P18d: Empty event list returns the "no events" message
# ---------------------------------------------------------------------------


@given(tz=_tz_strategy)
@settings(max_examples=20)
def test_empty_events_returns_no_events_message(tz: str):
    """An empty event list should produce the 'No events' message."""
    result = format_events_for_agent([], tz)
    assert result == "No events scheduled for today."


# ---------------------------------------------------------------------------
# P18e: Output line count matches event count
# ---------------------------------------------------------------------------


@given(events=_event_list, tz=_tz_strategy)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_output_line_count_matches_event_count(events: list[dict], tz: str):
    """The number of bullet-point lines should equal the number of events."""
    result = format_events_for_agent(events, tz)
    bullet_lines = [l for l in result.splitlines() if l.startswith("- ")]
    assert len(bullet_lines) == len(events), (
        f"Expected {len(events)} lines, got {len(bullet_lines)}:\n{result}"
    )


# ---------------------------------------------------------------------------
# P18f: Header line is present when events exist
# ---------------------------------------------------------------------------


@given(events=_event_list, tz=_tz_strategy)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_header_present_when_events_exist(events: list[dict], tz: str):
    """When events are present, the output should start with the header."""
    result = format_events_for_agent(events, tz)
    assert result.startswith("Today's calendar:")


# ---------------------------------------------------------------------------
# P18g: Mixed timed and all-day events are all included
# ---------------------------------------------------------------------------


@given(
    timed=st.lists(_timed_event, min_size=1, max_size=3),
    allday=st.lists(_allday_event, min_size=1, max_size=3),
    tz=_tz_strategy,
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_mixed_events_all_included(
    timed: list[dict], allday: list[dict], tz: str
):
    """A mix of timed and all-day events should all appear in the output."""
    combined = timed + allday
    result = format_events_for_agent(combined, tz)
    for event in combined:
        summary = event.get("summary", "Untitled event")
        assert summary in result
