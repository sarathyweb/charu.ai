"""WhatsAppService — Twilio message sending with truncation."""

import logging

from starlette.concurrency import run_in_threadpool
from twilio.rest import Client as TwilioClient

from app.config import get_settings

logger = logging.getLogger(__name__)

MAX_WHATSAPP_LENGTH = 1600
TRUNCATION_SUFFIX = "..."


class WhatsAppService:
    """Sends outbound WhatsApp replies via the Twilio REST API."""

    def __init__(self, twilio_client: TwilioClient | None = None) -> None:
        settings = get_settings()
        self._client = twilio_client or TwilioClient(
            settings.TWILIO_ACCOUNT_SID,
            settings.TWILIO_AUTH_TOKEN,
        )
        self._from_number = settings.TWILIO_WHATSAPP_NUMBER

    async def send_reply(self, to: str, body: str) -> None:
        """Send a WhatsApp reply, truncating if needed.

        Skips the Twilio API call when *body* is empty or whitespace-only
        (Twilio rejects messages without a body or media).

        Errors are logged but never raised — the webhook must always return 200.
        """
        if not body or not body.strip():
            logger.debug("Skipping WhatsApp reply to %s — empty body", to)
            return

        truncated_body = self._truncate(body)

        try:
            await run_in_threadpool(
                self._client.messages.create,
                from_=self._from_number,
                to=f"whatsapp:{to}",
                body=truncated_body,
            )
        except Exception:
            logger.exception("Failed to send WhatsApp reply to %s", to)

    @staticmethod
    def _truncate(text: str) -> str:
        """Truncate to 1600 chars, appending '...' when shortened."""
        if len(text) <= MAX_WHATSAPP_LENGTH:
            return text
        return text[: MAX_WHATSAPP_LENGTH - len(TRUNCATION_SUFFIX)] + TRUNCATION_SUFFIX
