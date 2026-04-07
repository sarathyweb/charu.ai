"""Transcript collection helper for Pipecat voice calls.

Wraps ``TranscriptProcessor`` and collects transcript entries in memory
during a call.  After the call ends the caller can retrieve the full
transcript list for persistence.

Design references:
  - Design §2: TranscriptProcessor captures user and assistant turns
  - Requirement 14 AC5-7: Transcript generation and storage
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pipecat.processors.transcript_processor import TranscriptProcessor

logger = logging.getLogger(__name__)


@dataclass
class TranscriptEntry:
    """A single transcript turn."""

    role: str  # "user" or "assistant"
    content: str
    timestamp: str | None = None


@dataclass
class TranscriptCollector:
    """Collects transcript entries from a ``TranscriptProcessor``.

    Usage::

        collector = TranscriptCollector()
        transcript_proc = collector.create_processor()
        # … use transcript_proc.user() and transcript_proc.assistant()
        # in the pipeline …
        entries = collector.entries  # after pipeline ends
    """

    entries: list[TranscriptEntry] = field(default_factory=list)
    first_user_utterance_at: datetime | None = field(default=None, init=False)
    _processor: TranscriptProcessor | None = field(default=None, init=False)

    def create_processor(self) -> TranscriptProcessor:
        """Create and return a wired ``TranscriptProcessor``."""
        self._processor = TranscriptProcessor()

        @self._processor.event_handler("on_transcript_update")
        async def _on_update(processor, frame):  # noqa: ARG001
            for msg in frame.messages:
                self.entries.append(
                    TranscriptEntry(
                        role=msg.role,
                        content=msg.content,
                        timestamp=msg.timestamp,
                    )
                )
                # Track first user utterance for early-disconnect detection
                if (
                    msg.role == "user"
                    and self.first_user_utterance_at is None
                ):
                    self.first_user_utterance_at = datetime.now(timezone.utc)

        return self._processor

    @property
    def processor(self) -> TranscriptProcessor:
        """Return the underlying processor (must call ``create_processor`` first)."""
        if self._processor is None:
            raise RuntimeError(
                "Call create_processor() before accessing .processor"
            )
        return self._processor

    def to_dicts(self) -> list[dict]:
        """Serialise entries to a list of plain dicts."""
        return [
            {
                "role": e.role,
                "content": e.content,
                "timestamp": e.timestamp,
            }
            for e in self.entries
        ]
