"""Property tests for auth layer (P9, P13).

P9  — Twilio signature validation gate: random payloads with valid/invalid
      signatures are correctly accepted/rejected.
P13 — Firebase JWT authentication gate: valid/invalid tokens produce 200 or 401.

Both properties use httpx.AsyncClient against a minimal FastAPI test app that
wires the real auth dependencies, with external calls (Firebase Admin SDK)
patched at the boundary.
"""

import string
import urllib.parse
from unittest.mock import patch, MagicMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from hypothesis import given, settings as h_settings, strategies as st, HealthCheck
from twilio.request_validator import RequestValidator

from app.auth.twilio import verify_twilio_signature

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEST_TWILIO_AUTH_TOKEN = "test_twilio_auth_token_abc123"
TEST_WEBHOOK_BASE_URL = "https://example.com"
WEBHOOK_PATH = "/webhook/whatsapp"
WEBHOOK_URL = TEST_WEBHOOK_BASE_URL + WEBHOOK_PATH

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Simple ASCII form field values (avoid encoding issues with non-ASCII)
_form_value = st.text(
    alphabet=string.ascii_letters + string.digits + " .-_",
    min_size=0,
    max_size=40,
)

# A dict of 1-5 form fields with safe keys
_form_payload = st.dictionaries(
    keys=st.text(alphabet=string.ascii_letters, min_size=1, max_size=15),
    values=_form_value,
    min_size=1,
    max_size=5,
)

# Firebase UID
_firebase_uid = st.text(
    alphabet=string.ascii_letters + string.digits, min_size=10, max_size=40
)

# E.164 phone numbers (valid ones that phonenumbers will accept)
_e164_phone = st.sampled_from([
    "+14155552671",
    "+447911123456",
    "+971501234567",
    "+919876543210",
    "+61412345678",
    "+4915112345678",
    "+33612345678",
    "+818012345678",
])


# ---------------------------------------------------------------------------
# Minimal FastAPI apps for testing
# ---------------------------------------------------------------------------


def _make_twilio_app() -> FastAPI:
    """FastAPI app with a single endpoint guarded by verify_twilio_signature."""
    app = FastAPI()

    @app.post(WEBHOOK_PATH)
    async def webhook(form_data: dict = Depends(verify_twilio_signature)):
        return JSONResponse({"ok": True, "fields": list(form_data.keys())})

    return app


def _make_firebase_app() -> FastAPI:
    """FastAPI app with a single endpoint guarded by get_firebase_user.

    Imports get_firebase_user lazily so patches are active at import time.
    """
    from app.auth.firebase import get_firebase_user
    from app.models.schemas import FirebasePrincipal

    app = FastAPI()

    @app.post("/api/chat")
    async def chat(principal: FirebasePrincipal = Depends(get_firebase_user)):
        return JSONResponse(
            {"uid": principal.uid, "phone": principal.phone_number}
        )

    return app


# ---------------------------------------------------------------------------
# P9: Twilio signature validation gate
# **Validates: Requirements 7.1, 7.2**
# ---------------------------------------------------------------------------


class TestTwilioSignatureValidation:
    """Property 9 — Twilio signature validation gate.

    For any incoming request to the WhatsApp webhook, the endpoint should
    accept the request (HTTP 200) if and only if the X-Twilio-Signature
    header is valid. Invalid or missing signatures → HTTP 403.
    """

    @given(payload=_form_payload)
    @h_settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow])
    @pytest.mark.asyncio
    async def test_valid_signature_accepted(self, payload: dict):
        """Requests signed with the correct auth token are accepted.

        **Validates: Requirements 7.1, 7.2**
        """
        validator = RequestValidator(TEST_TWILIO_AUTH_TOKEN)
        sig = validator.compute_signature(WEBHOOK_URL, payload)

        # Use proper URL encoding for the form body
        body = urllib.parse.urlencode(payload)

        mock_settings = MagicMock()
        mock_settings.TWILIO_AUTH_TOKEN = TEST_TWILIO_AUTH_TOKEN
        mock_settings.WEBHOOK_BASE_URL = TEST_WEBHOOK_BASE_URL

        with patch("app.auth.twilio.get_settings", return_value=mock_settings):
            app = _make_twilio_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    WEBHOOK_PATH,
                    content=body,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "X-Twilio-Signature": sig,
                    },
                )
            assert resp.status_code == 200, (
                f"Expected 200 for valid signature, got {resp.status_code}: {resp.text}"
            )

    @given(payload=_form_payload)
    @h_settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow])
    @pytest.mark.asyncio
    async def test_invalid_signature_rejected(self, payload: dict):
        """Requests with a wrong signature are rejected with 403.

        **Validates: Requirements 7.1, 7.2**
        """
        # Compute signature with a DIFFERENT token
        wrong_validator = RequestValidator("wrong_token_entirely")
        bad_sig = wrong_validator.compute_signature(WEBHOOK_URL, payload)

        body = urllib.parse.urlencode(payload)

        mock_settings = MagicMock()
        mock_settings.TWILIO_AUTH_TOKEN = TEST_TWILIO_AUTH_TOKEN
        mock_settings.WEBHOOK_BASE_URL = TEST_WEBHOOK_BASE_URL

        with patch("app.auth.twilio.get_settings", return_value=mock_settings):
            app = _make_twilio_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    WEBHOOK_PATH,
                    content=body,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "X-Twilio-Signature": bad_sig,
                    },
                )
            assert resp.status_code == 403, (
                f"Expected 403 for invalid signature, got {resp.status_code}"
            )

    @pytest.mark.asyncio
    async def test_missing_signature_rejected(self):
        """Requests with no X-Twilio-Signature header are rejected with 403.

        **Validates: Requirements 7.1, 7.2**
        """
        mock_settings = MagicMock()
        mock_settings.TWILIO_AUTH_TOKEN = TEST_TWILIO_AUTH_TOKEN
        mock_settings.WEBHOOK_BASE_URL = TEST_WEBHOOK_BASE_URL

        with patch("app.auth.twilio.get_settings", return_value=mock_settings):
            app = _make_twilio_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    WEBHOOK_PATH,
                    content="Body=hello",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            assert resp.status_code == 403


