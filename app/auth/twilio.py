"""Twilio X-Twilio-Signature validation dependency."""

from fastapi import HTTPException, Request, status
from twilio.request_validator import RequestValidator

from app.config import get_settings


async def verify_twilio_signature(request: Request) -> dict:
    """Validate the ``X-Twilio-Signature`` header and return parsed form data.

    Uses ``WEBHOOK_BASE_URL`` from settings for URL construction so the
    validation works correctly behind reverse proxies / load balancers.

    Returns:
        A ``dict`` of the parsed form data on success.

    Raises:
        HTTPException 403 if the signature is missing or invalid.
    """
    settings = get_settings()
    validator = RequestValidator(settings.TWILIO_AUTH_TOKEN)

    # Build the canonical URL from the trusted base URL + request path
    url = settings.WEBHOOK_BASE_URL.rstrip("/") + request.url.path

    form_data = await request.form()
    signature = request.headers.get("X-Twilio-Signature", "")

    if not validator.validate(url, dict(form_data), signature):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid Twilio signature",
        )

    return dict(form_data)
