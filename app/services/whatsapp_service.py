"""WhatsAppService — Twilio-based WhatsApp messaging with template and free-form support.

All outbound WhatsApp goes through Twilio REST API (client.messages.create).
Template messages use content_sid + content_variables (Twilio Content API).
Free-form messages use multi-message splitting instead of truncation.

For proactive sends (recap, checkin, weekly, draft review), use
``OutboundMessageService`` which wraps this service with DB-backed
at-most-once dedup via the ``OutboundMessage`` table.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from starlette.concurrency import run_in_threadpool
from twilio.rest import Client as TwilioClient

from app.config import get_settings

if TYPE_CHECKING:
    from app.models.user import User

logger = logging.getLogger(__name__)


class WhatsAppPartialSendError(Exception):
    """Raised when some but not all message chunks were delivered.

    The caller should treat this as a non-retryable failure for the dedup
    layer — the partial content was already delivered, so retrying would
    duplicate the sent chunks.  The dedup record should be marked ``sent``
    with the SIDs that succeeded, and the partial delivery logged for
    operator review.
    """

    def __init__(
        self,
        sent_sids: list[str],
        total_chunks: int,
        cause: Exception,
    ) -> None:
        self.sent_sids = sent_sids
        self.total_chunks = total_chunks
        self.cause = cause
        super().__init__(
            f"Partial send: {len(sent_sids)}/{total_chunks} chunks delivered"
        )

# Twilio's per-message body limit for WhatsApp via client.messages.create
MAX_WHATSAPP_BODY = 1600

# Backward-compatible aliases (referenced by existing tests; will be removed in task 5.3)
MAX_WHATSAPP_LENGTH = MAX_WHATSAPP_BODY
TRUNCATION_SUFFIX = "..."  # deprecated — splitting replaces truncation

# WhatsApp template body limit (the template definition itself)
MAX_TEMPLATE_BODY = 1024

# 24-hour window duration in seconds
WHATSAPP_WINDOW_SECONDS = 24 * 60 * 60

# Sentence-ending pattern for smart splitting
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_PARAGRAPH_BREAK = re.compile(r"\n\n+")


class WhatsAppService:
    """Sends outbound WhatsApp messages via the Twilio REST API."""

    def __init__(self, twilio_client: TwilioClient | None = None) -> None:
        settings = get_settings()
        self._client = twilio_client or TwilioClient(
            settings.TWILIO_ACCOUNT_SID,
            settings.TWILIO_AUTH_TOKEN,
        )
        self._from_number = settings.TWILIO_WHATSAPP_NUMBER

    # ------------------------------------------------------------------
    # Free-form messaging (within 24-hour window)
    # ------------------------------------------------------------------

    async def send_reply(self, to: str, body: str) -> list[str]:
        """Send a free-form WhatsApp message, splitting into multiple
        messages if the body exceeds 1600 chars.

        Returns a list of Twilio message SIDs for each chunk sent.

        Raises ``WhatsAppPartialSendError`` if some but not all chunks
        were delivered — the caller must decide whether to retry the
        remaining chunks.  A complete failure (zero chunks sent) raises
        the underlying exception so the caller can mark the send as
        failed and allow retries.
        """
        if not body or not body.strip():
            logger.warning("Skipping WhatsApp reply to %s — empty body", to)
            return []

        chunks = split_message(body)
        sids: list[str] = []
        first_error: Exception | None = None

        for i, chunk in enumerate(chunks):
            try:
                msg = await run_in_threadpool(
                    self._client.messages.create,
                    from_=self._from_number,
                    to=f"whatsapp:{to}",
                    body=chunk,
                )
                sids.append(msg.sid)
            except Exception as exc:
                logger.exception(
                    "Failed to send WhatsApp reply chunk %d/%d to %s",
                    i + 1,
                    len(chunks),
                    to,
                )
                if first_error is None:
                    first_error = exc
                # Stop sending remaining chunks — partial delivery is
                # worse than a clean failure because the dedup layer
                # would consume the key with incomplete content.
                break

        if first_error is not None:
            if sids:
                # Partial send: some chunks delivered, tail lost.
                raise WhatsAppPartialSendError(
                    sent_sids=sids,
                    total_chunks=len(chunks),
                    cause=first_error,
                )
            # Complete failure — propagate so caller can mark as failed
            raise first_error

        return sids

    # ------------------------------------------------------------------
    # Template messaging (works outside 24-hour window)
    # ------------------------------------------------------------------

    async def send_template_message(
        self,
        to: str,
        content_sid: str,
        content_variables: dict[str, str] | None = None,
    ) -> str:
        """Send a pre-approved WhatsApp template via Twilio Content API.

        Args:
            to: Recipient phone in E.164 format (e.g. "+919025589022").
            content_sid: Twilio Content SID (HXXX...) identifying the template.
            content_variables: Key-value pairs for template placeholders.

        Returns:
            The Twilio message SID on success.

        Raises:
            Exception: Any Twilio error is propagated to the caller so the
            dedup layer can distinguish ambiguous failures (timeout after
            possible acceptance) from definitive rejections.
        """
        kwargs: dict = {
            "from_": self._from_number,
            "to": f"whatsapp:{to}",
            "content_sid": content_sid,
        }
        if content_variables:
            kwargs["content_variables"] = json.dumps(content_variables)

        msg = await run_in_threadpool(
            self._client.messages.create,
            **kwargs,
        )
        return msg.sid

    # ------------------------------------------------------------------
    # 24-hour window check
    # ------------------------------------------------------------------

    @staticmethod
    def is_within_service_window(user: User) -> bool:
        """Return True if the user has an open 24-hour WhatsApp service window.

        The window opens when the user sends a WhatsApp message and lasts 24 hours.
        Outside this window, only template messages are allowed.
        """
        if user.last_user_whatsapp_message_at is None:
            return False
        elapsed = (
            datetime.now(timezone.utc) - user.last_user_whatsapp_message_at
        ).total_seconds()
        return elapsed < WHATSAPP_WINDOW_SECONDS


# ======================================================================
# Message splitting (module-level, testable independently)
# ======================================================================


def split_message(text: str, limit: int = MAX_WHATSAPP_BODY) -> list[str]:
    """Split *text* into chunks of at most *limit* chars each.

    Splitting strategy (in priority order):
    1. Paragraph boundaries (double newline)
    2. Sentence boundaries (after . ! ?)
    3. Hard split at *limit* (last resort)

    Concatenating all returned chunks reproduces the original text exactly.
    No content is silently dropped.
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        # Try to find a paragraph break within the limit
        split_pos = _find_split_point(remaining, limit)
        chunks.append(remaining[:split_pos])
        remaining = remaining[split_pos:]

    return chunks


