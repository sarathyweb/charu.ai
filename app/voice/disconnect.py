"""Early disconnect detection for voice calls.

Detects calls that disconnect within a short threshold (default 10 seconds)
before any meaningful user utterance.  These are treated as missed calls
and trigger retry logic.

Design references:
  - Design §2: Voice Call Pipeline (early disconnect detection)
  - Property 25: Early disconnect detection
  - Requirement 6: Missed Call Retry Behavior
  - Requirement 14.4: Early disconnect → treat as missed
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

#: Default threshold in seconds — calls shorter than this with no user
#: utterance are treated as missed.
DEFAULT_THRESHOLD_SECONDS: float = 10.0


@dataclass
class EarlyDisconnectDetector:
    """Detect calls that disconnect before meaningful user interaction.

    Usage::

        detector = EarlyDisconnectDetector()
        detector.mark_connected()          # when pipeline starts
        # ... during the call, TranscriptCollector tracks first_user_utterance_at ...
        detector.mark_disconnected()       # when pipeline ends

        if detector.is_early_disconnect(first_user_utterance_at):
            # treat as missed, trigger retry

    The detector tracks ``connected_at`` and ``disconnected_at`` timestamps.
    It consults the ``TranscriptCollector.first_user_utterance_at`` (passed
    in at check time) to determine whether the user spoke.
    """

    threshold_seconds: float = DEFAULT_THRESHOLD_SECONDS
    connected_at: datetime | None = field(default=None, init=False)
    disconnected_at: datetime | None = field(default=None, init=False)

    def mark_connected(self) -> None:
        """Record the moment the pipeline starts (call connected)."""
        self.connected_at = datetime.now(timezone.utc)

    def mark_disconnected(self) -> None:
        """Record the moment the pipeline ends (call disconnected)."""
        self.disconnected_at = datetime.now(timezone.utc)

    @property
    def elapsed_seconds(self) -> float:
        """Return elapsed time between connect and disconnect in seconds.

        Returns 0.0 if either timestamp is missing.
        """
        if self.connected_at is None or self.disconnected_at is None:
            return 0.0
        delta = (self.disconnected_at - self.connected_at).total_seconds()
        return max(delta, 0.0)

    def is_early_disconnect(
        self,
        first_user_utterance_at: datetime | None,
    ) -> bool:
        """Return True if the call qualifies as an early disconnect.

        A call is an early disconnect when:
        1. The connection was established (``connected_at`` is set).
        2. The elapsed time is less than ``threshold_seconds``.
        3. No meaningful user utterance was detected
           (``first_user_utterance_at`` is None).

        If ``connected_at`` was never set (pipeline never started), this
        returns True — the call never really connected.
        """
        if self.connected_at is None:
            # Pipeline never started — treat as missed
            return True

        elapsed = self.elapsed_seconds
        has_user_utterance = first_user_utterance_at is not None

        is_early = elapsed < self.threshold_seconds and not has_user_utterance

        if is_early:
            logger.info(
                "Early disconnect detected: elapsed=%.1fs, "
                "threshold=%.1fs, user_utterance=%s",
                elapsed,
                self.threshold_seconds,
                has_user_utterance,
            )

        return is_early
