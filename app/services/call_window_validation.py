"""Call window validation helpers.

Pure functions used by CallWindowService (task 3.3) and tested
by property tests (task 1.10).  No DB access — validation only.
"""

from datetime import time
from zoneinfo import available_timezones

# Minimum window width in minutes (Requirement 2.V2).
MIN_WINDOW_WIDTH_MINUTES = 20


def validate_call_window(
    start_time: time,
    end_time: time,
    timezone: str,
) -> tuple[bool, str | None]:
    """Validate a call window's inputs.

    Returns (True, None) on success or (False, error_message) on failure.

    Rules (from Requirements 2.3, 2.4, 2.V1, 2.V2):
      - ``timezone`` must be a valid IANA identifier.
      - ``end_time`` must be strictly after ``start_time`` (no cross-midnight).
      - The window must be at least 20 minutes wide.
    """
    # 1. IANA timezone check
    if timezone not in available_timezones():
        return (
            False,
            f"Invalid timezone: {timezone}. Use an IANA identifier like America/New_York.",
        )

    # 2. Cross-midnight / start >= end
    start_minutes = start_time.hour * 60 + start_time.minute
    end_minutes = end_time.hour * 60 + end_time.minute
    if end_minutes <= start_minutes:
        return False, "End time must be after start time (no cross-midnight windows)."

    # 3. Minimum width
    width = end_minutes - start_minutes
    if width < MIN_WINDOW_WIDTH_MINUTES:
        return False, (
            f"Call window must be at least {MIN_WINDOW_WIDTH_MINUTES} minutes wide. "
            f"Got {width} minutes."
        )

    return True, None
