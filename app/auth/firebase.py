"""Firebase JWT verification dependency — verify_id_token and FirebasePrincipal."""

import firebase_admin
from fastapi import Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import auth as firebase_auth
from firebase_admin import credentials

from app.models.schemas import FirebasePrincipal
from app.utils import normalize_phone

# ---------------------------------------------------------------------------
# Bearer token scheme (auto_error=False so we handle missing tokens ourselves)
# ---------------------------------------------------------------------------
_bearer_scheme = HTTPBearer(auto_error=False)


def _ensure_firebase_initialized() -> None:
    """Initialize Firebase Admin SDK once (idempotent)."""
    if not firebase_admin._apps:
        from app.config import get_settings

        _settings = get_settings()
        _cred = credentials.Certificate(_settings.FIREBASE_CREDENTIALS_PATH)
        firebase_admin.initialize_app(_cred)


async def get_firebase_user(
    token: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> FirebasePrincipal:
    """Verify a Firebase JWT and return a FirebasePrincipal.

    Raises HTTP 401 if the token is missing, invalid, expired, or lacks a
    ``phone_number`` claim.
    """
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    _ensure_firebase_initialized()

    try:
        # verify_id_token is a blocking network call — offload to threadpool
        decoded = await run_in_threadpool(
            firebase_auth.verify_id_token, token.credentials
        )
    except (
        firebase_auth.InvalidIdTokenError,
        firebase_auth.ExpiredIdTokenError,
        firebase_auth.RevokedIdTokenError,
        firebase_auth.CertificateFetchError,
        ValueError,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    uid: str | None = decoded.get("uid") or decoded.get("sub")
    phone_raw: str | None = decoded.get("phone_number")

    if not uid or not phone_raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        phone = normalize_phone(phone_raw)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return FirebasePrincipal(uid=uid, phone_number=phone)