def _find_split_point(text: str, limit: int) -> int:
    """Find the best position to split *text* at or before *limit*.

    Prefers paragraph breaks, then sentence ends, then hard cut.
    """
    window = text[:limit]

    # 1. Try paragraph break (last double-newline within limit)
    last_para = 0
    for m in _PARAGRAPH_BREAK.finditer(window):
        last_para = m.end()
    if last_para > 0:
        return last_para

    # 2. Try sentence boundary (last sentence-ending whitespace within limit)
    last_sentence = 0
    for m in _SENTENCE_END.finditer(window):
        last_sentence = m.end()
    if last_sentence > 0:
        return last_sentence

    # 3. Hard split at limit
    return limit


# ======================================================================
# Template parameter builders
# ======================================================================


def _truncate_param(value: str, max_len: int, suffix: str = "") -> str:
    """Truncate a template parameter value to fit within *max_len* chars.

    If *suffix* is provided and truncation is needed, the suffix is appended
    and the value is shortened to make room for it.
    """
    if len(value) <= max_len:
        return value
    if suffix:
        return value[: max_len - len(suffix)] + suffix
    return value[:max_len]


def _budget_for_params(
    template_body_len: int,
    fixed_param_total: int,
    target_param_name: str,  # noqa: ARG001 — kept for clarity
) -> int:
    """Compute the remaining character budget for a variable-length parameter.

    Template body limit is 1024 chars. The rendered body = template skeleton
    + all parameter values. We estimate the budget for the target parameter
    as: MAX_TEMPLATE_BODY - template_body_len - fixed_param_total.
    """
    budget = MAX_TEMPLATE_BODY - template_body_len - fixed_param_total
    return max(budget, 50)  # floor at 50 chars to avoid useless truncation


