"""Gmail read service — fetch emails needing a reply and full email content.

All API calls delegate through ``google_api_call`` (task 8.6) so that
token refresh, auth errors, and retryable errors are handled in one place.

Requirements: 11
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from googleapiclient.discovery import build
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.user import User
from app.services.google_api_wrapper import google_api_call
from app.services.google_oauth_service import build_google_credentials

logger = logging.getLogger(__name__)

# Sender patterns that indicate automated / no-reply emails.
# Matched case-insensitively against the full ``From`` header value.
NO_REPLY_PATTERNS: tuple[str, ...] = (
    "noreply",
    "no-reply",
    "no_reply",
    "donotreply",
    "do-not-reply",
    "do_not_reply",
    "notifications@",
    "notification@",
    "mailer-daemon",
    "postmaster@",
    "automated",
    "newsletter",
    "updates@",
    "digest@",
    "alert@",
    "alerts@",
    "bounce@",
)


def _build_gmail_service(credentials: Any) -> Any:
    """Build a Gmail API v1 service object."""
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def _is_no_reply_sender(from_header: str) -> bool:
    """Return ``True`` if *from_header* matches a known automated sender pattern."""
    lower = from_header.lower()
    return any(pattern in lower for pattern in NO_REPLY_PATTERNS)


def _extract_text_body(payload: dict) -> str:
    """Recursively extract plain-text body from a Gmail MIME payload.

    Prefers ``text/plain``; falls back to ``text/html`` (raw, unstripped)
    if no plain-text part is found.
    """
    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data")

    if mime_type == "text/plain" and body_data:
        return base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")

    # Recurse into multipart parts.
    for part in payload.get("parts", []):
        text = _extract_text_body(part)
        if text:
            return text

    # Fallback: HTML body (agent can handle it for summarisation).
    if mime_type == "text/html" and body_data:
        return base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")

    return ""


def _parse_headers(msg: dict) -> dict[str, str]:
    """Extract a header-name → value mapping from a Gmail message resource."""
    return {
        h["name"]: h["value"]
        for h in msg.get("payload", {}).get("headers", [])
    }


async def get_emails_needing_reply(
    user: User,
    session: AsyncSession,
    *,
    max_results: int = 3,
) -> list[dict] | dict:
    """Fetch emails that likely need a reply from the user.

    Uses a Gmail search query to find recent unread inbox messages from
    other people, then filters out automated / no-reply senders.

    Parameters
    ----------
    user:
        The authenticated user with Gmail connected.
    session:
        Active DB session for token-refresh persistence.
    max_results:
        Maximum number of emails to return (default 3, per Req 11 AC6).

    Returns
    -------
    A list of email summary dicts on success, or a structured error dict
    (with an ``"error"`` key) on auth / API failure.
    """
    credentials = build_google_credentials(
        access_token_encrypted=user.google_access_token_encrypted,
        refresh_token_encrypted=user.google_refresh_token_encrypted,
        token_expiry=user.google_token_expiry,
    )

    service = _build_gmail_service(credentials)

    # Fetch extra candidates to account for no-reply filtering.
    fetch_count = max_results * 3

    query = (
        "in:inbox is:unread -from:me "
        "-category:promotions -category:social "
        "-category:updates newer_than:7d"
    )

    list_result = await google_api_call(
        user=user,
        credentials=credentials,
        api_callable=lambda: service.users().messages().list(
            userId="me",
            q=query,
            maxResults=fetch_count,
        ).execute(),
        session=session,
    )

    if isinstance(list_result, dict) and "error" in list_result:
        return list_result

    message_refs: list[dict] = list_result.get("messages", [])
    if not message_refs:
        return []

    emails: list[dict] = []

    for msg_ref in message_refs:
        if len(emails) >= max_results:
            break

        # Fetch metadata only — efficient, no body download.
        msg_result = await google_api_call(
            user=user,
            credentials=credentials,
            api_callable=lambda msg_id=msg_ref["id"]: service.users().messages().get(
                userId="me",
                id=msg_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute(),
            session=session,
        )

        if isinstance(msg_result, dict) and "error" in msg_result:
            logger.warning(
                "Failed to fetch message %s for user %s: %s",
                msg_ref["id"], user.id, msg_result.get("error"),
            )
            continue

        headers = _parse_headers(msg_result)
        sender = headers.get("From", "Unknown")

        if _is_no_reply_sender(sender):
            continue

        emails.append({
            "id": msg_result["id"],
            "thread_id": msg_result["threadId"],
            "subject": headers.get("Subject", "(No subject)"),
            "from": sender,
            "date": headers.get("Date", ""),
            "snippet": msg_result.get("snippet", ""),
        })

    return emails


async def get_email_for_reply(
    user: User,
    session: AsyncSession,
    *,
    message_id: str,
) -> dict:
    """Fetch the full content of a specific email for drafting a reply.

    Returns the email body, subject, sender, and thread info needed to
    compose a properly-threaded reply.

    Parameters
    ----------
    user:
        The authenticated user with Gmail connected.
    session:
        Active DB session for token-refresh persistence.
    message_id:
        The Gmail message ID of the email to fetch.

    Returns
    -------
    A dict with ``id``, ``thread_id``, ``message_id`` (MIME Message-ID),
    ``subject``, ``from``, and ``body``; or a structured error dict on
    failure.
    """
    credentials = build_google_credentials(
        access_token_encrypted=user.google_access_token_encrypted,
        refresh_token_encrypted=user.google_refresh_token_encrypted,
        token_expiry=user.google_token_expiry,
    )

    service = _build_gmail_service(credentials)

    msg_result = await google_api_call(
        user=user,
        credentials=credentials,
        api_callable=lambda: service.users().messages().get(
            userId="me",
            id=message_id,
            format="full",
        ).execute(),
        session=session,
    )

    if isinstance(msg_result, dict) and "error" in msg_result:
        return msg_result

    headers = _parse_headers(msg_result)
    body_text = _extract_text_body(msg_result.get("payload", {}))

    # Truncate very long bodies to avoid token waste when the agent
    # summarises the email for draft generation.
    max_body_chars = 3000
    if len(body_text) > max_body_chars:
        body_text = body_text[:max_body_chars] + "\n\n[… truncated]"

    return {
        "id": msg_result["id"],
        "thread_id": msg_result["threadId"],
        "message_id": headers.get("Message-ID", ""),
        "subject": headers.get("Subject", "(No subject)"),
        "from": headers.get("From", "Unknown"),
        "date": headers.get("Date", ""),
        "body": body_text,
    }


def format_emails_for_agent(emails: list[dict]) -> str:
    """Format email summaries for injection into agent context at call start.

    Parameters
    ----------
    emails:
        Email summary dicts as returned by ``get_emails_needing_reply``.

    Returns
    -------
    A human-readable summary suitable for the agent's system instruction.
    """
    if not emails:
        return "No emails needing a reply right now."

    count = len(emails)
    lines: list[str] = [f"You have {count} email(s) that might need a reply:"]
    for i, email in enumerate(emails, 1):
        sender_name = email["from"].split("<")[0].strip().strip('"')
        snippet = email.get("snippet", "")[:80]
        lines.append(f'{i}. From {sender_name}: "{email["subject"]}" — {snippet}')

    return "\n".join(lines)
