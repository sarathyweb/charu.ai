"""Shared utilities — phone normalization and helpers."""

import phonenumbers


def normalize_phone(raw: str, default_region: str | None = None) -> str:
    """Normalize a phone number to E.164 format.

    Args:
        raw: Raw phone number string (e.g. "+971501234567", "0501234567").
        default_region: ISO 3166-1 alpha-2 region code used when *raw*
            lacks an international prefix (e.g. "AE", "US").

    Returns:
        The phone number in E.164 format (e.g. "+971501234567").

    Raises:
        ValueError: If the number cannot be parsed or is not valid.
    """
    try:
        parsed = phonenumbers.parse(raw, default_region)
    except phonenumbers.NumberParseException as exc:
        raise ValueError(f"Invalid phone number: {raw}") from exc

    if not phonenumbers.is_valid_number(parsed):
        raise ValueError(f"Invalid phone number: {raw}")

    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
