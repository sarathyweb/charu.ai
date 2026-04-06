"""Property tests for call log state machine (P31).

P31 — Call log status transitions follow state machine: for any CallLog
      status update, the transition should only be applied if it is in the
      set of valid transitions for the current status.  Terminal states
      (completed, missed, cancelled, skipped, deferred) reject all outgoing
      transitions.

These are pure-function tests — no database required.

Validates: Requirements 22.2
"""

from hypothesis import given, settings, strategies as st

from app.models.enums import CallLogStatus
from app.services.call_log_service import (
    TERMINAL_STATUSES,
    VALID_TRANSITIONS,
    validate_transition,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_all_statuses = st.sampled_from(list(CallLogStatus))
_terminal_statuses = st.sampled_from(list(TERMINAL_STATUSES))
_non_terminal_statuses = st.sampled_from(
    [s for s in CallLogStatus if s not in TERMINAL_STATUSES]
)


# ---------------------------------------------------------------------------
# P31a: Valid transitions are accepted
# ---------------------------------------------------------------------------


@given(current=_all_statuses)
@settings(max_examples=100)
def test_valid_transitions_accepted(current: CallLogStatus):
    """For every status, each declared valid target is accepted by
    ``validate_transition``."""
    for target in VALID_TRANSITIONS[current]:
        assert validate_transition(current, target), (
            f"Expected {current.value} → {target.value} to be valid"
        )


# ---------------------------------------------------------------------------
# P31b: Terminal states reject ALL outgoing transitions
# ---------------------------------------------------------------------------


@given(terminal=_terminal_statuses, target=_all_statuses)
@settings(max_examples=100)
def test_terminal_states_reject_all(
    terminal: CallLogStatus,
    target: CallLogStatus,
):
    """No status can be reached from a terminal state."""
    assert not validate_transition(terminal, target), (
        f"Terminal state {terminal.value} should reject → {target.value}"
    )


# ---------------------------------------------------------------------------
# P31c: Invalid transitions are rejected
# ---------------------------------------------------------------------------


@given(current=_all_statuses, target=_all_statuses)
@settings(max_examples=200)
def test_unlisted_transitions_rejected(
    current: CallLogStatus,
    target: CallLogStatus,
):
    """A transition is accepted iff the target is in VALID_TRANSITIONS[current]."""
    expected = target in VALID_TRANSITIONS[current]
    actual = validate_transition(current, target)
    assert actual == expected, (
        f"validate_transition({current.value}, {target.value}) "
        f"returned {actual}, expected {expected}"
    )


# ---------------------------------------------------------------------------
# P31d: validate_transition accepts string values
# ---------------------------------------------------------------------------


@given(current=_all_statuses, target=_all_statuses)
@settings(max_examples=100)
def test_string_values_accepted(
    current: CallLogStatus,
    target: CallLogStatus,
):
    """``validate_transition`` works with raw string values, not just enums."""
    expected = target in VALID_TRANSITIONS[current]
    actual = validate_transition(current.value, target.value)
    assert actual == expected


# ---------------------------------------------------------------------------
# P31e: VALID_TRANSITIONS covers every CallLogStatus as a key
# ---------------------------------------------------------------------------


def test_all_statuses_have_transition_entry():
    """Every CallLogStatus member appears as a key in VALID_TRANSITIONS."""
    for status in CallLogStatus:
        assert status in VALID_TRANSITIONS, (
            f"{status.value} missing from VALID_TRANSITIONS"
        )


# ---------------------------------------------------------------------------
# P31f: dispatching transitions are present (design-specific)
# ---------------------------------------------------------------------------


def test_dispatching_transitions():
    """The dispatching state supports → ringing, → scheduled (rollback),
    and → missed (terminal Twilio error) per the design doc."""
    dispatching = CallLogStatus.DISPATCHING
    assert CallLogStatus.RINGING in VALID_TRANSITIONS[dispatching]
    assert CallLogStatus.SCHEDULED in VALID_TRANSITIONS[dispatching]
    assert CallLogStatus.MISSED in VALID_TRANSITIONS[dispatching]
