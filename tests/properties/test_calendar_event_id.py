"""Property tests for calendar event ID generation (P29).

Property 29 — Calendar event ID is deterministic and idempotent:
  For any user_id + task_title + date combination,
  ``generate_calendar_event_id`` should always produce the same
  base32hex-compatible event ID.  Attempting to create a calendar event
  with an existing ID should return the existing event rather than
  creating a duplicate.

These are pure-function tests — no database or Google API required.

Validates: Requirements 17.2
"""

from __future__ import annotations

import re

from hypothesis import given, settings, strategies as st, HealthCheck

from app.services.google_calendar_write_service import generate_calendar_event_id

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_user_ids = st.integers(min_value=1, max_value=10_000_000)
_task_titles = st.text(min_size=1, max_size=200)
_date_strs = st.dates().map(lambda d: d.isoformat())  # "YYYY-MM-DD"

# Google Calendar event IDs: lowercase a-v and digits 0-9, length 5-1024.
_BASE32HEX_RE = re.compile(r"^[a-v0-9]+$")


# ---------------------------------------------------------------------------
# P29a: Deterministic — same inputs always produce the same ID
# ---------------------------------------------------------------------------


@given(user_id=_user_ids, title=_task_titles, date_str=_date_strs)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_deterministic_event_id(user_id: int, title: str, date_str: str):
    """Calling generate_calendar_event_id twice with identical inputs
    must return the exact same string."""
    id_a = generate_calendar_event_id(user_id, title, date_str)
    id_b = generate_calendar_event_id(user_id, title, date_str)
    assert id_a == id_b


# ---------------------------------------------------------------------------
# P29b: Output is valid base32hex (a-v, 0-9) and within length bounds
# ---------------------------------------------------------------------------


@given(user_id=_user_ids, title=_task_titles, date_str=_date_strs)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_valid_base32hex_format(user_id: int, title: str, date_str: str):
    """The event ID must contain only base32hex characters (a-v, 0-9)
    and be between 5 and 1024 characters long — Google Calendar's rules."""
    event_id = generate_calendar_event_id(user_id, title, date_str)
    assert 5 <= len(event_id) <= 1024, f"Length {len(event_id)} out of range"
    assert _BASE32HEX_RE.match(event_id), f"Invalid chars in: {event_id}"


# ---------------------------------------------------------------------------
# P29c: Different inputs produce different IDs (collision resistance)
# ---------------------------------------------------------------------------


@given(
    user_id=_user_ids,
    title_a=_task_titles,
    title_b=_task_titles,
    date_str=_date_strs,
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_different_titles_produce_different_ids(
    user_id: int, title_a: str, title_b: str, date_str: str
):
    """Two distinct task titles for the same user and date should yield
    different event IDs (SHA-256 collision is astronomically unlikely)."""
    if title_a == title_b:
        return  # skip trivially equal inputs
    id_a = generate_calendar_event_id(user_id, title_a, date_str)
    id_b = generate_calendar_event_id(user_id, title_b, date_str)
    assert id_a != id_b


@given(
    user_id_a=_user_ids,
    user_id_b=_user_ids,
    title=_task_titles,
    date_str=_date_strs,
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_different_users_produce_different_ids(
    user_id_a: int, user_id_b: int, title: str, date_str: str
):
    """Two distinct users with the same task and date should yield
    different event IDs."""
    if user_id_a == user_id_b:
        return
    id_a = generate_calendar_event_id(user_id_a, title, date_str)
    id_b = generate_calendar_event_id(user_id_b, title, date_str)
    assert id_a != id_b


@given(
    user_id=_user_ids,
    title=_task_titles,
    date_a=st.dates(),
    date_b=st.dates(),
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_different_dates_produce_different_ids(
    user_id: int, title: str, date_a, date_b
):
    """The same task on different dates should yield different event IDs."""
    if date_a == date_b:
        return
    id_a = generate_calendar_event_id(user_id, title, date_a.isoformat())
    id_b = generate_calendar_event_id(user_id, title, date_b.isoformat())
    assert id_a != id_b


# ---------------------------------------------------------------------------
# P29d: Fixed-length output (32 chars from SHA-256 → base32hex truncation)
# ---------------------------------------------------------------------------


@given(user_id=_user_ids, title=_task_titles, date_str=_date_strs)
@settings(max_examples=100)
def test_fixed_length_output(user_id: int, title: str, date_str: str):
    """The implementation truncates to 32 chars — verify consistency."""
    event_id = generate_calendar_event_id(user_id, title, date_str)
    assert len(event_id) == 32
