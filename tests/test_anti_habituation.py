"""Property tests for the anti-habituation opener pool, approach rotation, and variation.

Feature: accountability-call-onboarding, Property 21: Anti-habituation no consecutive repeats
Feature: accountability-call-onboarding, Property 23: Streak variation triggers at days 10-14

Tests:
  - select_opener never returns the same opener as last_opener_id
  - Context filtering works (calendar openers excluded when has_calendar=False, etc.)
  - Fallback works when all context-dependent openers are filtered out
  - Function works with None as last_opener_id (first call)
  - Both morning and evening pools have at least 10 openers
  - select_approach never returns the same approach consecutively
  - select_approach respects context availability
  - update_streak correctly tracks consecutive active days
  - get_two_week_variation returns variation only for days 10-14

**Validates: Requirements 12.1, 12.3, 12.4**
"""

from __future__ import annotations

from datetime import date, timedelta

from hypothesis import given, settings, assume, strategies as st

from app.services.anti_habituation import (
    APPROACHES,
    Approach,
    EVENING_OPENER_POOL,
    MORNING_OPENER_POOL,
    OpenerCategory,
    TwoWeekVariationType,
    _TWO_WEEK_VARIATIONS,
    get_two_week_variation,
    select_approach,
    select_opener,
    update_streak,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_morning_opener_ids = st.sampled_from([o["id"] for o in MORNING_OPENER_POOL])
_evening_opener_ids = st.sampled_from([o["id"] for o in EVENING_OPENER_POOL])

_context_flags = st.fixed_dictionaries(
    {
        "has_calendar": st.booleans(),
        "has_tasks": st.booleans(),
        "has_yesterday": st.booleans(),
    }
)

_optional_morning_id = st.one_of(st.none(), _morning_opener_ids)
_optional_evening_id = st.one_of(st.none(), _evening_opener_ids)


# ---------------------------------------------------------------------------
# Pool size checks
# ---------------------------------------------------------------------------


def test_morning_pool_has_at_least_10_openers():
    """Morning opener pool must have at least 10 openers."""
    assert len(MORNING_OPENER_POOL) >= 10


def test_evening_pool_has_at_least_10_openers():
    """Evening opener pool must have at least 10 openers."""
    assert len(EVENING_OPENER_POOL) >= 10


def test_morning_pool_ids_are_unique():
    """All morning opener IDs must be unique."""
    ids = [o["id"] for o in MORNING_OPENER_POOL]
    assert len(ids) == len(set(ids))


def test_evening_pool_ids_are_unique():
    """All evening opener IDs must be unique."""
    ids = [o["id"] for o in EVENING_OPENER_POOL]
    assert len(ids) == len(set(ids))


def test_morning_pool_covers_all_categories():
    """Morning pool should have openers across all 7 categories."""
    categories = {o["category"] for o in MORNING_OPENER_POOL}
    # Convert to OpenerCategory values for comparison
    cat_values = set()
    for c in categories:
        cat_values.add(c if isinstance(c, str) else c.value)
    expected = {cat.value for cat in OpenerCategory}
    assert expected.issubset(cat_values), (
        f"Missing categories: {expected - cat_values}"
    )


# ---------------------------------------------------------------------------
# Property: No consecutive repeat (morning pool)
# ---------------------------------------------------------------------------


@given(last_id=_morning_opener_ids, context=_context_flags)
@settings(max_examples=100)
def test_morning_no_consecutive_repeat(last_id: str, context: dict):
    """select_opener never returns the same opener as last_opener_id
    when the pool has more than one opener."""
    result = select_opener(MORNING_OPENER_POOL, last_id, context)
    assert result["id"] != last_id


# ---------------------------------------------------------------------------
# Property: No consecutive repeat (evening pool)
# ---------------------------------------------------------------------------


@given(last_id=_evening_opener_ids, context=_context_flags)
@settings(max_examples=100)
def test_evening_no_consecutive_repeat(last_id: str, context: dict):
    """select_opener never returns the same opener as last_opener_id
    for the evening pool."""
    result = select_opener(EVENING_OPENER_POOL, last_id, context)
    assert result["id"] != last_id


# ---------------------------------------------------------------------------
# Property: Works with None as last_opener_id (first call)
# ---------------------------------------------------------------------------


@given(context=_context_flags)
@settings(max_examples=100)
def test_morning_first_call_none_last_id(context: dict):
    """select_opener works when last_opener_id is None (first call)."""
    result = select_opener(MORNING_OPENER_POOL, None, context)
    assert result["id"] in {o["id"] for o in MORNING_OPENER_POOL}


@given(context=_context_flags)
@settings(max_examples=100)
def test_evening_first_call_none_last_id(context: dict):
    """select_opener works when last_opener_id is None (first call)."""
    result = select_opener(EVENING_OPENER_POOL, None, context)
    assert result["id"] in {o["id"] for o in EVENING_OPENER_POOL}


# ---------------------------------------------------------------------------
# Property: Context filtering — calendar openers excluded when no calendar
# ---------------------------------------------------------------------------


@given(last_id=_optional_morning_id)
@settings(max_examples=100)
def test_calendar_openers_excluded_without_calendar(last_id: str | None):
    """When has_calendar is False, no calendar-category opener is returned."""
    context = {"has_calendar": False, "has_tasks": True, "has_yesterday": True}
    result = select_opener(MORNING_OPENER_POOL, last_id, context)
    cat = result["category"]
    cat_val = cat.value if isinstance(cat, OpenerCategory) else cat
    assert cat_val != OpenerCategory.CALENDAR.value


# ---------------------------------------------------------------------------
# Property: Context filtering — task openers excluded when no tasks
# ---------------------------------------------------------------------------


@given(last_id=_optional_morning_id)
@settings(max_examples=100)
def test_task_openers_excluded_without_tasks(last_id: str | None):
    """When has_tasks is False, no task-category opener is returned."""
    context = {"has_calendar": True, "has_tasks": False, "has_yesterday": True}
    result = select_opener(MORNING_OPENER_POOL, last_id, context)
    cat = result["category"]
    cat_val = cat.value if isinstance(cat, OpenerCategory) else cat
    assert cat_val != OpenerCategory.TASK.value


# ---------------------------------------------------------------------------
# Property: Context filtering — yesterday openers excluded when no yesterday
# ---------------------------------------------------------------------------


@given(last_id=_optional_morning_id)
@settings(max_examples=100)
def test_yesterday_openers_excluded_without_yesterday(last_id: str | None):
    """When has_yesterday is False, no yesterday-category opener is returned."""
    context = {"has_calendar": True, "has_tasks": True, "has_yesterday": False}
    result = select_opener(MORNING_OPENER_POOL, last_id, context)
    cat = result["category"]
    cat_val = cat.value if isinstance(cat, OpenerCategory) else cat
    assert cat_val != OpenerCategory.YESTERDAY.value


# ---------------------------------------------------------------------------
# Property: Fallback when all context-dependent openers are filtered out
# ---------------------------------------------------------------------------


def test_fallback_when_no_context_available():
    """When no context is available, select_opener still returns a valid
    opener from the context-free categories."""
    context = {"has_calendar": False, "has_tasks": False, "has_yesterday": False}
    for opener in MORNING_OPENER_POOL:
        result = select_opener(MORNING_OPENER_POOL, opener["id"], context)
        assert result["id"] in {o["id"] for o in MORNING_OPENER_POOL}
        assert result["id"] != opener["id"]


# ---------------------------------------------------------------------------
# Property: Result is always a valid opener from the pool
# ---------------------------------------------------------------------------


@given(last_id=_optional_morning_id, context=_context_flags)
@settings(max_examples=100)
def test_result_is_from_morning_pool(last_id: str | None, context: dict):
    """The returned opener is always a member of the provided pool."""
    result = select_opener(MORNING_OPENER_POOL, last_id, context)
    pool_ids = {o["id"] for o in MORNING_OPENER_POOL}
    assert result["id"] in pool_ids


@given(last_id=_optional_evening_id, context=_context_flags)
@settings(max_examples=100)
def test_result_is_from_evening_pool(last_id: str | None, context: dict):
    """The returned opener is always a member of the provided pool."""
    result = select_opener(EVENING_OPENER_POOL, last_id, context)
    pool_ids = {o["id"] for o in EVENING_OPENER_POOL}
    assert result["id"] in pool_ids


# ---------------------------------------------------------------------------
# Property: Each opener has required fields
# ---------------------------------------------------------------------------


def test_morning_openers_have_required_fields():
    """Every morning opener has id, category, and template fields."""
    for opener in MORNING_OPENER_POOL:
        assert "id" in opener, f"Missing 'id' in opener: {opener}"
        assert "category" in opener, f"Missing 'category' in opener: {opener}"
        assert "template" in opener, f"Missing 'template' in opener: {opener}"
        assert "{name}" in opener["template"], (
            f"Opener {opener['id']} template missing {{name}} placeholder"
        )


def test_evening_openers_have_required_fields():
    """Every evening opener has id, category, and template fields."""
    for opener in EVENING_OPENER_POOL:
        assert "id" in opener, f"Missing 'id' in opener: {opener}"
        assert "category" in opener, f"Missing 'category' in opener: {opener}"
        assert "template" in opener, f"Missing 'template' in opener: {opener}"
        assert "{name}" in opener["template"], (
            f"Opener {opener['id']} template missing {{name}} placeholder"
        )


# ---------------------------------------------------------------------------
# Edge case: single-element pool
# ---------------------------------------------------------------------------


def test_single_element_pool_returns_that_element():
    """A pool with one opener always returns it, even if it was the last."""
    pool = [{"id": "only", "category": "direct", "template": "Hi {name}"}]
    result = select_opener(pool, "only", {})
    assert result["id"] == "only"


# ---------------------------------------------------------------------------
# Edge case: empty pool raises ValueError
# ---------------------------------------------------------------------------


def test_empty_pool_raises():
    """An empty pool raises ValueError."""
    import pytest

    with pytest.raises(ValueError, match="opener_pool must not be empty"):
        select_opener([], None, {})


# ===========================================================================
# Approach rotation tests (Requirement 12.3)
# ===========================================================================

# ---------------------------------------------------------------------------
# Strategies for approach rotation
# ---------------------------------------------------------------------------

_approach_values = st.sampled_from(APPROACHES)
_optional_approach = st.one_of(st.none(), _approach_values)
_context_bools = st.fixed_dictionaries(
    {
        "has_calendar_events": st.booleans(),
        "has_pending_tasks": st.booleans(),
    }
)


# ---------------------------------------------------------------------------
# Property: APPROACHES constant matches Approach enum
# ---------------------------------------------------------------------------


def test_approaches_constant_matches_enum():
    """APPROACHES list contains exactly the Approach enum values."""
    assert set(APPROACHES) == {a.value for a in Approach}


# ---------------------------------------------------------------------------
# Property: No consecutive repeat when alternatives exist
# ---------------------------------------------------------------------------


@given(last=_approach_values, ctx=_context_bools)
@settings(max_examples=200)
def test_approach_no_consecutive_repeat(last: str, ctx: dict):
    """select_approach never returns the same approach as last_approach
    when at least one alternative is eligible.

    **Validates: Requirements 12.3**
    """
    result = select_approach(
        last_approach=last,
        has_calendar_events=ctx["has_calendar_events"],
        has_pending_tasks=ctx["has_pending_tasks"],
    )
    # Count how many approaches are eligible
    eligible_count = 1  # open_question always eligible
    if ctx["has_calendar_events"]:
        eligible_count += 1
    if ctx["has_pending_tasks"]:
        eligible_count += 1

    if eligible_count > 1:
        assert result != last, (
            f"Consecutive repeat: got {result!r} again with "
            f"eligible_count={eligible_count}"
        )
    # When only 1 eligible, repeat is allowed (no alternative)
    assert result in APPROACHES


# ---------------------------------------------------------------------------
# Property: Result is always a valid approach
# ---------------------------------------------------------------------------


@given(last=_optional_approach, ctx=_context_bools)
@settings(max_examples=200)
def test_approach_result_is_valid(last: str | None, ctx: dict):
    """select_approach always returns a valid approach string."""
    result = select_approach(
        last_approach=last,
        has_calendar_events=ctx["has_calendar_events"],
        has_pending_tasks=ctx["has_pending_tasks"],
    )
    assert result in APPROACHES


# ---------------------------------------------------------------------------
# Property: First call (None last_approach) works
# ---------------------------------------------------------------------------


@given(ctx=_context_bools)
@settings(max_examples=100)
def test_approach_first_call_none(ctx: dict):
    """select_approach works when last_approach is None (first call)."""
    result = select_approach(
        last_approach=None,
        has_calendar_events=ctx["has_calendar_events"],
        has_pending_tasks=ctx["has_pending_tasks"],
    )
    assert result in APPROACHES


# ---------------------------------------------------------------------------
# Property: calendar_led excluded when no calendar events
# ---------------------------------------------------------------------------


@given(last=_optional_approach)
@settings(max_examples=100)
def test_approach_calendar_excluded_without_events(last: str | None):
    """calendar_led is never returned when has_calendar_events is False."""
    result = select_approach(
        last_approach=last,
        has_calendar_events=False,
        has_pending_tasks=True,
    )
    assert result != Approach.CALENDAR_LED


# ---------------------------------------------------------------------------
# Property: task_led excluded when no pending tasks
# ---------------------------------------------------------------------------


@given(last=_optional_approach)
@settings(max_examples=100)
def test_approach_task_excluded_without_tasks(last: str | None):
    """task_led is never returned when has_pending_tasks is False."""
    result = select_approach(
        last_approach=last,
        has_calendar_events=True,
        has_pending_tasks=False,
    )
    assert result != Approach.TASK_LED


# ---------------------------------------------------------------------------
# Property: open_question always available
# ---------------------------------------------------------------------------


def test_approach_open_question_always_available():
    """When no context is available, open_question is always returned."""
    for _ in range(50):
        result = select_approach(
            last_approach=None,
            has_calendar_events=False,
            has_pending_tasks=False,
        )
        assert result == Approach.OPEN_QUESTION


# ---------------------------------------------------------------------------
# Edge: only open_question eligible and it was last — still returned
# ---------------------------------------------------------------------------


def test_approach_single_eligible_allows_repeat():
    """When only open_question is eligible and it was the last approach,
    it is still returned (no alternative exists)."""
    result = select_approach(
        last_approach=Approach.OPEN_QUESTION,
        has_calendar_events=False,
        has_pending_tasks=False,
    )
    assert result == Approach.OPEN_QUESTION



# ===========================================================================
# Streak tracking tests (Requirement 12.4)
# ===========================================================================

# ---------------------------------------------------------------------------
# Strategies for streak tracking
# ---------------------------------------------------------------------------

_dates = st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 12, 31))
_streak_counts = st.integers(min_value=0, max_value=365)


