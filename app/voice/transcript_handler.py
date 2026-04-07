"""Transcript collection helper for voice calls.

Collects user and assistant transcript entries in memory during a call.
After the call ends the caller can retrieve the full transcript list
for persistence.

Design references:
  - Design §2: TranscriptProcessor captures user and assistant turns
  - Requirement 14 AC5-7: Transcript generation and storage
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class TranscriptEntry:
    """A single transcript turn."""

    role: str  # "user" or "assistant"
    content: str
    timestamp: str | None = None


@dataclass
class TranscriptCollector:
    """Collects transcript entries during a voice call.

    Usage::

        collector = TranscriptCollector()
        collector.add_user_entry("Hello")
        collector.add_assistant_entry("Hi there!")
        entries = collector.entries  # after pipeline ends
    """

    entries: list[TranscriptEntry] = field(default_factory=list)
    first_user_utterance_at: datetime | None = field(default=None, init=False)

    def add_user_entry(self, text: str) -> None:
        """Add a user transcript entry."""
        now = datetime.now(timezone.utc)
        if self.first_user_utterance_at is None:
            self.first_user_utterance_at = now
        self.entries.append(
            TranscriptEntry(
                role="user",
                content=text,
                timestamp=now.isoformat(),
            )
        )

    def add_assistant_entry(self, text: str) -> None:
        """Add an assistant transcript entry."""
        self.entries.append(
            TranscriptEntry(
                role="assistant",
                content=text,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

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
