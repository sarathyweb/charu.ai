"""Pure helpers for post-call recap template selection.

These functions decide which WhatsApp template to use based on the
structured call outcome.  They are intentionally free of side-effects
so they can be property-tested without a database or Celery.

Design Property 8: Recap template selection matches outcome confidence.
"""

from __future__ import annotations

from app.models.enums import CallType, OutcomeConfidence

# ---------------------------------------------------------------------------
# Template name constants
# ---------------------------------------------------------------------------

DAILY_RECAP = "daily_recap"
DAILY_RECAP_NO_GOAL = "daily_recap_no_goal"
EVENING_RECAP = "evening_recap"
EVENING_RECAP_NO_ACCOMPLISHMENTS = "evening_recap_no_accomplishments"


def select_morning_recap_template(
    call_outcome_confidence: str | None,
) -> str:
    """Return the template name for a morning/afternoon post-call recap.

    Rules (from design Property 8 / Requirements 5.1, 5.3, 5.5):
    - confidence "clear" or "partial" → ``daily_recap``
    - confidence "none" or ``None``   → ``daily_recap_no_goal``
    """
    if call_outcome_confidence in (
        OutcomeConfidence.CLEAR.value,
        OutcomeConfidence.PARTIAL.value,
    ):
        return DAILY_RECAP
    return DAILY_RECAP_NO_GOAL


def select_evening_recap_template(
    reflection_confidence: str | None,
) -> str:
    """Return the template name for an evening post-call recap.

    Rules (from design Property 8 / Requirement 20.7):
    - confidence "clear" or "partial" → ``evening_recap``
    - confidence "none" or ``None``   → ``evening_recap_no_accomplishments``
    """
    if reflection_confidence in (
        OutcomeConfidence.CLEAR.value,
        OutcomeConfidence.PARTIAL.value,
    ):
        return EVENING_RECAP
    return EVENING_RECAP_NO_ACCOMPLISHMENTS


def select_recap_template(
    call_type: str,
    call_outcome_confidence: str | None = None,
    reflection_confidence: str | None = None,
) -> str:
    """Unified dispatcher — pick the right template for any call type.

    For evening calls, uses ``reflection_confidence``.
    For all other call types, uses ``call_outcome_confidence``.
    """
    if call_type == CallType.EVENING.value:
        return select_evening_recap_template(reflection_confidence)
    return select_morning_recap_template(call_outcome_confidence)