# ---------------------------------------------------------------------------
# P13: Firebase JWT authentication gate
# **Validates: Requirements 9.5, 9.6**
# ---------------------------------------------------------------------------


def _patch_firebase():
    """Context manager that patches Firebase SDK calls for testing.

    We must import the module first so that ``unittest.mock.patch`` can
    resolve the dotted path through the ``app.auth`` package.
    """
    import app.auth.firebase  # noqa: F401 — ensure module is loaded

    return patch(
        "app.auth.firebase._ensure_firebase_initialized",
        return_value=None,
    )


class TestFirebaseJWTAuthentication:
    """Property 13 — Firebase JWT authentication gate.

    For any request to /api/chat, the endpoint should extract the phone number
    from a valid Firebase JWT and proceed (200), or return HTTP 401 if the JWT
    is missing, expired, or invalid. A valid JWT without a phone_number claim
    should also result in HTTP 401.
    """

    @given(uid=_firebase_uid, phone=_e164_phone)
    @h_settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow])
    @pytest.mark.asyncio
    async def test_valid_token_accepted(self, uid: str, phone: str):
        """A valid Firebase JWT with uid and phone_number yields 200.

        **Validates: Requirements 9.5, 9.6**
        """
        decoded_token = {"uid": uid, "phone_number": phone}

        import app.auth.firebase as fb_mod  # ensure module is loaded

        with (
            _patch_firebase(),
            patch.object(
                fb_mod.firebase_auth,
                "verify_id_token",
                return_value=decoded_token,
            ),
        ):
            app = _make_firebase_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/chat",
                    headers={"Authorization": "Bearer fake_valid_jwt"},
                )

            assert resp.status_code == 200, (
                f"Expected 200 for valid token, got {resp.status_code}: {resp.text}"
            )
            data = resp.json()
            assert data["uid"] == uid
            # Phone should be E.164 normalised
            assert data["phone"].startswith("+")

    @given(uid=_firebase_uid)
    @h_settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow])
    @pytest.mark.asyncio
    async def test_token_without_phone_rejected(self, uid: str):
        """A JWT that lacks a phone_number claim results in 401.

        **Validates: Requirements 9.5, 9.6**
        """
        decoded_token = {"uid": uid}  # no phone_number

        import app.auth.firebase as fb_mod  # ensure module is loaded

        with (
            _patch_firebase(),
            patch.object(
                fb_mod.firebase_auth,
                "verify_id_token",
                return_value=decoded_token,
            ),
        ):
            app = _make_firebase_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/chat",
                    headers={"Authorization": "Bearer fake_jwt_no_phone"},
                )
            assert resp.status_code == 401, (
                f"Expected 401 for token without phone, got {resp.status_code}"
            )

    @pytest.mark.asyncio
    async def test_missing_token_rejected(self):
        """A request with no Authorization header results in 401.

        **Validates: Requirements 9.5, 9.6**
        """
        with _patch_firebase():
            app = _make_firebase_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/api/chat")
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_expired_token_rejected(self):
        """An expired/invalid JWT results in 401.

        **Validates: Requirements 9.5, 9.6**
        """
        import app.auth.firebase as fb_mod  # ensure module is loaded
        from firebase_admin import auth as _fb_auth

        with (
            _patch_firebase(),
            patch.object(
                fb_mod.firebase_auth,
                "verify_id_token",
                side_effect=_fb_auth.ExpiredIdTokenError("Token expired", cause=None),
            ),
        ):
            app = _make_firebase_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/chat",
                    headers={"Authorization": "Bearer expired_jwt"},
                )
            assert resp.status_code == 401
