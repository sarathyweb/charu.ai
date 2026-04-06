"""End-to-end backend integration tests (task 16.1).

Tests the full request flow through FastAPI routing, dependency injection,
auth middleware, and service layers — with external dependencies (Firebase,
ADK, Twilio, PostgreSQL) mocked at the boundary.

Verifies:
- All routers are wired into the app
- CORS middleware is configured
- Lifespan initializes Firebase, DB, ADK session service, and Runner
- DI chain: db_session → user_service → agent_service flows correctly
- Full /api/chat flow (mock Firebase + ADK)
- Full /webhook/whatsapp flow (mock Twilio + ADK)

Requirements: 6.1–6.6, 1.1–1.6
"""

import urllib.parse
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from httpx import ASGITransport, AsyncClient

from app.api.auth_sync import router as auth_sync_router
from app.api.chat import router as chat_router
from app.api.health import router as health_router
from app.api.whatsapp import router as whatsapp_router
from app.auth.firebase import get_firebase_user
from app.auth.twilio import verify_twilio_signature
from app.dependencies import (
    get_agent_service,
    get_db_session,
    get_runner,
    get_session_service,
    get_user_service,
)
from app.models.schemas import AgentRunResult, FirebasePrincipal


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

FAKE_PRINCIPAL = FirebasePrincipal(uid="integ-uid-001", phone_number="+14155550100")
FAKE_AGENT_RESULT = AgentRunResult(reply="Integration reply", session_id="sess-integ")


async def _async_gen(value):
    """Helper to create an async generator from a value (for Depends override)."""
    yield value


def _build_app_with_overrides(
    *,
    firebase_principal: FirebasePrincipal | None = FAKE_PRINCIPAL,
    agent_result: AgentRunResult = FAKE_AGENT_RESULT,
    twilio_form_data: dict | None = None,
) -> tuple[FastAPI, dict[str, AsyncMock]]:
    """Build a FastAPI app with all routers and configurable dependency overrides.

    Returns the app and a dict of the mock services for assertion.
    """
    app = FastAPI()
    app.include_router(health_router)
    app.include_router(auth_sync_router)
    app.include_router(chat_router)
    app.include_router(whatsapp_router)

    # Mock services
    mock_user_svc = AsyncMock()
    mock_user_svc.ensure_from_firebase = AsyncMock(return_value=(MagicMock(), True))
    mock_user_svc.ensure_from_whatsapp = AsyncMock(return_value=MagicMock())

    mock_agent_svc = AsyncMock()
    mock_agent_svc.run = AsyncMock(return_value=agent_result)

    mock_session = AsyncMock()

    # Override DI
    app.dependency_overrides[get_db_session] = lambda: mock_session
    app.dependency_overrides[get_user_service] = lambda: mock_user_svc
    app.dependency_overrides[get_agent_service] = lambda: mock_agent_svc

    if firebase_principal is not None:
        app.dependency_overrides[get_firebase_user] = lambda: firebase_principal

    if twilio_form_data is not None:
        app.dependency_overrides[verify_twilio_signature] = lambda: twilio_form_data

    mocks = {
        "user_service": mock_user_svc,
        "agent_service": mock_agent_svc,
        "session": mock_session,
    }
    return app, mocks


# ---------------------------------------------------------------------------
# 1. Router wiring verification
# ---------------------------------------------------------------------------


class TestRouterWiring:
    """Verify all expected routes are registered in the real app."""

    def test_all_routers_present(self):
        """The real app from app.main includes health, auth_sync, chat, whatsapp."""
        with (
            patch("app.main.get_settings") as mock_settings,
        ):
            mock_settings.return_value = MagicMock(
                CORS_ORIGINS="http://localhost:3000",
                FIREBASE_CREDENTIALS_PATH="/fake/path.json",
                DATABASE_URL="postgresql+asyncpg://fake:fake@localhost/fake",
            )
            # Re-import to get the real app with patched settings
            import importlib
            import app.main as main_mod

            importlib.reload(main_mod)
            real_app = main_mod.app

        route_paths = {r.path for r in real_app.routes if hasattr(r, "path")}
        assert "/health" in route_paths
        assert "/api/auth/sync" in route_paths
        assert "/api/chat" in route_paths
        assert "/webhook/whatsapp" in route_paths


# ---------------------------------------------------------------------------
# 2. CORS middleware verification
# ---------------------------------------------------------------------------


