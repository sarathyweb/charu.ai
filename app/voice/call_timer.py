"""CallTimerProcessor — custom FrameProcessor that enforces call duration limits.

Injects a wrap-up system message at the warning mark and sends an EndFrame
at the hard cutoff.  Configurable for morning/afternoon (5 min / 4 min warn)
and evening (3 min / 2 min warn) calls.

Design references:
  - Design §2: Call types and timers table
  - Requirement 4 AC6: 5-minute limit, wrap-up at 4 minutes
  - Requirement 20 AC5: 3-minute limit for evening calls
"""

from __future__ import annotations

import asyncio
import logging
import time as _time

from pipecat.frames.frames import (
    EndFrame,
    Frame,
    LLMMessagesFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger(__name__)

# ── Timer presets per call type ──────────────────────────────────────────

TIMER_PRESETS: dict[str, dict[str, int]] = {
    "morning": {"max_duration": 300, "warn_at": 240},
    "afternoon": {"max_duration": 300, "warn_at": 240},
    "evening": {"max_duration": 180, "warn_at": 120},
    "on_demand": {"max_duration": 300, "warn_at": 240},
}

_WRAP_UP_MSG = (
    "[SYSTEM: {elapsed} seconds elapsed — {remaining} seconds remaining. "
    "Begin wrapping up. Steer toward a summary and say goodbye.]"
)


class CallTimerProcessor(FrameProcessor):
    """Injects time-warning messages and terminates the pipeline at the limit.

    Place this processor *before* the LLM in the pipeline so that the
    injected ``LLMMessagesFrame`` reaches the model.

    Parameters
    ----------
    max_duration:
        Hard cutoff in seconds.  An ``EndFrame`` is pushed when elapsed
        time reaches this value.
    warn_at:
        Seconds after which a wrap-up system message is injected.
    """

    def __init__(
        self,
        *,
        max_duration: int = 300,
        warn_at: int = 240,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.max_duration = max_duration
        self.warn_at = warn_at
        self._start_time: float | None = None
        self._warned = False

    # ── helpers ───────────────────────────────────────────────────────

    @property
    def elapsed(self) -> float:
        if self._start_time is None:
            return 0.0
        return _time.monotonic() - self._start_time

    # ── frame processing ─────────────────────────────────────────────

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        # Lazily start the clock on the first frame we see.
        if self._start_time is None:
            self._start_time = _time.monotonic()

        elapsed = self.elapsed

        # 1) Inject wrap-up warning once
        if elapsed >= self.warn_at and not self._warned:
            self._warned = True
            remaining = max(0, self.max_duration - int(elapsed))
            warn_text = _WRAP_UP_MSG.format(
                elapsed=int(elapsed), remaining=remaining
            )
            logger.info(
                "CallTimer: injecting wrap-up warning at %.1fs", elapsed
            )
            await self.push_frame(
                LLMMessagesFrame(
                    [{"role": "user", "content": warn_text}]
                )
            )

        # 2) Hard cutoff — push EndFrame to terminate the pipeline
        if elapsed >= self.max_duration:
            logger.info(
                "CallTimer: hard cutoff reached at %.1fs — ending pipeline",
                elapsed,
            )
            await self.push_frame(EndFrame())
            # Don't forward the current frame — pipeline is ending.
            return

        # 3) Always forward the frame
        await self.push_frame(frame, direction)


def create_call_timer(call_type: str) -> CallTimerProcessor:
    """Factory: return a ``CallTimerProcessor`` configured for *call_type*.

    Recognised call types: ``morning``, ``afternoon``, ``evening``,
    ``on_demand``.  Unknown types default to the morning preset.
    """
    preset = TIMER_PRESETS.get(call_type, TIMER_PRESETS["morning"])
    return CallTimerProcessor(
        max_duration=preset["max_duration"],
        warn_at=preset["warn_at"],
    )
