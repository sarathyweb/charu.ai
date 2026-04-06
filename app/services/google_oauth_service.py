"""Google OAuth token encryption/decryption and credential building.

Provides Fernet-based symmetric encryption for storing OAuth tokens at rest,
and a helper to reconstruct ``google.oauth2.credentials.Credentials`` from
the encrypted values stored on the ``User`` model.

Requirements: 15.3, Implementation Constraints 9
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken
from google.oauth2.credentials import Credentials

from app.config import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level Fernet instance (lazy, cached)
# ---------------------------------------------------------------------------

_fernet: Fernet | None = None
_fernet_lock = threading.Lock()


def _get_fernet() -> Fernet:
    """Return a cached Fernet instance using the env-var key."""
    global _fernet  # noqa: PLW0603
    if _fernet is None:
        with _fernet_lock:
            # Double-check after acquiring the lock
            if _fernet is None:
                key = get_settings().OAUTH_TOKEN_ENCRYPTION_KEY
                if not key:
                    raise RuntimeError(
                        "OAUTH_TOKEN_ENCRYPTION_KEY is not set. "
                        "Generate one with: python -c "
                        '"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
                    )
                _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def encrypt_token(token: str) -> str:
    """Encrypt a plaintext token string and return the ciphertext as a string.

    The result is safe to store in a ``VARCHAR`` / ``TEXT`` DB column.
    """
    return _get_fernet().encrypt(token.encode("utf-8")).decode("utf-8")


def decrypt_token(encrypted: str) -> str:
    """Decrypt a previously encrypted token string.

    Raises ``cryptography.fernet.InvalidToken`` if the ciphertext is
    corrupted or the key has changed.
    """
    return _get_fernet().decrypt(encrypted.encode("utf-8")).decode("utf-8")


def build_google_credentials(
    *,
    access_token_encrypted: str | None,
    refresh_token_encrypted: str | None,
    token_expiry: datetime | None = None,
    scopes: list[str] | None = None,
) -> Credentials:
    """Build ``google.oauth2.credentials.Credentials`` from stored encrypted tokens.

    Parameters
    ----------
    access_token_encrypted:
        Fernet-encrypted access token (from ``User.google_access_token_encrypted``).
        May be ``None`` if only a refresh token is available — the library will
        refresh automatically on first use.
    refresh_token_encrypted:
        Fernet-encrypted refresh token (from ``User.google_refresh_token_encrypted``).
    token_expiry:
        Optional expiry datetime for the access token.
    scopes:
        Optional list of OAuth scopes the token was granted for.

    Returns
    -------
    google.oauth2.credentials.Credentials
        Ready-to-use credentials object.  The ``google-auth`` library handles
        transparent token refresh when the access token expires.
    """
    settings = get_settings()

    access_token: str | None = None
    if access_token_encrypted:
        access_token = decrypt_token(access_token_encrypted)

    refresh_token: str | None = None
    if refresh_token_encrypted:
        refresh_token = decrypt_token(refresh_token_encrypted)

    if not access_token and not refresh_token:
        raise ValueError("At least one of access_token or refresh_token must be provided.")

    return Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
        client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
        expiry=token_expiry,
        scopes=scopes,
    )
