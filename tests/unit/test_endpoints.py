"""Unit tests for API endpoints (task 10.6).

Tests:
- GET /health returns 200 with {"status": "ok"}
- POST /api/chat with valid JWT returns 200 with reply and session_id
- POST /api/chat with missing/invalid JWT returns 401
- POST /webhook/whatsapp with invalid signature returns 403
- POST /webhook/whatsapp with empty Body returns 200 silently
- Lifespan: after startup, app.state.runner and app.state.session_service exist

Requirements: 6.1–6.6, 9.5, 9.6
"""

import urllib.parse
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.auth_sync import router as auth_sync_router
from app.api.chat import router as chat_router
from app.api.health import router as health_router
from app.api.whatsapp import router as whatsapp_router
from app.auth.firebase import get_firebase_user
from app.auth.twilio import verify_twilio_signature
from app.dependencies import get_agent_service, get_db_session, get_user_service
from app.models.schemas import AgentRunResult, FirebasePrincipal


# ---------------------------------------------------------------------------
# Helpers — build a test FastAPI app with overridden dependencies
# ---------------------------------------------------------------------------

FAKE_PRINCIPAL = FirebasePrincipal(uid="test-uid-123", phone_number="+14155552671")
FAKE_AGENT_RESULT = AgentRunResult(reply="Hello from agent", session_id="sess-abc")


def _make_test_app() -> FastAPI:
    """Create a FastAPI app with all routers and dependency overrides."""
    app = FastAPI()
    app.include_router(health_router)
    app.include_router(auth_sync_router)
    app.include_router(chat_router)
    app.include_router(whatsapp_router)
    return app


def _override_firebase_valid(app: FastAPI) -> None:
    """Override Firebase auth to return a valid principal."""
    app.dependency_overrides[get_firebase_user] = lambda: FAKE_PRINCIPAL


def _override_services(app: FastAPI) -> None:
    """Override UserService and AgentService with mocks."""
    mock_user_svc = AsyncMock()
    mock_user_svc.ensure_from_firebase = AsyncMock(return_value=(MagicMock(), False))
    mock_user_svc.ensure_from_whatsapp = AsyncMock(return_value=MagicMock())
    mock_user_svc.get_by_phone = AsyncMock(return_value=None)

    mock_agent_svc = AsyncMock()
    mock_agent_svc.run = AsyncMock(return_value=FAKE_AGENT_RESULT)

    mock_session = AsyncMock()

    app.dependency_overrides[get_user_service] = lambda: mock_user_svc
    app.dependency_overrides[get_agent_service] = lambda: mock_agent_svc
    app.dependency_overrides[get_db_session] = lambda: mock_session


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    """GET /health returns 200 with {"status": "ok"}, no auth required."""

    @pytest.mark.asyncio
    async def test_health_returns_ok(self):
        app = _make_test_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# POST /api/auth/sync
# ---------------------------------------------------------------------------


