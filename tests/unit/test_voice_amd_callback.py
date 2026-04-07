"""Unit tests for POST /voice/amd-callback (task 15.2).

Tests:
- Machine/fax AnsweredBy values trigger hang-up + missed + retry
- Human/unknown AnsweredBy values proceed normally (no hang-up)
- AnsweredBy is persisted to CallLog.answered_by for ALL outcomes
- Always returns 200 to Twilio
- Terminal state CallLog is not re-transitioned
- Unknown CallSid handled gracefully
- Signature failure returns 200

Requirements: 6.V2
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.enums import CallLogStatus


# ---------------------------------------------------------------------------
# Tests — Machine AnsweredBy set (pure, no DB)
# ---------------------------------------------------------------------------


class TestMachineAnsweredBySet:
    """Verify _MACHINE_ANSWERED_BY covers all expected machine/fax values."""

    def test_machine_start_is_machine(self):
        from app.api.voice import _MACHINE_ANSWERED_BY
        assert "machine_start" in _MACHINE_ANSWERED_BY

    def test_machine_end_beep_is_machine(self):
        from app.api.voice import _MACHINE_ANSWERED_BY
        assert "machine_end_beep" in _MACHINE_ANSWERED_BY

    def test_machine_end_silence_is_machine(self):
        from app.api.voice import _MACHINE_ANSWERED_BY
        assert "machine_end_silence" in _MACHINE_ANSWERED_BY

    def test_machine_end_other_is_machine(self):
        from app.api.voice import _MACHINE_ANSWERED_BY
        assert "machine_end_other" in _MACHINE_ANSWERED_BY

    def test_fax_is_machine(self):
        from app.api.voice import _MACHINE_ANSWERED_BY
        assert "fax" in _MACHINE_ANSWERED_BY

    def test_human_is_not_machine(self):
        from app.api.voice import _MACHINE_ANSWERED_BY
        assert "human" not in _MACHINE_ANSWERED_BY

    def test_unknown_is_not_machine(self):
        from app.api.voice import _MACHINE_ANSWERED_BY
        assert "unknown" not in _MACHINE_ANSWERED_BY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_call_log(
    *,
    id: int = 1,
    status: str = CallLogStatus.RINGING.value,
    version: int = 1,
    twilio_call_sid: str = "CA_test_amd",
    answered_by: str | None = None,
    attempt_number: int = 1,
    user_id: int = 42,
    call_type: str = "morning",
    origin_window_id: int | None = None,
    root_call_log_id: int | None = None,
) -> MagicMock:
    cl = MagicMock()
    cl.id = id
    cl.status = status
    cl.version = version
    cl.twilio_call_sid = twilio_call_sid
    cl.answered_by = answered_by
    cl.attempt_number = attempt_number
    cl.user_id = user_id
    cl.call_type = call_type
    cl.origin_window_id = origin_window_id
    cl.root_call_log_id = root_call_log_id
    cl.call_date = datetime.now(timezone.utc).date()
    cl.scheduled_timezone = "America/New_York"
    return cl


def _make_async_session_ctx(mock_session):
    """Build an async context manager that yields mock_session."""
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    return mock_ctx


# ---------------------------------------------------------------------------
# Tests — Endpoint handler (mocked DB + Twilio)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestAmdCallbackEndpoint:
    """Test the HTTP endpoint behavior with mocked internals."""

    async def test_always_returns_200_on_signature_failure(self):
        """Even if Twilio signature fails, return 200."""
        from app.api.voice import voice_amd_callback

        with patch(
            "app.api.voice.verify_twilio_signature",
            new_callable=AsyncMock,
            side_effect=Exception("bad sig"),
        ):
            request = MagicMock()
            resp = await voice_amd_callback(request)
            assert resp.status_code == 200

    async def test_returns_200_on_empty_call_sid(self):
        """Missing CallSid should return 200."""
        from app.api.voice import voice_amd_callback

        with patch("app.api.voice.verify_twilio_signature", new_callable=AsyncMock) as mock_sig:
            mock_sig.return_value = {"CallSid": "", "AnsweredBy": "human"}
            request = MagicMock()
            resp = await voice_amd_callback(request)
            assert resp.status_code == 200

    async def test_returns_200_on_empty_answered_by(self):
        """Missing AnsweredBy should return 200."""
        from app.api.voice import voice_amd_callback

        with patch("app.api.voice.verify_twilio_signature", new_callable=AsyncMock) as mock_sig:
            mock_sig.return_value = {"CallSid": "CA_test", "AnsweredBy": ""}
            request = MagicMock()
            resp = await voice_amd_callback(request)
            assert resp.status_code == 200

    async def test_returns_200_on_unknown_call_sid(self):
        """Unknown CallSid should return 200."""
        from app.api.voice import voice_amd_callback

        mock_svc = AsyncMock()
        mock_svc.find_by_twilio_sid.return_value = None

        mock_session = AsyncMock()
        mock_ctx = _make_async_session_ctx(mock_session)

        with patch("app.api.voice.verify_twilio_signature", new_callable=AsyncMock) as mock_sig:
            mock_sig.return_value = {
                "CallSid": "CA_unknown",
                "AnsweredBy": "human",
            }
            with patch("app.api.voice.async_session_factory", return_value=mock_ctx):
                with patch("app.api.voice.CallLogService", return_value=mock_svc):
                    request = MagicMock()
                    resp = await voice_amd_callback(request)
                    assert resp.status_code == 200

    async def test_human_persists_answered_by_no_hangup(self):
        """human AnsweredBy should persist to DB but NOT hang up or trigger retry."""
        from app.api.voice import voice_amd_callback

        mock_cl = _mock_call_log()
        mock_svc = AsyncMock()
        mock_svc.find_by_twilio_sid.return_value = mock_cl

        mock_session = AsyncMock()
        mock_ctx = _make_async_session_ctx(mock_session)

        with patch("app.api.voice.verify_twilio_signature", new_callable=AsyncMock) as mock_sig:
            mock_sig.return_value = {
                "CallSid": "CA_test_amd",
                "AnsweredBy": "human",
            }
            with patch("app.api.voice.async_session_factory", return_value=mock_ctx):
                with patch("app.api.voice.CallLogService", return_value=mock_svc):
                    with patch("app.api.voice._handle_missed_call_retry", new_callable=AsyncMock) as mock_retry:
                        request = MagicMock()
                        resp = await voice_amd_callback(request)
                        assert resp.status_code == 200
                        # answered_by persisted via raw SQL update
                        mock_session.exec.assert_called()
                        mock_session.commit.assert_called()
                        # No retry triggered
                        mock_retry.assert_not_called()

    async def test_unknown_persists_answered_by_no_hangup(self):
        """unknown AnsweredBy should persist to DB but NOT hang up or trigger retry."""
        from app.api.voice import voice_amd_callback

        mock_cl = _mock_call_log()
        mock_svc = AsyncMock()
        mock_svc.find_by_twilio_sid.return_value = mock_cl

        mock_session = AsyncMock()
        mock_ctx = _make_async_session_ctx(mock_session)

        with patch("app.api.voice.verify_twilio_signature", new_callable=AsyncMock) as mock_sig:
            mock_sig.return_value = {
                "CallSid": "CA_test_amd",
                "AnsweredBy": "unknown",
            }
            with patch("app.api.voice.async_session_factory", return_value=mock_ctx):
                with patch("app.api.voice.CallLogService", return_value=mock_svc):
                    with patch("app.api.voice._handle_missed_call_retry", new_callable=AsyncMock) as mock_retry:
                        request = MagicMock()
                        resp = await voice_amd_callback(request)
                        assert resp.status_code == 200
                        mock_session.exec.assert_called()
                        mock_session.commit.assert_called()
                        mock_retry.assert_not_called()

    async def test_machine_start_hangs_up_and_triggers_retry(self):
        """machine_start should hang up, transition to missed, and trigger retry."""
        from app.api.voice import voice_amd_callback

        mock_cl = _mock_call_log(status=CallLogStatus.RINGING.value)
        updated_cl = _mock_call_log(status=CallLogStatus.MISSED.value)

        mock_svc = AsyncMock()
        mock_svc.find_by_twilio_sid.return_value = mock_cl
        mock_svc.update_status.return_value = updated_cl

        mock_session = AsyncMock()
        mock_ctx = _make_async_session_ctx(mock_session)

        mock_twilio_call = MagicMock()
        mock_twilio_client = MagicMock()
        mock_twilio_client.calls.return_value = mock_twilio_call

        with patch("app.api.voice.verify_twilio_signature", new_callable=AsyncMock) as mock_sig:
            mock_sig.return_value = {
                "CallSid": "CA_test_amd",
                "AnsweredBy": "machine_start",
                "MachineDetectionDuration": "2500",
            }
            with patch("app.api.voice.async_session_factory", return_value=mock_ctx):
                with patch("app.api.voice.CallLogService", return_value=mock_svc):
                    with patch("app.api.voice._handle_missed_call_retry", new_callable=AsyncMock) as mock_retry:
                        with patch("starlette.concurrency.run_in_threadpool", new_callable=AsyncMock) as mock_threadpool:
                            with patch("twilio.rest.Client", return_value=mock_twilio_client):
                                request = MagicMock()
                                resp = await voice_amd_callback(request)
                                assert resp.status_code == 200

                                # Twilio hang-up called
                                mock_threadpool.assert_called_once()

                                # Transition to missed
                                mock_svc.update_status.assert_called_once()
                                call_args = mock_svc.update_status.call_args
                                assert call_args[0][1] == CallLogStatus.MISSED

                                # Retry triggered with "busy" (retryable)
                                mock_retry.assert_called_once_with(updated_cl, "busy")

    async def test_fax_hangs_up_and_triggers_retry(self):
        """fax should hang up, transition to missed, and trigger retry."""
        from app.api.voice import voice_amd_callback

        mock_cl = _mock_call_log(status=CallLogStatus.RINGING.value)
        updated_cl = _mock_call_log(status=CallLogStatus.MISSED.value)

        mock_svc = AsyncMock()
        mock_svc.find_by_twilio_sid.return_value = mock_cl
        mock_svc.update_status.return_value = updated_cl

        mock_session = AsyncMock()
        mock_ctx = _make_async_session_ctx(mock_session)

        with patch("app.api.voice.verify_twilio_signature", new_callable=AsyncMock) as mock_sig:
            mock_sig.return_value = {
                "CallSid": "CA_test_amd",
                "AnsweredBy": "fax",
            }
            with patch("app.api.voice.async_session_factory", return_value=mock_ctx):
                with patch("app.api.voice.CallLogService", return_value=mock_svc):
                    with patch("app.api.voice._handle_missed_call_retry", new_callable=AsyncMock) as mock_retry:
                        with patch("starlette.concurrency.run_in_threadpool", new_callable=AsyncMock):
                            with patch("twilio.rest.Client", return_value=MagicMock()):
                                request = MagicMock()
                                resp = await voice_amd_callback(request)
                                assert resp.status_code == 200
                                mock_svc.update_status.assert_called_once()
                                mock_retry.assert_called_once_with(updated_cl, "busy")

    async def test_terminal_state_skips_missed_transition(self):
        """If CallLog is already in a terminal state, skip the missed transition."""
        from app.api.voice import voice_amd_callback

        mock_cl = _mock_call_log(status=CallLogStatus.COMPLETED.value)
        mock_svc = AsyncMock()
        mock_svc.find_by_twilio_sid.return_value = mock_cl

        mock_session = AsyncMock()
        mock_ctx = _make_async_session_ctx(mock_session)

        with patch("app.api.voice.verify_twilio_signature", new_callable=AsyncMock) as mock_sig:
            mock_sig.return_value = {
                "CallSid": "CA_test_amd",
                "AnsweredBy": "machine_start",
            }
            with patch("app.api.voice.async_session_factory", return_value=mock_ctx):
                with patch("app.api.voice.CallLogService", return_value=mock_svc):
                    with patch("app.api.voice._handle_missed_call_retry", new_callable=AsyncMock) as mock_retry:
                        with patch("starlette.concurrency.run_in_threadpool", new_callable=AsyncMock):
                            with patch("twilio.rest.Client", return_value=MagicMock()):
                                request = MagicMock()
                                resp = await voice_amd_callback(request)
                                assert resp.status_code == 200
                                # update_status should NOT be called
                                mock_svc.update_status.assert_not_called()
                                # retry should NOT be triggered
                                mock_retry.assert_not_called()

    async def test_machine_end_beep_triggers_hangup(self):
        """machine_end_beep should also trigger hang-up and retry."""
        from app.api.voice import voice_amd_callback

        mock_cl = _mock_call_log(status=CallLogStatus.IN_PROGRESS.value)
        updated_cl = _mock_call_log(status=CallLogStatus.MISSED.value)

        mock_svc = AsyncMock()
        mock_svc.find_by_twilio_sid.return_value = mock_cl
        mock_svc.update_status.return_value = updated_cl

        mock_session = AsyncMock()
        mock_ctx = _make_async_session_ctx(mock_session)

        with patch("app.api.voice.verify_twilio_signature", new_callable=AsyncMock) as mock_sig:
            mock_sig.return_value = {
                "CallSid": "CA_test_amd",
                "AnsweredBy": "machine_end_beep",
            }
            with patch("app.api.voice.async_session_factory", return_value=mock_ctx):
                with patch("app.api.voice.CallLogService", return_value=mock_svc):
                    with patch("app.api.voice._handle_missed_call_retry", new_callable=AsyncMock) as mock_retry:
                        with patch("starlette.concurrency.run_in_threadpool", new_callable=AsyncMock):
                            with patch("twilio.rest.Client", return_value=MagicMock()):
                                request = MagicMock()
                                resp = await voice_amd_callback(request)
                                assert resp.status_code == 200
                                mock_retry.assert_called_once()

    async def test_hangup_failure_still_transitions_to_missed(self):
        """If Twilio hang-up fails, still transition to missed and retry."""
        from app.api.voice import voice_amd_callback

        mock_cl = _mock_call_log(status=CallLogStatus.RINGING.value)
        updated_cl = _mock_call_log(status=CallLogStatus.MISSED.value)

        mock_svc = AsyncMock()
        mock_svc.find_by_twilio_sid.return_value = mock_cl
        mock_svc.update_status.return_value = updated_cl

        mock_session = AsyncMock()
        mock_ctx = _make_async_session_ctx(mock_session)

        with patch("app.api.voice.verify_twilio_signature", new_callable=AsyncMock) as mock_sig:
            mock_sig.return_value = {
                "CallSid": "CA_test_amd",
                "AnsweredBy": "machine_start",
            }
            with patch("app.api.voice.async_session_factory", return_value=mock_ctx):
                with patch("app.api.voice.CallLogService", return_value=mock_svc):
                    with patch("app.api.voice._handle_missed_call_retry", new_callable=AsyncMock) as mock_retry:
                        with patch(
                            "starlette.concurrency.run_in_threadpool",
                            new_callable=AsyncMock,
                            side_effect=Exception("Twilio API error"),
                        ):
                            with patch("twilio.rest.Client", return_value=MagicMock()):
                                request = MagicMock()
                                resp = await voice_amd_callback(request)
                                assert resp.status_code == 200
                                # Still transitions to missed
                                mock_svc.update_status.assert_called_once()
                                # Still triggers retry
                                mock_retry.assert_called_once()
