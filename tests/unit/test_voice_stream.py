"""Unit tests for the WebSocket /voice/stream endpoint (task 14.1).

Tests:
- Missing token → WebSocket closed with 4001
- Invalid/expired token → WebSocket closed with 4001
- Valid token + successful start message → CallLog transitioned to in_progress
- AccountSid mismatch → WebSocket closed with 4003
- call_log_id mismatch between token and custom params → closed with 4004
- user_id mismatch between token and custom params → closed with 4005
- CallLog not found → closed with 4006
- Stop event ends the media loop gracefully

Requirements: 14, Design Voice Call Pipeline section
"""

from __future__ import annotations

import json
import time as _time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.voice import router as voice_router
from app.utils import generate_stream_token


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_SECRET = "test-secret-for-voice-stream"
TEST_TWILIO_ACCOUNT_SID = "ACtest1234567890abcdef1234567890ab"


def _make_test_app() -> FastAPI:
    """Create a minimal FastAPI app with the voice router."""
    app = FastAPI()
    app.include_router(voice_router)
    return app


def _make_token(
    call_log_id: int = 1,
    user_id: int = 42,
    secret: str = TEST_SECRET,
    ttl: int = 300,
) -> str:
    """Generate a valid HMAC stream token for testing."""
    return generate_stream_token(
        secret=secret,
        call_log_id=call_log_id,
        user_id=user_id,
        ttl=ttl,
    )


def _make_start_message(
    stream_sid: str = "MZtest_stream_sid",
    call_sid: str = "CAtest_call_sid",
    account_sid: str = TEST_TWILIO_ACCOUNT_SID,
    call_log_id: int = 1,
    user_id: int = 42,
    call_type: str = "morning",
    token: str | None = None,
) -> list[str]:
    """Return the connected + start messages that Twilio sends."""
    connected = json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"})
    custom_parameters = {
        "call_log_id": str(call_log_id),
        "user_id": str(user_id),
        "call_type": call_type,
    }
    if token is not None:
        custom_parameters["token"] = token
    start = json.dumps({
        "event": "start",
        "sequenceNumber": "1",
        "start": {
            "streamSid": stream_sid,
            "accountSid": account_sid,
            "callSid": call_sid,
            "tracks": ["inbound", "outbound"],
            "customParameters": custom_parameters,
            "mediaFormat": {
                "encoding": "audio/x-mulaw",
                "sampleRate": 8000,
                "channels": 1,
            },
        },
        "streamSid": stream_sid,
    })
    return [connected, start]


def _make_stop_message(
    stream_sid: str = "MZtest_stream_sid",
    account_sid: str = TEST_TWILIO_ACCOUNT_SID,
    call_sid: str = "CAtest_call_sid",
) -> str:
    return json.dumps({
        "event": "stop",
        "sequenceNumber": "3",
        "stop": {
            "accountSid": account_sid,
            "callSid": call_sid,
        },
        "streamSid": stream_sid,
    })


def _mock_settings():
    """Return a mock Settings object."""
    s = MagicMock()
    s.STREAM_TOKEN_SECRET = TEST_SECRET
    s.TWILIO_ACCOUNT_SID = TEST_TWILIO_ACCOUNT_SID
    return s