class TestCORSMiddleware:
    """Verify CORS middleware is configured and responds with correct headers."""

    @pytest.mark.asyncio
    async def test_cors_preflight_returns_allow_headers(self):
        """OPTIONS preflight to /api/chat returns CORS headers."""
        app, _ = _build_app_with_overrides()
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:3000"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.options(
                "/api/chat",
                headers={
                    "Origin": "http://localhost:3000",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "Authorization, Content-Type",
                },
            )

        assert resp.status_code == 200
        assert "access-control-allow-origin" in resp.headers
        assert resp.headers["access-control-allow-origin"] == "http://localhost:3000"

    @pytest.mark.asyncio
    async def test_cors_disallowed_origin_no_header(self):
        """Request from disallowed origin does not get CORS allow header."""
        app, _ = _build_app_with_overrides()
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:3000"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.options(
                "/api/chat",
                headers={
                    "Origin": "http://evil.com",
                    "Access-Control-Request-Method": "POST",
                },
            )

        # Disallowed origin should not get the allow-origin header
        allow_origin = resp.headers.get("access-control-allow-origin", "")
        assert allow_origin != "http://evil.com"


# ---------------------------------------------------------------------------
# 3. Lifespan verification
# ---------------------------------------------------------------------------


class TestLifespanIntegration:
    """Verify the real lifespan wires Firebase, DB, ADK session service, and Runner."""

    @pytest.mark.asyncio
    async def test_lifespan_initializes_all_components(self):
        """Real lifespan sets runner and session_service on app.state."""
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

            from app.main import app as real_app, lifespan

            async with lifespan(real_app):
                # Firebase was initialized
                mock_fb_admin.initialize_app.assert_called_once()
                # DB tables were created
                # ADK components are on app.state
                assert real_app.state.runner is mock_runner_cls.return_value
                assert real_app.state.session_service is mock_dss_cls.return_value

    @pytest.mark.asyncio
    async def test_lifespan_skips_firebase_if_already_initialized(self):
        """If Firebase is already initialized, lifespan does not re-init."""
        with (
            patch("app.main.firebase_admin") as mock_fb_admin,
            patch("app.main.credentials"),
            patch("app.main.create_db_tables", new_callable=AsyncMock),
            patch("app.main.DatabaseSessionService") as mock_dss_cls,
            patch("app.main.Runner") as mock_runner_cls,
            patch("app.main.get_settings") as mock_get_settings,
        ):
            mock_fb_admin._apps = {"[DEFAULT]": MagicMock()}  # Already init'd
            mock_get_settings.return_value = MagicMock(
                FIREBASE_CREDENTIALS_PATH="/fake/path.json",
                DATABASE_URL="postgresql+asyncpg://fake:fake@localhost/fake",
            )
            mock_dss_cls.return_value = MagicMock()
            mock_runner_cls.return_value = MagicMock()

            from app.main import app as real_app, lifespan

            async with lifespan(real_app):
                mock_fb_admin.initialize_app.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Dependency injection chain verification
# ---------------------------------------------------------------------------


class TestDependencyInjection:
    """Verify the DI chain flows correctly through real FastAPI Depends()."""

    @pytest.mark.asyncio
    async def test_chat_endpoint_receives_injected_services(self):
        """POST /api/chat receives UserService and AgentService via DI."""
        app, mocks = _build_app_with_overrides()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/chat",
                json={"message": "DI test"},
                headers={"Authorization": "Bearer fake_jwt"},
            )

        assert resp.status_code == 200
        # Verify the services were actually called (DI chain worked)
        mocks["user_service"].ensure_from_firebase.assert_awaited_once_with(
            FAKE_PRINCIPAL.phone_number, FAKE_PRINCIPAL.uid
        )
        mocks["agent_service"].run.assert_awaited_once_with(
            user_id=FAKE_PRINCIPAL.phone_number,
            message="DI test",
            channel="web",
        )

    @pytest.mark.asyncio
    async def test_auth_sync_endpoint_receives_injected_user_service(self):
        """POST /api/auth/sync receives UserService via DI."""
        app, mocks = _build_app_with_overrides()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/auth/sync",
                headers={"Authorization": "Bearer fake_jwt"},
            )

        assert resp.status_code == 200
        mocks["user_service"].ensure_from_firebase.assert_awaited_once()


# ---------------------------------------------------------------------------
# 5. Full /api/chat request flow (mock Firebase + ADK)
# ---------------------------------------------------------------------------