# ---------------------------------------------------------------------------
# Property: First call starts streak at 1
# ---------------------------------------------------------------------------


@given(today=_dates)
@settings(max_examples=100)
def test_streak_first_call_starts_at_one(today: date):
    """When last_active_date is None (first call), streak starts at 1."""
    new_streak, new_date = update_streak(0, None, today)
    assert new_streak == 1
    assert new_date == today


# ---------------------------------------------------------------------------
# Property: Consecutive day increments streak
# ---------------------------------------------------------------------------


@given(streak=_streak_counts, today=_dates)
@settings(max_examples=200)
def test_streak_consecutive_day_increments(streak: int, today: date):
    """When last_active_date is yesterday, streak increments by 1."""
    yesterday = today - timedelta(days=1)
    new_streak, new_date = update_streak(streak, yesterday, today)
    assert new_streak == streak + 1
    assert new_date == today


# ---------------------------------------------------------------------------
# Property: Same day is idempotent
# ---------------------------------------------------------------------------


@given(streak=_streak_counts, today=_dates)
@settings(max_examples=200)
def test_streak_same_day_idempotent(streak: int, today: date):
    """When last_active_date is today, streak is unchanged."""
    new_streak, new_date = update_streak(streak, today, today)
    assert new_streak == streak
    assert new_date == today