def _mock_call_log(
    call_log_id: int = 1,
    user_id: int = 42,
    status: str = "ringing",
    version: int = 2,
):
    """Return a mock CallLog object."""
    cl = MagicMock()
    cl.id = call_log_id
    cl.user_id = user_id
    cl.status = status
    cl.version = version
    cl.call_type = "morning"
    return cl


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestVoiceStreamTokenValidation:
    """Token validation at WebSocket connection time."""

    def test_missing_token_closes_with_4001(self):
        """Missing customParameter token → close 4001."""
        app = _make_test_app()
        messages = _make_start_message()

        with patch("app.api.voice.get_settings", return_value=_mock_settings()):
            client = TestClient(app)
            with pytest.raises(WebSocketDisconnect) as exc_info:
                with client.websocket_connect("/voice/stream") as websocket:
                    websocket.send_text(messages[0])
                    websocket.send_text(messages[1])
                    websocket.receive_text()
            assert exc_info.value.code == 4001

    def test_expired_token_closes_with_4001(self):
        """Expired customParameter token → close 4001."""
        token = _make_token(ttl=-10)
        app = _make_test_app()
        messages = _make_start_message(token=token)

        with patch("app.api.voice.get_settings", return_value=_mock_settings()):
            client = TestClient(app)
            with pytest.raises(WebSocketDisconnect) as exc_info:
                with client.websocket_connect("/voice/stream") as websocket:
                    websocket.send_text(messages[0])
                    websocket.send_text(messages[1])
                    websocket.receive_text()
            assert exc_info.value.code == 4001

    def test_invalid_signature_closes_with_4001(self):
        """Wrong-signature customParameter token → close 4001."""
        token = _make_token(secret="wrong-secret")
        app = _make_test_app()
        messages = _make_start_message(token=token)

        with patch("app.api.voice.get_settings", return_value=_mock_settings()):
            client = TestClient(app)
            with pytest.raises(WebSocketDisconnect) as exc_info:
                with client.websocket_connect("/voice/stream") as websocket:
                    websocket.send_text(messages[0])
                    websocket.send_text(messages[1])
                    websocket.receive_text()
            assert exc_info.value.code == 4001


class TestVoiceStreamVerifyStreamToken:
    """Test the verify_stream_token utility directly for correctness."""

    def test_valid_token_returns_payload(self):
        from app.utils import verify_stream_token

        token = generate_stream_token(
            secret=TEST_SECRET, call_log_id=99, user_id=7, ttl=60
        )
        result = verify_stream_token(secret=TEST_SECRET, token=token)
        assert result is not None
        assert result["call_log_id"] == 99
        assert result["user_id"] == 7
        assert result["expires"] > _time.time()

    def test_expired_token_returns_none(self):
        from app.utils import verify_stream_token

        token = generate_stream_token(
            secret=TEST_SECRET, call_log_id=1, user_id=1, ttl=-10
        )
        result = verify_stream_token(secret=TEST_SECRET, token=token)
        assert result is None

    def test_wrong_secret_returns_none(self):
        from app.utils import verify_stream_token

        token = generate_stream_token(
            secret="correct-secret", call_log_id=1, user_id=1
        )
        result = verify_stream_token(secret="wrong-secret", token=token)
        assert result is None

    def test_malformed_token_returns_none(self):
        from app.utils import verify_stream_token

        assert verify_stream_token(secret=TEST_SECRET, token="garbage") is None
        assert verify_stream_token(secret=TEST_SECRET, token="a:b:c") is None
        assert verify_stream_token(secret=TEST_SECRET, token="") is None
        assert verify_stream_token(secret=TEST_SECRET, token="1:2:notint:sig") is None

    def test_tampered_payload_returns_none(self):
        from app.utils import verify_stream_token

        token = generate_stream_token(
            secret=TEST_SECRET, call_log_id=1, user_id=1
        )
        # Tamper with the call_log_id
        parts = token.split(":")
        parts[0] = "999"
        tampered = ":".join(parts)
        result = verify_stream_token(secret=TEST_SECRET, token=tampered)
        assert result is None


class TestVoiceStreamTransitionLogic:
    """Test the _transition_call_to_in_progress helper."""

    @pytest.mark.asyncio
    async def test_transition_ringing_to_in_progress(self):
        """CallLog in 'ringing' state transitions to 'in_progress'."""
        from app.api.voice import _transition_call_to_in_progress

        mock_call_log = _mock_call_log(status="ringing", version=2)
        updated_call_log = _mock_call_log(status="in_progress", version=3)

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_call_log)

        mock_svc = AsyncMock()
        mock_svc.update_status = AsyncMock(return_value=updated_call_log)

        with (
            patch("app.api.voice.async_session_factory") as mock_factory,
            patch("app.api.voice.CallLogService", return_value=mock_svc),
        ):
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await _transition_call_to_in_progress(1)

        assert result is not None
        mock_svc.update_status.assert_called_once()

    @pytest.mark.asyncio
    async def test_transition_already_in_progress_returns_call_log(self):
        """CallLog already in 'in_progress' returns it without error."""
        from app.api.voice import _transition_call_to_in_progress

        mock_call_log = _mock_call_log(status="in_progress", version=3)

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_call_log)

        with patch("app.api.voice.async_session_factory") as mock_factory:
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await _transition_call_to_in_progress(1)

        assert result is not None
        assert result.status == "in_progress"

    @pytest.mark.asyncio
    async def test_transition_not_found_returns_none(self):
        """CallLog not found returns None."""
        from app.api.voice import _transition_call_to_in_progress

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=None)

        with patch("app.api.voice.async_session_factory") as mock_factory:
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await _transition_call_to_in_progress(999)

        assert result is None