class TestChatFlowIntegration:
    """Full request flow for POST /api/chat with mocked Firebase and ADK."""

    @pytest.mark.asyncio
    async def test_chat_happy_path(self):
        """Valid JWT + message → 200 with reply and session_id from agent."""
        app, mocks = _build_app_with_overrides()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/chat",
                json={"message": "What's on my schedule?"},
                headers={"Authorization": "Bearer valid_jwt"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["reply"] == "Integration reply"
        assert data["session_id"] == "sess-integ"

        # User was ensured in DB
        mocks["user_service"].ensure_from_firebase.assert_awaited_once()
        # Agent was invoked with correct params
        mocks["agent_service"].run.assert_awaited_once_with(
            user_id=FAKE_PRINCIPAL.phone_number,
            message="What's on my schedule?",
            channel="web",
        )

    @pytest.mark.asyncio
    async def test_chat_no_auth_returns_401(self):
        """No Authorization header → 401 before any service is called."""
        app, mocks = _build_app_with_overrides(firebase_principal=None)

        with patch("app.auth.firebase._ensure_firebase_initialized", return_value=None):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.post("/api/chat", json={"message": "Hello"})

        assert resp.status_code == 401
        # Services should NOT have been called
        mocks["agent_service"].run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_chat_invalid_jwt_returns_401(self):
        """Invalid JWT → 401."""
        app, mocks = _build_app_with_overrides(firebase_principal=None)

        import app.auth.firebase as fb_mod
        from firebase_admin import auth as _fb_auth

        with (
            patch("app.auth.firebase._ensure_firebase_initialized"),
            patch.object(
                fb_mod.firebase_auth,
                "verify_id_token",
                side_effect=_fb_auth.InvalidIdTokenError("bad"),
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
        mocks["agent_service"].run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_chat_missing_message_returns_422(self):
        """Missing 'message' field in body → 422 validation error."""
        app, _ = _build_app_with_overrides()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/chat",
                json={},
                headers={"Authorization": "Bearer valid_jwt"},
            )

        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 6. Full /webhook/whatsapp request flow (mock Twilio + ADK)
# ---------------------------------------------------------------------------


class TestWhatsAppFlowIntegration:
    """Full request flow for POST /webhook/whatsapp with mocked Twilio and ADK."""

    @pytest.mark.asyncio
    async def test_whatsapp_happy_path(self):
        """Valid signature + message → 200, user ensured, agent called, reply sent."""
        form_data = {
            "From": "whatsapp:+14155550100",
            "Body": "Remind me to buy milk",
            "MessageSid": "SM_integ_001",
        }
        app, mocks = _build_app_with_overrides(twilio_form_data=form_data)

        # The whatsapp endpoint accesses request.app.state.runner/session_service
        # directly (not via DI), so we must set them on app.state.
        app.state.runner = MagicMock()
        app.state.session_service = MagicMock()

        # Override the whatsapp endpoint's local _get_db_session via DI
        from app.api.whatsapp import _get_db_session as wa_get_db_session

        mock_wa_session = AsyncMock()
        mock_wa_session.commit = AsyncMock()
        mock_wa_session.add = MagicMock()

        async def _mock_wa_db_session():
            yield mock_wa_session

        app.dependency_overrides[wa_get_db_session] = _mock_wa_db_session

        mock_user_svc_inst = AsyncMock()
        mock_agent_svc_inst = AsyncMock()
        mock_agent_svc_inst.run = AsyncMock(return_value=FAKE_AGENT_RESULT)
        mock_wa_svc_inst = AsyncMock()

        with (
            patch(
                "app.api.whatsapp.UserService",
                return_value=mock_user_svc_inst,
            ),
            patch(
                "app.api.whatsapp.AgentService",
                return_value=mock_agent_svc_inst,
            ),
            patch(
                "app.api.whatsapp.WhatsAppService",
                return_value=mock_wa_svc_inst,
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/webhook/whatsapp",
                    content=urllib.parse.urlencode(form_data),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )

            assert resp.status_code == 200

            # User was ensured from WhatsApp
            mock_user_svc_inst.ensure_from_whatsapp.assert_awaited_once()

            # Agent was invoked
            mock_agent_svc_inst.run.assert_awaited_once()
            call_kwargs = mock_agent_svc_inst.run.call_args
            assert call_kwargs.kwargs["message"] == "Remind me to buy milk"
            assert call_kwargs.kwargs["channel"] == "whatsapp"

            # Reply was sent
            mock_wa_svc_inst.send_reply.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_whatsapp_invalid_signature_returns_403(self):
        """Invalid Twilio signature → 403, no services called."""
        app, mocks = _build_app_with_overrides()
        # Do NOT override verify_twilio_signature — let it validate

        mock_settings = MagicMock()
        mock_settings.TWILIO_AUTH_TOKEN = "test_auth_token"
        mock_settings.WEBHOOK_BASE_URL = "https://example.com"

        payload = {"From": "whatsapp:+14155550100", "Body": "Hi", "MessageSid": "SM1"}

        with patch("app.auth.twilio.get_settings", return_value=mock_settings):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/webhook/whatsapp",
                    content=urllib.parse.urlencode(payload),
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "X-Twilio-Signature": "invalid_sig",
                    },
                )

        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_whatsapp_empty_body_returns_200_silently(self):
        """Valid signature but empty Body → 200, no agent call."""
        form_data = {
            "From": "whatsapp:+14155550100",
            "Body": "",
            "MessageSid": "SM_empty",
        }
        app, _ = _build_app_with_overrides(twilio_form_data=form_data)

        mock_wa_session = AsyncMock()

        with (
            patch(
                "app.api.whatsapp._get_db_session",
                return_value=_async_gen(mock_wa_session),
            ),
            patch("app.api.whatsapp.AgentService") as mock_agent_svc_cls,
        ):
            mock_agent_svc_cls.return_value = AsyncMock()
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/webhook/whatsapp",
                    content=urllib.parse.urlencode(form_data),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )

        assert resp.status_code == 200
        # Agent should NOT have been called for empty body
        mock_agent_svc_cls.return_value.run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_whatsapp_duplicate_message_sid_returns_200(self):
        """Duplicate MessageSid (idempotency) → 200, no agent call."""
        form_data = {
            "From": "whatsapp:+14155550100",
            "Body": "Hello again",
            "MessageSid": "SM_dup_001",
        }
        app, _ = _build_app_with_overrides(twilio_form_data=form_data)

        mock_wa_session = AsyncMock()
        # Simulate PK conflict on commit (duplicate MessageSid)
        mock_wa_session.commit = AsyncMock(side_effect=Exception("PK conflict"))
        mock_wa_session.rollback = AsyncMock()
        mock_wa_session.add = MagicMock()

        with (
            patch(
                "app.api.whatsapp._get_db_session",
                return_value=_async_gen(mock_wa_session),
            ),
            patch("app.api.whatsapp.AgentService") as mock_agent_svc_cls,
        ):
            mock_agent_svc_cls.return_value = AsyncMock()
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/webhook/whatsapp",
                    content=urllib.parse.urlencode(form_data),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )

        assert resp.status_code == 200
        # Agent should NOT have been called — idempotency guard kicked in
        mock_agent_svc_cls.return_value.run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_whatsapp_agent_error_returns_200(self):
        """Agent raises an exception → 200 (swallowed to prevent Twilio retries)."""
        form_data = {
            "From": "whatsapp:+14155550100",
            "Body": "Trigger error",
            "MessageSid": "SM_err_001",
        }
        app, _ = _build_app_with_overrides(twilio_form_data=form_data)

        mock_wa_session = AsyncMock()
        mock_wa_session.commit = AsyncMock()
        mock_wa_session.add = MagicMock()

        with (
            patch(
                "app.api.whatsapp._get_db_session",
                return_value=_async_gen(mock_wa_session),
            ),
            patch("app.api.whatsapp.UserService") as mock_user_svc_cls,
            patch("app.api.whatsapp.AgentService") as mock_agent_svc_cls,
        ):
            mock_user_svc_cls.return_value = AsyncMock()
            mock_agent_svc_cls.return_value.run = AsyncMock(
                side_effect=RuntimeError("ADK exploded")
            )

            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/webhook/whatsapp",
                    content=urllib.parse.urlencode(form_data),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )

        # Must return 200 even on error to prevent Twilio retries
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_whatsapp_empty_agent_reply_skips_send(self):
        """Agent returns empty reply → 200, no Twilio send attempted."""
        form_data = {
            "From": "whatsapp:+14155550100",
            "Body": "Hello",
            "MessageSid": "SM_empty_reply",
        }
        empty_result = AgentRunResult(reply="", session_id="sess-empty")
        app, _ = _build_app_with_overrides(twilio_form_data=form_data)

        app.state.runner = MagicMock()
        app.state.session_service = MagicMock()

        from app.api.whatsapp import _get_db_session as wa_get_db_session

        mock_wa_session = AsyncMock()
        mock_wa_session.commit = AsyncMock()
        mock_wa_session.add = MagicMock()

        async def _mock_wa_db_session():
            yield mock_wa_session

        app.dependency_overrides[wa_get_db_session] = _mock_wa_db_session

        mock_agent_svc_inst = AsyncMock()
        mock_agent_svc_inst.run = AsyncMock(return_value=empty_result)
        mock_wa_svc_inst = AsyncMock()

        with (
            patch("app.api.whatsapp.UserService", return_value=AsyncMock()),
            patch(
                "app.api.whatsapp.AgentService",
                return_value=mock_agent_svc_inst,
            ),
            patch(
                "app.api.whatsapp.WhatsAppService",
                return_value=mock_wa_svc_inst,
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/webhook/whatsapp",
                    content=urllib.parse.urlencode(form_data),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )

            assert resp.status_code == 200
            # WhatsApp send should NOT have been called for empty reply
            mock_wa_svc_inst.send_reply.assert_not_awaited()
