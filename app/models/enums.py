"""Str enums for all status and type fields.

Every enum here must have a matching DB-level CheckConstraint
on the corresponding model column (added in tasks 1.3–1.9).
"""

from enum import Enum


class WindowType(str, Enum):
    """CallWindow.window_type — morning / afternoon / evening."""

    MORNING = "morning"
    AFTERNOON = "afternoon"
    EVENING = "evening"


class CallLogStatus(str, Enum):
    """CallLog.status — full lifecycle of a call instance."""

    SCHEDULED = "scheduled"
    DISPATCHING = "dispatching"
    RINGING = "ringing"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    MISSED = "missed"
    DEFERRED = "deferred"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class CallType(str, Enum):
    """CallLog.call_type — which window or on-demand."""

    MORNING = "morning"
    AFTERNOON = "afternoon"
    EVENING = "evening"
    ON_DEMAND = "on_demand"


class OccurrenceKind(str, Enum):
    """CallLog.occurrence_kind — how the row was created."""

    PLANNED = "planned"
    ON_DEMAND = "on_demand"
    RETRY = "retry"
    RESCHEDULED = "rescheduled"


class OutcomeConfidence(str, Enum):
    """CallLog.call_outcome_confidence / reflection_confidence."""

    CLEAR = "clear"
    PARTIAL = "partial"
    NONE = "none"


class TaskStatus(str, Enum):
    """Task.status."""

    PENDING = "pending"
    COMPLETED = "completed"
    SNOOZED = "snoozed"


class TaskSource(str, Enum):
    """Task.source — where the task originated."""

    USER_MENTION = "user_mention"
    GMAIL = "gmail"
    CALENDAR = "calendar"
    IMPORT = "import"


class GoalStatus(str, Enum):
    """Goal.status."""

    ACTIVE = "active"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class DraftStatus(str, Enum):
    """EmailDraftState.status."""

    PENDING_REVIEW = "pending_review"
    REVISION_REQUESTED = "revision_requested"
    APPROVED = "approved"
    SENT = "sent"
    ABANDONED = "abandoned"


class OutboundMessageStatus(str, Enum):
    """OutboundMessage.status."""

    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