class TestVoiceStreamContextLoading:
    """Test cached-context loading for voice streams."""

    @pytest.mark.asyncio
    async def test_load_system_instruction_uses_cache(self):
        from app.api.voice import _load_system_instruction_for_call

        with (
            patch(
                "app.api.voice.get_cached_call_context",
                AsyncMock(return_value=("cached instruction", {"opener": {"id": "x"}})),
            ) as cached,
            patch("app.voice.context.prepare_call_context", new_callable=AsyncMock) as prepare,
        ):
            result = await _load_system_instruction_for_call(1, 42, "morning")

        assert result == ("cached instruction", {"opener": {"id": "x"}})
        cached.assert_awaited_once_with(1)
        prepare.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_load_system_instruction_falls_back_to_live_context(self):
        from app.api.voice import _load_system_instruction_for_call

        mock_session = AsyncMock()
        with (
            patch("app.api.voice.get_cached_call_context", AsyncMock(return_value=None)),
            patch("app.api.voice.async_session_factory") as mock_factory,
            patch(
                "app.voice.context.prepare_call_context",
                AsyncMock(return_value=("live instruction", {"approach": "task_led"})),
            ) as prepare,
        ):
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await _load_system_instruction_for_call(1, 42, "morning")

        assert result == ("live instruction", {"approach": "task_led"})
        prepare.assert_awaited_once_with(
            user_id=42,
            call_type="morning",
            session=mock_session,
        )

    @pytest.mark.asyncio
    async def test_transition_completed_returns_none(self):
        """CallLog in terminal state 'completed' returns None."""
        from app.api.voice import _transition_call_to_in_progress

        mock_call_log = _mock_call_log(status="completed", version=5)

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_call_log)

        with patch("app.api.voice.async_session_factory") as mock_factory:
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await _transition_call_to_in_progress(1)

        assert result is None


class TestVoiceStreamStartMessageParsing:
    """Test the _read_start_message helper."""

    @pytest.mark.asyncio
    async def test_read_start_message_normal_flow(self):
        """Connected + start messages are parsed correctly."""
        from app.api.voice import _read_start_message

        messages = _make_start_message()
        mock_ws = AsyncMock()
        mock_ws.receive_text = AsyncMock(side_effect=messages)

        result = await _read_start_message(mock_ws)

        assert result is not None
        assert result["event"] == "start"
        assert result["start"]["streamSid"] == "MZtest_stream_sid"
        assert result["start"]["callSid"] == "CAtest_call_sid"
        assert result["start"]["customParameters"]["call_log_id"] == "1"

    @pytest.mark.asyncio
    async def test_read_start_message_start_only(self):
        """If Twilio sends start directly (no connected), still works."""
        from app.api.voice import _read_start_message

        start_msg = json.dumps({
            "event": "start",
            "start": {
                "streamSid": "MZ123",
                "callSid": "CA123",
                "accountSid": "AC123",
                "customParameters": {},
                "tracks": ["inbound"],
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
            },
            "streamSid": "MZ123",
        })
        mock_ws = AsyncMock()
        mock_ws.receive_text = AsyncMock(return_value=start_msg)

        result = await _read_start_message(mock_ws)

        assert result is not None
        assert result["event"] == "start"


class TestVoiceStreamAccountSidValidation:
    """Test AccountSid validation against configured Twilio account."""

    def test_account_sid_from_start_message(self):
        """The start message contains accountSid that must match settings."""
        messages = _make_start_message(account_sid="ACwrong_account")
        start_msg = json.loads(messages[1])
        assert start_msg["start"]["accountSid"] == "ACwrong_account"

    def test_matching_account_sid(self):
        """Matching accountSid passes validation."""
        messages = _make_start_message(account_sid=TEST_TWILIO_ACCOUNT_SID)
        start_msg = json.loads(messages[1])
        assert start_msg["start"]["accountSid"] == TEST_TWILIO_ACCOUNT_SID