# ---------------------------------------------------------------------------
# Property: Gap resets streak to 1
# ---------------------------------------------------------------------------


@given(streak=_streak_counts, today=_dates, gap=st.integers(min_value=2, max_value=365))
@settings(max_examples=200)
def test_streak_gap_resets(streak: int, today: date, gap: int):
    """When there's a gap of 2+ days, streak resets to 1."""
    assume(today.toordinal() - gap >= date(2020, 1, 1).toordinal())
    old_date = today - timedelta(days=gap)
    new_streak, new_date = update_streak(streak, old_date, today)
    assert new_streak == 1
    assert new_date == today


# ---------------------------------------------------------------------------
# Property: Streak is always >= 1 after update (except same-day no-op)
# ---------------------------------------------------------------------------


@given(
    streak=_streak_counts,
    last_date=st.one_of(st.none(), _dates),
    today=_dates,
)
@settings(max_examples=200)
def test_streak_always_positive_after_update(
    streak: int, last_date: date | None, today: date
):
    """After update_streak, the returned streak is always >= 1
    (or unchanged if same day)."""
    if last_date is not None and last_date > today:
        assume(False)  # skip future dates
    new_streak, _ = update_streak(streak, last_date, today)
    if last_date == today:
        assert new_streak == streak
    else:
        assert new_streak >= 1