class TestAuthSyncEndpoint:
    """POST /api/auth/sync — sync Firebase user to PostgreSQL."""

    @pytest.mark.asyncio
    async def test_new_user_returns_created_true(self):
        """Valid JWT + first-time phone → 200 with created: true."""
        app = _make_test_app()
        _override_firebase_valid(app)

        mock_user_svc = AsyncMock()
        mock_user_svc.ensure_from_firebase = AsyncMock(
            return_value=(MagicMock(), True)
        )
        mock_session = AsyncMock()
        app.dependency_overrides[get_user_service] = lambda: mock_user_svc
        app.dependency_overrides[get_db_session] = lambda: mock_session

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/auth/sync",
                headers={"Authorization": "Bearer fake_valid_jwt"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["phone"] == FAKE_PRINCIPAL.phone_number
        assert data["created"] is True

    @pytest.mark.asyncio
    async def test_existing_user_returns_created_false(self):
        """Valid JWT + existing phone → 200 with created: false."""
        app = _make_test_app()
        _override_firebase_valid(app)

        mock_user_svc = AsyncMock()
        mock_user_svc.ensure_from_firebase = AsyncMock(
            return_value=(MagicMock(), False)
        )
        mock_session = AsyncMock()
        app.dependency_overrides[get_user_service] = lambda: mock_user_svc
        app.dependency_overrides[get_db_session] = lambda: mock_session

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/auth/sync",
                headers={"Authorization": "Bearer fake_valid_jwt"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["phone"] == FAKE_PRINCIPAL.phone_number
        assert data["created"] is False

    @pytest.mark.asyncio
    async def test_missing_jwt_returns_401(self):
        """No Authorization header → 401."""
        app = _make_test_app()
        _override_services(app)

        with patch(
            "app.auth.firebase._ensure_firebase_initialized", return_value=None
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.post("/api/auth/sync")

        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/chat
# ---------------------------------------------------------------------------


class TestChatEndpoint:
    """POST /api/chat — authenticated web chat."""

    @pytest.mark.asyncio
    async def test_valid_jwt_returns_reply(self):
        """Valid JWT + message → 200 with reply and session_id."""
        app = _make_test_app()
        _override_firebase_valid(app)
        _override_services(app)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/chat",
                json={"message": "Hello"},
                headers={"Authorization": "Bearer fake_valid_jwt"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["reply"] == "Hello from agent"
        assert data["session_id"] == "sess-abc"

    @pytest.mark.asyncio
    async def test_missing_jwt_returns_401(self):
        """No Authorization header → 401."""
        app = _make_test_app()
        # Do NOT override Firebase auth — let the real dependency run
        _override_services(app)

        # Patch _ensure_firebase_initialized to avoid needing real credentials
        with patch(
            "app.auth.firebase._ensure_firebase_initialized", return_value=None
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.post("/api/chat", json={"message": "Hello"})

        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_jwt_returns_401(self):
        """Invalid/expired JWT → 401."""
        app = _make_test_app()
        _override_services(app)

        import app.auth.firebase as fb_mod
        from firebase_admin import auth as _fb_auth

        with (
            patch(
                "app.auth.firebase._ensure_firebase_initialized",
                return_value=None,
            ),
            patch.object(
                fb_mod.firebase_auth,
                "verify_id_token",
                side_effect=_fb_auth.InvalidIdTokenError("bad token"),
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/chat",
                    json={"message": "Hello"},
                    headers={"Authorization": "Bearer bad_jwt"},
                )

        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /webhook/whatsapp
# ---------------------------------------------------------------------------

TEST_TWILIO_AUTH_TOKEN = "test_twilio_auth_token_abc123"
TEST_WEBHOOK_BASE_URL = "https://example.com"
WEBHOOK_PATH = "/webhook/whatsapp"


class TestWhatsAppEndpoint:
    """POST /webhook/whatsapp — Twilio WhatsApp webhook."""

    @pytest.mark.asyncio
    async def test_invalid_signature_returns_403(self):
        """Invalid X-Twilio-Signature → 403."""
        app = _make_test_app()
        _override_services(app)

        mock_settings = MagicMock()
        mock_settings.TWILIO_AUTH_TOKEN = TEST_TWILIO_AUTH_TOKEN
        mock_settings.WEBHOOK_BASE_URL = TEST_WEBHOOK_BASE_URL

        payload = {"From": "whatsapp:+14155552671", "Body": "Hi", "MessageSid": "SM1"}
        body = urllib.parse.urlencode(payload)

        with patch("app.auth.twilio.get_settings", return_value=mock_settings):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.post(
                    WEBHOOK_PATH,
                    content=body,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "X-Twilio-Signature": "invalid_signature",
                    },
                )

        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_empty_body_returns_200(self):
        """Valid signature but empty Body → 200 silently (no agent call)."""
        app = _make_test_app()
        _override_services(app)

        # Override Twilio signature verification to return form data with empty Body
        form_data = {
            "From": "whatsapp:+14155552671",
            "Body": "",
            "MessageSid": "SM2",
        }
        app.dependency_overrides[verify_twilio_signature] = lambda: form_data

        # Mock the DB session used by the whatsapp endpoint's local _get_db_session
        mock_session = AsyncMock()
        with patch(
            "app.api.whatsapp._get_db_session",
            return_value=_async_gen(mock_session),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.post(
                    WEBHOOK_PATH,
                    content=urllib.parse.urlencode(form_data),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )

        assert resp.status_code == 200


# Helper to create an async generator from a value (for Depends override)
async def _async_gen(value):
    yield value


# ---------------------------------------------------------------------------
# Lifespan — app.state.runner and app.state.session_service exist
# ---------------------------------------------------------------------------


class TestLifespan:
    """After startup, app.state.runner and app.state.session_service exist."""

    @pytest.mark.asyncio
    async def test_lifespan_sets_app_state(self):
        """A lifespan that sets runner and session_service makes them
        available on app.state during the lifespan context.
        """
        mock_runner = MagicMock()
        mock_session_service = MagicMock()

        @asynccontextmanager
        async def fake_lifespan(app: FastAPI):
            app.state.runner = mock_runner
            app.state.session_service = mock_session_service
            yield

        app = FastAPI(lifespan=fake_lifespan)

        # Directly invoke the lifespan context manager
        async with fake_lifespan(app):
            assert app.state.runner is mock_runner
            assert app.state.session_service is mock_session_service

    @pytest.mark.asyncio
    async def test_real_lifespan_sets_state(self):
        """The real lifespan from app.main sets runner and session_service.

        We patch the heavy external dependencies (Firebase, DB, ADK) to
        verify the wiring without needing real infrastructure.
        """
        with (
            patch("app.main.firebase_admin") as mock_fb_admin,
            patch("app.main.credentials") as mock_creds,
            patch("app.main.create_db_tables", new_callable=AsyncMock),
            patch("app.main.DatabaseSessionService") as mock_dss_cls,
            patch("app.main.Runner") as mock_runner_cls,
            patch("app.main.get_settings") as mock_get_settings,
        ):
            mock_fb_admin._apps = {}
            mock_creds.Certificate.return_value = MagicMock()
            mock_get_settings.return_value = MagicMock(
                FIREBASE_CREDENTIALS_PATH="/fake/path.json",
                DATABASE_URL="postgresql+asyncpg://fake:fake@localhost/fake",
            )
            mock_dss_cls.return_value = MagicMock()
            mock_runner_cls.return_value = MagicMock()

            from app.main import lifespan, app as real_app

            # Invoke the real lifespan directly and check state
            async with lifespan(real_app):
                assert hasattr(real_app.state, "runner")
                assert hasattr(real_app.state, "session_service")
                assert real_app.state.runner is mock_runner_cls.return_value
                assert (
                    real_app.state.session_service
                    is mock_dss_cls.return_value
                )
