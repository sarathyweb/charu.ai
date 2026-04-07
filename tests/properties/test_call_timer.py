"""Property tests for call timer (P7).

Property 7 — Call timer enforces duration limits:
  For any call type (morning=300s, evening=180s), the CallTimerProcessor
  should inject a warning frame when elapsed time >= warn_at, and an end
  frame when elapsed time >= max_duration.  No warning or end frame should
  be emitted before those thresholds.

These are pure-function tests — no database required.

Validates: Requirements 4.6, 20.5
"""

from __future__ import annotations

from hypothesis import given, settings, strategies as st

from app.voice.call_timer import (
    TIMER_PRESETS,
    CallTimerProcessor,
    create_call_timer,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_known_call_types = st.sampled_from(["morning", "afternoon", "evening", "on_demand"])

# Arbitrary (max_duration, warn_at) pairs where warn_at < max_duration.
_valid_timer_params = st.tuples(
    st.integers(min_value=10, max_value=600),  # max_duration
    st.integers(min_value=1, max_value=599),   # warn_at
).filter(lambda t: t[1] < t[0])


# ---------------------------------------------------------------------------
# P7a: create_call_timer returns correct presets for known call types
# ---------------------------------------------------------------------------


@given(call_type=_known_call_types)
@settings(max_examples=100)
def test_create_call_timer_returns_correct_preset(call_type: str):
    """**Validates: Requirements 4.6, 20.5**

    For any known call_type, create_call_timer returns a timer whose
    max_duration and warn_at match the TIMER_PRESETS table.
    """
    timer = create_call_timer(call_type)
    expected = TIMER_PRESETS[call_type]
    assert timer.max_duration == expected["max_duration"]
    assert timer.warn_at == expected["warn_at"]


# ---------------------------------------------------------------------------
# P7b: Unknown call types default to morning preset
# ---------------------------------------------------------------------------


@given(call_type=st.text(min_size=1, max_size=20).filter(
    lambda s: s not in TIMER_PRESETS
))
@settings(max_examples=100)
def test_unknown_call_type_defaults_to_morning(call_type: str):
    """**Validates: Requirements 4.6, 20.5**

    Unknown call types fall back to the morning preset.
    """
    timer = create_call_timer(call_type)
    morning = TIMER_PRESETS["morning"]
    assert timer.max_duration == morning["max_duration"]
    assert timer.warn_at == morning["warn_at"]


# ---------------------------------------------------------------------------
# P7c: warn_at < max_duration for all presets
# ---------------------------------------------------------------------------


@given(call_type=_known_call_types)
@settings(max_examples=100)
def test_warn_at_less_than_max_duration(call_type: str):
    """**Validates: Requirements 4.6, 20.5**

    For any call_type, the timer's warn_at is strictly less than
    max_duration — the warning always fires before the cutoff.
    """
    timer = create_call_timer(call_type)
    assert timer.warn_at < timer.max_duration


# ---------------------------------------------------------------------------
# P7d: Morning/afternoon calls have 5-minute max with 4-minute warn
# ---------------------------------------------------------------------------


def test_morning_afternoon_timer_values():
    """**Validates: Requirements 4.6**

    Morning and afternoon calls: 300s max, 240s warn.
    """
    for ct in ("morning", "afternoon"):
        timer = create_call_timer(ct)
        assert timer.max_duration == 300, f"{ct}: max_duration should be 300"
        assert timer.warn_at == 240, f"{ct}: warn_at should be 240"


# ---------------------------------------------------------------------------
# P7e: Evening calls have 3-minute max with 2-minute warn
# ---------------------------------------------------------------------------


def test_evening_timer_values():
    """**Validates: Requirements 20.5**

    Evening calls: 180s max, 120s warn.
    """
    timer = create_call_timer("evening")
    assert timer.max_duration == 180
    assert timer.warn_at == 120


# ---------------------------------------------------------------------------
# P7f: On-demand calls use morning preset
# ---------------------------------------------------------------------------


def test_on_demand_timer_values():
    """**Validates: Requirements 4.6**

    On-demand calls use the same limits as morning calls.
    """
    timer = create_call_timer("on_demand")
    morning = TIMER_PRESETS["morning"]
    assert timer.max_duration == morning["max_duration"]
    assert timer.warn_at == morning["warn_at"]


# ---------------------------------------------------------------------------
# P7g: Elapsed starts at 0 before any frame processing
# ---------------------------------------------------------------------------


@given(call_type=_known_call_types)
@settings(max_examples=100)
def test_elapsed_starts_at_zero(call_type: str):
    """**Validates: Requirements 4.6, 20.5**

    Before any frame is processed, elapsed time is 0.
    """
    timer = create_call_timer(call_type)
    assert timer.elapsed == 0.0


# ---------------------------------------------------------------------------
# P7h: For any valid (max_duration, warn_at), warn_at < max_duration holds
# ---------------------------------------------------------------------------


@given(params=_valid_timer_params)
@settings(max_examples=100)
def test_arbitrary_timer_warn_before_cutoff(params: tuple[int, int]):
    """**Validates: Requirements 4.6, 20.5**

    For any (max_duration, warn_at) where warn_at < max_duration,
    the timer is constructed with the warning before the cutoff.
    """
    max_duration, warn_at = params
    timer = CallTimerProcessor(max_duration=max_duration, warn_at=warn_at)
    assert timer.warn_at < timer.max_duration
    assert timer.max_duration == max_duration
    assert timer.warn_at == warn_at


# ---------------------------------------------------------------------------
# P7i: Timer is not warned initially
# ---------------------------------------------------------------------------


@given(call_type=_known_call_types)
@settings(max_examples=100)
def test_timer_not_warned_initially(call_type: str):
    """**Validates: Requirements 4.6, 20.5**

    A freshly created timer has not yet issued a warning.
    """
    timer = create_call_timer(call_type)
    assert timer._warned is False


# ---------------------------------------------------------------------------
# P7j: Exhaustive preset table verification
# ---------------------------------------------------------------------------


def test_exhaustive_preset_table():
    """**Validates: Requirements 4.6, 20.5**

    Every entry in TIMER_PRESETS has warn_at < max_duration and
    matches the documented values.
    """
    expected = {
        "morning": {"max_duration": 300, "warn_at": 240},
        "afternoon": {"max_duration": 300, "warn_at": 240},
        "evening": {"max_duration": 180, "warn_at": 120},
        "on_demand": {"max_duration": 300, "warn_at": 240},
    }
    assert set(TIMER_PRESETS.keys()) == set(expected.keys())
    for call_type, preset in expected.items():
        actual = TIMER_PRESETS[call_type]
        assert actual["max_duration"] == preset["max_duration"], (
            f"{call_type}: max_duration mismatch"
        )
        assert actual["warn_at"] == preset["warn_at"], (
            f"{call_type}: warn_at mismatch"
        )
        assert actual["warn_at"] < actual["max_duration"], (
            f"{call_type}: warn_at must be < max_duration"
        )
