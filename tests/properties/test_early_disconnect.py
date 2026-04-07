"""Property tests for early disconnect detection (P25).

Property 25 — Early disconnect detection:
  For any call session where elapsed time < 10 seconds and no user utterance
  was detected, ``is_early_disconnect`` should return True.  For calls with
  elapsed time >= 10 seconds or with a detected user utterance, it should
  return False.

These are pure-function tests — no database required.

Validates: Requirements 14.4
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given, settings, strategies as st

from app.voice.disconnect import DEFAULT_THRESHOLD_SECONDS, EarlyDisconnectDetector

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Positive floats for elapsed seconds (0 exclusive to avoid exact-zero edge)
_positive_elapsed = st.floats(min_value=0.001, max_value=3600.0, allow_nan=False, allow_infinity=False)

# Threshold values — always positive
_threshold = st.floats(min_value=0.1, max_value=120.0, allow_nan=False, allow_infinity=False)

# A UTC datetime used as a base for connected_at
_base_dt = st.datetimes(
    min_value=datetime(2020, 1, 1),
    max_value=datetime(2030, 1, 1),
    timezones=st.just(timezone.utc),
)

# An optional UTC datetime for first_user_utterance_at
_optional_utterance_dt = st.one_of(st.none(), _base_dt)


def _make_detector(
    threshold: float,
    connected_at: datetime,
    elapsed: float,
) -> EarlyDisconnectDetector:
    """Build a detector with explicit connected/disconnected timestamps."""
    d = EarlyDisconnectDetector(threshold_seconds=threshold)
    d.connected_at = connected_at
    d.disconnected_at = connected_at + timedelta(seconds=elapsed)
    return d


# ---------------------------------------------------------------------------
# P25a: elapsed < threshold AND no utterance → early disconnect (True)
# ---------------------------------------------------------------------------


@given(
    threshold=_threshold,
    base=_base_dt,
    fraction=st.floats(min_value=0.0, max_value=0.999, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=100)
def test_short_call_no_utterance_is_early_disconnect(
    threshold: float,
    base: datetime,
    fraction: float,
):
    """**Validates: Requirements 14.4**

    For any elapsed < threshold and no user utterance,
    is_early_disconnect returns True.
    """
    elapsed = threshold * fraction  # always < threshold
    detector = _make_detector(threshold, base, elapsed)
    assert detector.is_early_disconnect(first_user_utterance_at=None) is True


# ---------------------------------------------------------------------------
# P25b: elapsed >= threshold → NOT early disconnect (regardless of utterance)
# ---------------------------------------------------------------------------


@given(
    threshold=_threshold,
    base=_base_dt,
    extra=st.floats(min_value=0.001, max_value=3600.0, allow_nan=False, allow_infinity=False),
    utterance=_optional_utterance_dt,
)
@settings(max_examples=100)
def test_long_call_is_not_early_disconnect(
    threshold: float,
    base: datetime,
    extra: float,
    utterance: datetime | None,
):
    """**Validates: Requirements 14.4**

    For any elapsed >= threshold, is_early_disconnect returns False
    regardless of whether a user utterance was detected.

    Note: extra starts at 0.001 (not 0.0) because timedelta has
    microsecond precision — sub-microsecond fractions in the threshold
    can be lost, causing elapsed_seconds < threshold when extra == 0.
    """
    elapsed = threshold + extra  # always > threshold
    detector = _make_detector(threshold, base, elapsed)
    assert detector.is_early_disconnect(first_user_utterance_at=utterance) is False


# ---------------------------------------------------------------------------
# P25c: elapsed < threshold BUT with user utterance → NOT early disconnect
# ---------------------------------------------------------------------------


@given(
    threshold=_threshold,
    base=_base_dt,
    fraction=st.floats(min_value=0.0, max_value=0.999, allow_nan=False, allow_infinity=False),
    utterance=_base_dt,
)
@settings(max_examples=100)
def test_short_call_with_utterance_is_not_early_disconnect(
    threshold: float,
    base: datetime,
    fraction: float,
    utterance: datetime,
):
    """**Validates: Requirements 14.4**

    For any elapsed < threshold but with a user utterance detected,
    is_early_disconnect returns False.
    """
    elapsed = threshold * fraction
    detector = _make_detector(threshold, base, elapsed)
    assert detector.is_early_disconnect(first_user_utterance_at=utterance) is False


# ---------------------------------------------------------------------------
# P25d: connected_at never set → early disconnect (pipeline never started)
# ---------------------------------------------------------------------------


@given(utterance=_optional_utterance_dt)
@settings(max_examples=100)
def test_never_connected_is_early_disconnect(utterance: datetime | None):
    """**Validates: Requirements 14.4**

    If connected_at was never set (pipeline never started),
    is_early_disconnect returns True regardless of utterance.
    """
    detector = EarlyDisconnectDetector()
    # connected_at is None by default
    assert detector.connected_at is None
    assert detector.is_early_disconnect(first_user_utterance_at=utterance) is True


# ---------------------------------------------------------------------------
# P25e: elapsed_seconds is always >= 0
# ---------------------------------------------------------------------------


@given(
    base=_base_dt,
    elapsed=st.floats(min_value=-100.0, max_value=3600.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=100)
def test_elapsed_seconds_never_negative(base: datetime, elapsed: float):
    """**Validates: Requirements 14.4**

    elapsed_seconds is always >= 0, even if disconnected_at < connected_at
    (clock skew or test setup).
    """
    detector = EarlyDisconnectDetector()
    detector.connected_at = base
    detector.disconnected_at = base + timedelta(seconds=elapsed)
    assert detector.elapsed_seconds >= 0.0


# ---------------------------------------------------------------------------
# P25f: elapsed_seconds is 0 when timestamps are missing
# ---------------------------------------------------------------------------


def test_elapsed_zero_when_no_timestamps():
    """**Validates: Requirements 14.4**

    elapsed_seconds returns 0.0 when either timestamp is missing.
    """
    d = EarlyDisconnectDetector()
    assert d.elapsed_seconds == 0.0

    d.mark_connected()
    # disconnected_at still None
    assert d.elapsed_seconds == 0.0

    d2 = EarlyDisconnectDetector()
    d2.disconnected_at = datetime.now(timezone.utc)
    # connected_at still None
    assert d2.elapsed_seconds == 0.0


# ---------------------------------------------------------------------------
# P25g: threshold is configurable
# ---------------------------------------------------------------------------


@given(threshold=_threshold)
@settings(max_examples=100)
def test_threshold_is_configurable(threshold: float):
    """**Validates: Requirements 14.4**

    The threshold_seconds parameter is respected — a detector with a
    custom threshold uses that value, not the default.
    """
    detector = EarlyDisconnectDetector(threshold_seconds=threshold)
    assert detector.threshold_seconds == threshold


# ---------------------------------------------------------------------------
# P25h: default threshold is 10 seconds
# ---------------------------------------------------------------------------


def test_default_threshold():
    """**Validates: Requirements 14.4**

    The default threshold is 10.0 seconds.
    """
    detector = EarlyDisconnectDetector()
    assert detector.threshold_seconds == DEFAULT_THRESHOLD_SECONDS
    assert detector.threshold_seconds == 10.0