# ===========================================================================
# Two-week variation tests (Requirement 12.4, Property 23)
# ===========================================================================

# ---------------------------------------------------------------------------
# Property 23: get_two_week_variation returns variation for days 10-14
# ---------------------------------------------------------------------------


@given(streak=st.integers(min_value=10, max_value=14))
@settings(max_examples=50)
def test_two_week_variation_returns_for_10_to_14(streak: int):
    """get_two_week_variation returns a variation config when streak
    is between 10 and 14 inclusive.

    **Validates: Requirements 12.4**
    """
    result = get_two_week_variation(streak)
    assert result is not None
    assert "type" in result
    assert "instruction_override" in result
    # Type must be one of the known variation types
    valid_types = {v.value for v in TwoWeekVariationType}
    result_type = result["type"]
    type_val = result_type.value if hasattr(result_type, "value") else result_type
    assert type_val in valid_types


# ---------------------------------------------------------------------------
# Property 23: get_two_week_variation returns None outside 10-14
# ---------------------------------------------------------------------------


@given(streak=st.integers(min_value=0, max_value=9))
@settings(max_examples=50)
def test_two_week_variation_none_below_10(streak: int):
    """get_two_week_variation returns None when streak < 10."""
    assert get_two_week_variation(streak) is None


@given(streak=st.integers(min_value=15, max_value=365))
@settings(max_examples=50)
def test_two_week_variation_none_above_14(streak: int):
    """get_two_week_variation returns None when streak > 14."""
    assert get_two_week_variation(streak) is None


