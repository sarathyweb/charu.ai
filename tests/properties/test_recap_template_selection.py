"""Property tests for recap template selection (P8).

Property 8 — Recap template selection matches outcome confidence:
  For any call outcome, if call_outcome_confidence is "clear" or "partial",
  the daily_recap template should be selected; if "none", the
  daily_recap_no_goal template should be selected.  The same logic applies
  to evening recaps with their respective templates.

These are pure-function tests — no database required.

Validates: Requirements 5.1, 5.3, 5.5, 20.7
"""

from hypothesis import given, settings, strategies as st

from app.models.enums import CallType, OutcomeConfidence
from app.services.recap_helpers import (
    DAILY_RECAP,
    DAILY_RECAP_NO_GOAL,
    EVENING_RECAP,
    EVENING_RECAP_NO_ACCOMPLISHMENTS,
    select_evening_recap_template,
    select_morning_recap_template,
    select_recap_template,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_confidence_with_goal = st.sampled_from([
    OutcomeConfidence.CLEAR.value,
    OutcomeConfidence.PARTIAL.value,
])

_confidence_no_goal = st.just(OutcomeConfidence.NONE.value)

_all_confidence = st.sampled_from([c.value for c in OutcomeConfidence])

_null_confidence = st.just(None)

_morning_afternoon_types = st.sampled_from([
    CallType.MORNING.value,
    CallType.AFTERNOON.value,
    CallType.ON_DEMAND.value,
])


# ---------------------------------------------------------------------------
# P8a: Morning/afternoon — clear or partial → daily_recap
# ---------------------------------------------------------------------------


@given(confidence=_confidence_with_goal)
@settings(max_examples=100)
def test_morning_clear_or_partial_selects_daily_recap(confidence: str):
    """When call_outcome_confidence is 'clear' or 'partial', the
    daily_recap template is selected."""
    assert select_morning_recap_template(confidence) == DAILY_RECAP


# ---------------------------------------------------------------------------
# P8b: Morning/afternoon — none → daily_recap_no_goal
# ---------------------------------------------------------------------------


@given(confidence=_confidence_no_goal)
@settings(max_examples=100)
def test_morning_none_selects_daily_recap_no_goal(confidence: str):
    """When call_outcome_confidence is 'none', the daily_recap_no_goal
    template is selected."""
    assert select_morning_recap_template(confidence) == DAILY_RECAP_NO_GOAL


# ---------------------------------------------------------------------------
# P8c: Morning/afternoon — None (null) → daily_recap_no_goal
# ---------------------------------------------------------------------------


@given(confidence=_null_confidence)
@settings(max_examples=10)
def test_morning_null_selects_daily_recap_no_goal(confidence: str | None):
    """When call_outcome_confidence is None (not set), the
    daily_recap_no_goal template is selected."""
    assert select_morning_recap_template(confidence) == DAILY_RECAP_NO_GOAL


# ---------------------------------------------------------------------------
# P8d: Evening — clear or partial → evening_recap
# ---------------------------------------------------------------------------


@given(confidence=_confidence_with_goal)
@settings(max_examples=100)
def test_evening_clear_or_partial_selects_evening_recap(confidence: str):
    """When reflection_confidence is 'clear' or 'partial', the
    evening_recap template is selected."""
    assert select_evening_recap_template(confidence) == EVENING_RECAP


# ---------------------------------------------------------------------------
# P8e: Evening — none → evening_recap_no_accomplishments
# ---------------------------------------------------------------------------


@given(confidence=_confidence_no_goal)
@settings(max_examples=100)
def test_evening_none_selects_evening_recap_no_accomplishments(confidence: str):
    """When reflection_confidence is 'none', the
    evening_recap_no_accomplishments template is selected."""
    assert select_evening_recap_template(confidence) == EVENING_RECAP_NO_ACCOMPLISHMENTS


# ---------------------------------------------------------------------------
# P8f: Evening — None (null) → evening_recap_no_accomplishments
# ---------------------------------------------------------------------------


@given(confidence=_null_confidence)
@settings(max_examples=10)
def test_evening_null_selects_evening_recap_no_accomplishments(
    confidence: str | None,
):
    """When reflection_confidence is None (not set), the
    evening_recap_no_accomplishments template is selected."""
    assert select_evening_recap_template(confidence) == EVENING_RECAP_NO_ACCOMPLISHMENTS


# ---------------------------------------------------------------------------
# P8g: Unified dispatcher routes morning/afternoon correctly
# ---------------------------------------------------------------------------


@given(call_type=_morning_afternoon_types, confidence=_all_confidence)
@settings(max_examples=100)
def test_unified_dispatcher_morning_afternoon(call_type: str, confidence: str):
    """select_recap_template delegates to select_morning_recap_template
    for non-evening call types."""
    expected = select_morning_recap_template(confidence)
    actual = select_recap_template(
        call_type=call_type,
        call_outcome_confidence=confidence,
    )
    assert actual == expected


# ---------------------------------------------------------------------------
# P8h: Unified dispatcher routes evening correctly
# ---------------------------------------------------------------------------


@given(confidence=_all_confidence)
@settings(max_examples=100)
def test_unified_dispatcher_evening(confidence: str):
    """select_recap_template delegates to select_evening_recap_template
    for evening call type."""
    expected = select_evening_recap_template(confidence)
    actual = select_recap_template(
        call_type=CallType.EVENING.value,
        reflection_confidence=confidence,
    )
    assert actual == expected


# ---------------------------------------------------------------------------
# P8i: Template names are always one of the four known templates
# ---------------------------------------------------------------------------


@given(
    call_type=st.sampled_from([c.value for c in CallType]),
    confidence=st.one_of(_all_confidence, _null_confidence),
)
@settings(max_examples=200)
def test_template_is_always_known(call_type: str, confidence: str | None):
    """The selected template is always one of the four known templates."""
    known = {
        DAILY_RECAP,
        DAILY_RECAP_NO_GOAL,
        EVENING_RECAP,
        EVENING_RECAP_NO_ACCOMPLISHMENTS,
    }
    result = select_recap_template(
        call_type=call_type,
        call_outcome_confidence=confidence,
        reflection_confidence=confidence,
    )
    assert result in known, f"Unknown template: {result}"


# ---------------------------------------------------------------------------
# P8j: Exhaustive — every confidence value maps to exactly one template
# ---------------------------------------------------------------------------


def test_exhaustive_morning_mapping():
    """Every OutcomeConfidence value produces a deterministic template
    for morning/afternoon calls."""
    mapping = {
        OutcomeConfidence.CLEAR.value: DAILY_RECAP,
        OutcomeConfidence.PARTIAL.value: DAILY_RECAP,
        OutcomeConfidence.NONE.value: DAILY_RECAP_NO_GOAL,
        None: DAILY_RECAP_NO_GOAL,
    }
    for confidence, expected in mapping.items():
        assert select_morning_recap_template(confidence) == expected, (
            f"Morning: confidence={confidence!r} → expected {expected}"
        )


def test_exhaustive_evening_mapping():
    """Every OutcomeConfidence value produces a deterministic template
    for evening calls."""
    mapping = {
        OutcomeConfidence.CLEAR.value: EVENING_RECAP,
        OutcomeConfidence.PARTIAL.value: EVENING_RECAP,
        OutcomeConfidence.NONE.value: EVENING_RECAP_NO_ACCOMPLISHMENTS,
        None: EVENING_RECAP_NO_ACCOMPLISHMENTS,
    }
    for confidence, expected in mapping.items():
        assert select_evening_recap_template(confidence) == expected, (
            f"Evening: confidence={confidence!r} → expected {expected}"
        )