def build_daily_recap_params(
    user_name: str,
    goal: str,
    next_action: str,
    date_str: str,
) -> dict[str, str]:
    """Build content_variables for the ``daily_recap`` template."""
    return {
        "1": _truncate_param(date_str, 60),
        "2": _truncate_param(goal, 200),
        "3": _truncate_param(next_action, 200),
        "4": _truncate_param(user_name, 60),
    }


def build_daily_recap_no_goal_params(user_name: str) -> dict[str, str]:
    """Build content_variables for the ``daily_recap_no_goal`` template."""
    return {
        "1": _truncate_param(user_name, 60),
    }


def build_evening_recap_params(
    user_name: str,
    accomplishments: str,
    tomorrow_intention: str,
    date_str: str,
) -> dict[str, str]:
    """Build content_variables for the ``evening_recap`` template."""
    return {
        "1": _truncate_param(date_str, 60),
        "2": _truncate_param(accomplishments, 300),
        "3": _truncate_param(tomorrow_intention, 200),
        "4": _truncate_param(user_name, 60),
    }


def build_evening_recap_no_accomplishments_params(
    user_name: str,
) -> dict[str, str]:
    """Build content_variables for ``evening_recap_no_accomplishments``."""
    return {
        "1": _truncate_param(user_name, 60),
    }


def build_midday_checkin_params(
    user_name: str,
    next_action: str,
) -> dict[str, str]:
    """Build content_variables for any ``midday_checkin`` variant."""
    return {
        "1": _truncate_param(user_name, 60),
        "2": _truncate_param(next_action, 300),
    }


def build_weekly_summary_params(
    user_name: str,
    week_range: str,
    calls_answered: int,
    goals_set: int,
    closing_message: str,
) -> dict[str, str]:
    """Build content_variables for the ``weekly_summary`` template."""
    return {
        "1": _truncate_param(week_range, 60),
        "2": str(calls_answered),
        "3": str(goals_set),
        "4": _truncate_param(closing_message, 200),
        "5": _truncate_param(user_name, 60),
    }


def build_missed_call_params(user_name: str) -> dict[str, str]:
    """Build content_variables for ``missed_call_encouragement``."""
    return {
        "1": _truncate_param(user_name, 60),
    }


def build_email_draft_review_params(
    sender_name: str,
    subject: str,
    draft_text: str,
) -> tuple[dict[str, str], str | None]:
    """Build content_variables for ``email_draft_review``.

    Returns:
        A tuple of (content_variables, overflow_text).
        *overflow_text* is the full draft body to send as a follow-up
        free-form message if the draft was too long for the template.
        It is ``None`` when the draft fits within the template budget.
    """
    # Template skeleton chars (emoji, labels, fixed text) ≈ 80 chars
    fixed_overhead = len(sender_name) + len(subject) + 80
    draft_budget = MAX_TEMPLATE_BODY - fixed_overhead
    draft_budget = max(draft_budget, 100)

    overflow: str | None = None
    if len(draft_text) > draft_budget:
        suffix = "… (full draft follows)"
        preview = draft_text[: draft_budget - len(suffix)] + suffix
        overflow = draft_text
    else:
        preview = draft_text

    variables = {
        "1": _truncate_param(sender_name, 100),
        "2": _truncate_param(subject, 200),
        "3": preview,
    }
    return variables, overflow