# ---------------------------------------------------------------------------
# Property: Variation pool has at least one entry
# ---------------------------------------------------------------------------


def test_two_week_variation_pool_not_empty():
    """The variation pool must have at least one entry."""
    assert len(_TWO_WEEK_VARIATIONS) >= 1


# ---------------------------------------------------------------------------
# Property: All variations have required fields
# ---------------------------------------------------------------------------


def test_two_week_variations_have_required_fields():
    """Every variation in the pool has type and instruction_override."""
    for var in _TWO_WEEK_VARIATIONS:
        assert "type" in var
        assert "instruction_override" in var
        assert isinstance(var["instruction_override"], str)
        assert len(var["instruction_override"]) > 0


# ---------------------------------------------------------------------------
# Edge: streak exactly at boundaries
# ---------------------------------------------------------------------------


def test_two_week_variation_boundary_9():
    """Streak of 9 returns None (just below threshold)."""
    assert get_two_week_variation(9) is None


def test_two_week_variation_boundary_10():
    """Streak of 10 returns a variation (lower boundary)."""
    result = get_two_week_variation(10)
    assert result is not None


def test_two_week_variation_boundary_14():
    """Streak of 14 returns a variation (upper boundary)."""
    result = get_two_week_variation(14)
    assert result is not None


def test_two_week_variation_boundary_15():
    """Streak of 15 returns None (just above threshold)."""
    assert get_two_week_variation(15) is None
