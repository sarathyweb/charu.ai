"""Unit tests for POST /voice/status-callback (task 15.1).

Tests:
- Twilio status → internal status mapping
- SequenceNumber ordering logic
- Retryable vs non-retryable missed statuses
- Endpoint always returns 200
- Stale sequence number discarded
- Unknown CallSid handled gracefully
- Missed call retry logic (mocked)
- Failed status does NOT retry

Requirements: 6, 22
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.enums import CallLogStatus


# ---------------------------------------------------------------------------
# Tests — Twilio status mapping (pure, no DB)
# ---------------------------------------------------------------------------


class TestTwilioStatusMapping:
    """Verify the TWILIO_STATUS_MAP covers all expected Twilio statuses."""

    def test_ringing_maps_to_ringing(self):
        from app.api.voice import TWILIO_STATUS_MAP
        assert TWILIO_STATUS_MAP["ringing"] == CallLogStatus.RINGING

    def test_in_progress_maps_to_in_progress(self):
        from app.api.voice import TWILIO_STATUS_MAP
        assert TWILIO_STATUS_MAP["in-progress"] == CallLogStatus.IN_PROGRESS

    def test_completed_maps_to_completed(self):
        from app.api.voice import TWILIO_STATUS_MAP
        assert TWILIO_STATUS_MAP["completed"] == CallLogStatus.COMPLETED

    def test_busy_maps_to_missed(self):
        from app.api.voice import TWILIO_STATUS_MAP
        assert TWILIO_STATUS_MAP["busy"] == CallLogStatus.MISSED

    def test_no_answer_maps_to_missed(self):
        from app.api.voice import TWILIO_STATUS_MAP
        assert TWILIO_STATUS_MAP["no-answer"] == CallLogStatus.MISSED

    def test_failed_maps_to_missed(self):
        from app.api.voice import TWILIO_STATUS_MAP
        assert TWILIO_STATUS_MAP["failed"] == CallLogStatus.MISSED

    def test_canceled_maps_to_cancelled(self):
        from app.api.voice import TWILIO_STATUS_MAP
        assert TWILIO_STATUS_MAP["canceled"] == CallLogStatus.CANCELLED

    def test_queued_is_noop(self):
        from app.api.voice import TWILIO_STATUS_MAP
        assert TWILIO_STATUS_MAP["queued"] is None

    def test_initiated_is_noop(self):
        from app.api.voice import TWILIO_STATUS_MAP
        assert TWILIO_STATUS_MAP["initiated"] is None


class TestRetryableStatuses:
    """Verify which missed statuses trigger retries."""

    def test_busy_is_retryable(self):
        from app.api.voice import _RETRYABLE_MISSED_STATUSES
        assert "busy" in _RETRYABLE_MISSED_STATUSES

    def test_no_answer_is_retryable(self):
        from app.api.voice import _RETRYABLE_MISSED_STATUSES
        assert "no-answer" in _RETRYABLE_MISSED_STATUSES

    def test_failed_is_not_retryable(self):
        from app.api.voice import _RETRYABLE_MISSED_STATUSES
        assert "failed" not in _RETRYABLE_MISSED_STATUSES


# ---------------------------------------------------------------------------
# Tests — Endpoint handler (mocked DB)
# ---------------------------------------------------------------------------


def _mock_call_log(
    *,
    id: int = 1,
    status: str = CallLogStatus.RINGING.value,
    version: int = 1,
    last_twilio_sequence_number: int | None = None,
    twilio_call_sid: str = "CA_test",
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
    cl.last_twilio_sequence_number = last_twilio_sequence_number
    cl.twilio_call_sid = twilio_call_sid
    cl.attempt_number = attempt_number
    cl.user_id = user_id
    cl.call_type = call_type
    cl.origin_window_id = origin_window_id
    cl.root_call_log_id = root_call_log_id
    cl.call_date = datetime.now(timezone.utc).date()
    cl.scheduled_timezone = "America/New_York"
    return cl


@pytest.mark.anyio
class TestStatusCallbackEndpoint:
    """Test the HTTP endpoint behavior with mocked internals."""

    async def test_always_returns_200_on_signature_failure(self):
        """Even if Twilio signature fails, return 200."""
        from app.api.voice import voice_status_callback

        with patch(
            "app.api.voice.verify_twilio_signature",
            new_callable=AsyncMock,
            side_effect=Exception("bad sig"),
        ):
            request = MagicMock()
            resp = await voice_status_callback(request)
            assert resp.status_code == 200

    async def test_returns_200_on_empty_call_sid(self):
        """Missing CallSid should return 200."""
        from app.api.voice import voice_status_callback

        with patch("app.api.voice.verify_twilio_signature", new_callable=AsyncMock) as mock_sig:
            mock_sig.return_value = {"CallSid": "", "CallStatus": "completed"}
            request = MagicMock()
            resp = await voice_status_callback(request)
            assert resp.status_code == 200

    async def test_returns_200_on_unknown_call_sid(self):
        """Unknown CallSid should return 200."""
        from app.api.voice import voice_status_callback

        mock_svc = AsyncMock()
        mock_svc.find_by_twilio_sid.return_value = None

        mock_session = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("app.api.voice.verify_twilio_signature", new_callable=AsyncMock) as mock_sig:
            mock_sig.return_value = {
                "CallSid": "CA_unknown",
                "CallStatus": "completed",
                "SequenceNumber": "1",
            }
            with patch("app.api.voice.async_session_factory", return_value=mock_ctx):
                with patch("app.api.voice.CallLogService", return_value=mock_svc):
                    request = MagicMock()
                    resp = await voice_status_callback(request)
                    assert resp.status_code == 200

    async def test_stale_seq_discarded(self):
        """Stale SequenceNumber should be discarded (return 200, no update)."""
        from app.api.voice import voice_status_callback

        mock_call_log = _mock_call_log(last_twilio_sequence_number=5)
        mock_svc = AsyncMock()
        mock_svc.find_by_twilio_sid.return_value = mock_call_log

        mock_session = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("app.api.voice.verify_twilio_signature", new_callable=AsyncMock) as mock_sig:
            mock_sig.return_value = {
                "CallSid": "CA_test",
                "CallStatus": "completed",
                "SequenceNumber": "3",  # ≤ 5 → stale
            }
            with patch("app.api.voice.async_session_factory", return_value=mock_ctx):
                with patch("app.api.voice.CallLogService", return_value=mock_svc):
                    request = MagicMock()
                    resp = await voice_status_callback(request)
                    assert resp.status_code == 200
                    # update_status should NOT have been called
                    mock_svc.update_status.assert_not_called()

    async def test_completed_calls_update_with_duration(self):
        """completed callback should call update_status with end_time and duration."""
        from app.api.voice import voice_status_callback

        mock_cl = _mock_call_log(status=CallLogStatus.IN_PROGRESS.value)
        updated_cl = _mock_call_log(status=CallLogStatus.COMPLETED.value)

        mock_svc = AsyncMock()
        mock_svc.find_by_twilio_sid.return_value = mock_cl
        mock_svc.update_status.return_value = updated_cl

        mock_session = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("app.api.voice.verify_twilio_signature", new_callable=AsyncMock) as mock_sig:
            mock_sig.return_value = {
                "CallSid": "CA_test",
                "CallStatus": "completed",
                "SequenceNumber": "4",
                "CallDuration": "120",
            }
            with patch("app.api.voice.async_session_factory", return_value=mock_ctx):
                with patch("app.api.voice.CallLogService", return_value=mock_svc):
                    request = MagicMock()
                    resp = await voice_status_callback(request)
                    assert resp.status_code == 200

                    # Verify update_status was called with correct args
                    mock_svc.update_status.assert_called_once()
                    call_args = mock_svc.update_status.call_args
                    assert call_args[0][1] == CallLogStatus.COMPLETED
                    assert call_args[1]["duration_seconds"] == 120
                    assert "end_time" in call_args[1]
                    assert call_args[1]["last_twilio_sequence_number"] == 4

    async def test_in_progress_sets_actual_start_time(self):
        """in-progress callback should pass actual_start_time to update_status."""
        from app.api.voice import voice_status_callback

        mock_cl = _mock_call_log(status=CallLogStatus.RINGING.value)
        updated_cl = _mock_call_log(status=CallLogStatus.IN_PROGRESS.value)

        mock_svc = AsyncMock()
        mock_svc.find_by_twilio_sid.return_value = mock_cl
        mock_svc.update_status.return_value = updated_cl

        mock_session = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("app.api.voice.verify_twilio_signature", new_callable=AsyncMock) as mock_sig:
            mock_sig.return_value = {
                "CallSid": "CA_test",
                "CallStatus": "in-progress",
                "SequenceNumber": "2",
            }
            with patch("app.api.voice.async_session_factory", return_value=mock_ctx):
                with patch("app.api.voice.CallLogService", return_value=mock_svc):
                    request = MagicMock()
                    resp = await voice_status_callback(request)
                    assert resp.status_code == 200

                    call_args = mock_svc.update_status.call_args
                    assert call_args[0][1] == CallLogStatus.IN_PROGRESS
                    assert "actual_start_time" in call_args[1]

    async def test_missed_triggers_retry_handler(self):
        """busy/no-answer should trigger _handle_missed_call_retry."""
        from app.api.voice import voice_status_callback

        mock_cl = _mock_call_log(status=CallLogStatus.RINGING.value)
        updated_cl = _mock_call_log(status=CallLogStatus.MISSED.value)

        mock_svc = AsyncMock()
        mock_svc.find_by_twilio_sid.return_value = mock_cl
        mock_svc.update_status.return_value = updated_cl

        mock_session = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("app.api.voice.verify_twilio_signature", new_callable=AsyncMock) as mock_sig:
            mock_sig.return_value = {
                "CallSid": "CA_test",
                "CallStatus": "busy",
                "SequenceNumber": "2",
            }
            with patch("app.api.voice.async_session_factory", return_value=mock_ctx):
                with patch("app.api.voice.CallLogService", return_value=mock_svc):
                    with patch("app.api.voice._handle_missed_call_retry", new_callable=AsyncMock) as mock_retry:
                        request = MagicMock()
                        resp = await voice_status_callback(request)
                        assert resp.status_code == 200
                        mock_retry.assert_called_once_with(updated_cl, "busy")

    async def test_failed_triggers_retry_handler_but_not_retryable(self):
        """failed should trigger _handle_missed_call_retry which won't retry."""
        from app.api.voice import voice_status_callback

        mock_cl = _mock_call_log(status=CallLogStatus.RINGING.value)
        updated_cl = _mock_call_log(status=CallLogStatus.MISSED.value)

        mock_svc = AsyncMock()
        mock_svc.find_by_twilio_sid.return_value = mock_cl
        mock_svc.update_status.return_value = updated_cl

        mock_session = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("app.api.voice.verify_twilio_signature", new_callable=AsyncMock) as mock_sig:
            mock_sig.return_value = {
                "CallSid": "CA_test",
                "CallStatus": "failed",
                "SequenceNumber": "2",
            }
            with patch("app.api.voice.async_session_factory", return_value=mock_ctx):
                with patch("app.api.voice.CallLogService", return_value=mock_svc):
                    with patch("app.api.voice._handle_missed_call_retry", new_callable=AsyncMock) as mock_retry:
                        request = MagicMock()
                        resp = await voice_status_callback(request)
                        assert resp.status_code == 200
                        mock_retry.assert_called_once_with(updated_cl, "failed")

    async def test_invalid_transition_returns_200(self):
        """Invalid transition should return 200 without crashing."""
        from app.api.voice import voice_status_callback
        from app.services.call_log_service import InvalidTransitionError

        mock_cl = _mock_call_log(status=CallLogStatus.COMPLETED.value)

        mock_svc = AsyncMock()
        mock_svc.find_by_twilio_sid.return_value = mock_cl
        mock_svc.update_status.side_effect = InvalidTransitionError("bad transition")

        mock_session = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("app.api.voice.verify_twilio_signature", new_callable=AsyncMock) as mock_sig:
            mock_sig.return_value = {
                "CallSid": "CA_test",
                "CallStatus": "ringing",
                "SequenceNumber": "5",
            }
            with patch("app.api.voice.async_session_factory", return_value=mock_ctx):
                with patch("app.api.voice.CallLogService", return_value=mock_svc):
                    request = MagicMock()
                    resp = await voice_status_callback(request)
                    assert resp.status_code == 200

    async def test_queued_no_state_change(self):
        """queued callback should not call update_status."""
        from app.api.voice import voice_status_callback

        mock_cl = _mock_call_log(status=CallLogStatus.RINGING.value)

        mock_svc = AsyncMock()
        mock_svc.find_by_twilio_sid.return_value = mock_cl

        mock_session = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("app.api.voice.verify_twilio_signature", new_callable=AsyncMock) as mock_sig:
            mock_sig.return_value = {
                "CallSid": "CA_test",
                "CallStatus": "queued",
                "SequenceNumber": "0",
            }
            with patch("app.api.voice.async_session_factory", return_value=mock_ctx):
                with patch("app.api.voice.CallLogService", return_value=mock_svc):
                    request = MagicMock()
                    resp = await voice_status_callback(request)
                    assert resp.status_code == 200
                    mock_svc.update_status.assert_not_called()

    async def test_canceled_maps_to_cancelled_status(self):
        """Twilio 'canceled' should map to internal CANCELLED."""
        from app.api.voice import voice_status_callback

        mock_cl = _mock_call_log(status=CallLogStatus.RINGING.value)
        updated_cl = _mock_call_log(status=CallLogStatus.CANCELLED.value)

        mock_svc = AsyncMock()
        mock_svc.find_by_twilio_sid.return_value = mock_cl
        mock_svc.update_status.return_value = updated_cl

        mock_session = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("app.api.voice.verify_twilio_signature", new_callable=AsyncMock) as mock_sig:
            mock_sig.return_value = {
                "CallSid": "CA_test",
                "CallStatus": "canceled",
                "SequenceNumber": "2",
            }
            with patch("app.api.voice.async_session_factory", return_value=mock_ctx):
                with patch("app.api.voice.CallLogService", return_value=mock_svc):
                    request = MagicMock()
                    resp = await voice_status_callback(request)
                    assert resp.status_code == 200

                    call_args = mock_svc.update_status.call_args
                    assert call_args[0][1] == CallLogStatus.CANCELLED
