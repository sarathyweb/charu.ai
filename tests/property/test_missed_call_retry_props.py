"""Property tests for missed call retry logic.

**Validates: Requirements 6.1, 6.3**

Property 11: Missed call triggers retry or WhatsApp based on attempt count
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from app.models.enums import CallLogStatus, OccurrenceKind
from app.services.scheduling_helpers import MAX_RETRIES, RETRY_DELAY_SECONDS


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

#: Twilio statuses that are retryable (busy, no-answer)
RETRYABLE_STATUSES = ["busy", "no-answer"]

#: Twilio status that is NOT retryable
NON_RETRYABLE_STATUS = "failed"


def _mock_call_log(
    *,
    id: int = 1,
    status: str = CallLogStatus.MISSED.value,
    version: int = 1,
    attempt_number: int = 1,
    user_id: int = 42,
    call_type: str = "morning",
    origin_window_id: int | None = None,
    root_call_log_id: int | None = None,
) -> MagicMock:
    """Build a mock CallLog with the given attributes."""
    cl = MagicMock()
    cl.id = id
    cl.status = status
    cl.version = version
    cl.attempt_number = attempt_number
    cl.user_id = user_id
    cl.call_type = call_type
    cl.origin_window_id = origin_window_id
    cl.root_call_log_id = root_call_log_id
    cl.call_date = datetime.now(timezone.utc).date()
    cl.scheduled_timezone = "America/New_York"
    cl.scheduled_time = datetime.now(timezone.utc)
    return cl


def _make_async_session_ctx(mock_session):
    """Build an async context manager that yields mock_session."""
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    return mock_ctx


# =========================================================================
# Property 11: Missed call triggers retry or WhatsApp based on attempt count
# =========================================================================


class TestProperty11MissedCallRetryLogic:
    """**Validates: Requirements 6.1, 6.3**

    For any missed call:
    - If attempt_number ≤ MAX_RETRIES and status is retryable (busy/no-answer):
      a new CallLog is created with incremented attempt_number,
      occurrence_kind='retry', and root_call_log_id pointing to the original.
    - If attempt_number > MAX_RETRIES: WhatsApp missed_call_encouragement
      is sent via OutboundMessage dedup flow.
    - If twilio_status is 'failed': no retry is scheduled (only WhatsApp
      if exhausted).
    - Retry timing: scheduled_time = now + RETRY_DELAY_SECONDS.
    """

    @pytest.mark.anyio
    @given(
        attempt_number=st.integers(min_value=1, max_value=MAX_RETRIES),
        twilio_status=st.sampled_from(RETRYABLE_STATUSES),
    )
    @settings(max_examples=30, deadline=None)
    async def test_retryable_within_budget_creates_retry_call_log(
        self, attempt_number: int, twilio_status: str
    ):
        """For attempt_number ≤ MAX_RETRIES with a retryable status,
        a new CallLog is created with incremented attempt_number,
        occurrence_kind='retry', and correct root_call_log_id."""
        from app.api.voice import _handle_missed_call_retry

        mock_cl = _mock_call_log(
            attempt_number=attempt_number,
            origin_window_id=None,  # skip window check
        )

        mock_session = AsyncMock()
        mock_retry_log = MagicMock()
        mock_retry_log.id = 100
        mock_retry_log.attempt_number = attempt_number + 1

        # Track what gets added to the session
        added_objects = []
        mock_session.add = lambda obj: added_objects.append(obj)
        mock_session.commit = AsyncMock()
        mock_session.refresh = AsyncMock()

        mock_ctx = _make_async_session_ctx(mock_session)

        with patch("app.api.voice.async_session_factory", return_value=mock_ctx):
            await _handle_missed_call_retry(mock_cl, twilio_status)

        # A new CallLog should have been added
        assert len(added_objects) == 1
        retry_log = added_objects[0]

        # Verify retry properties
        assert retry_log.attempt_number == attempt_number + 1
        assert retry_log.occurrence_kind == OccurrenceKind.RETRY.value
        assert retry_log.status == CallLogStatus.SCHEDULED.value
        assert retry_log.user_id == mock_cl.user_id
        assert retry_log.call_type == mock_cl.call_type
        assert retry_log.root_call_log_id == (mock_cl.root_call_log_id or mock_cl.id)

    @pytest.mark.anyio
    @given(
        attempt_number=st.integers(min_value=1, max_value=MAX_RETRIES),
        twilio_status=st.sampled_from(RETRYABLE_STATUSES),
    )
    @settings(max_examples=20, deadline=None)
    async def test_retry_scheduled_time_uses_delay(
        self, attempt_number: int, twilio_status: str
    ):
        """Retry scheduled_time should be approximately now + RETRY_DELAY_SECONDS."""
        from app.api.voice import _handle_missed_call_retry

        mock_cl = _mock_call_log(
            attempt_number=attempt_number,
            origin_window_id=None,
        )

        added_objects = []
        mock_session = AsyncMock()
        mock_session.add = lambda obj: added_objects.append(obj)
        mock_session.commit = AsyncMock()
        mock_session.refresh = AsyncMock()

        mock_ctx = _make_async_session_ctx(mock_session)

        before = datetime.now(timezone.utc)
        with patch("app.api.voice.async_session_factory", return_value=mock_ctx):
            await _handle_missed_call_retry(mock_cl, twilio_status)
        after = datetime.now(timezone.utc)

        assert len(added_objects) == 1
        retry_log = added_objects[0]

        expected_min = before + timedelta(seconds=RETRY_DELAY_SECONDS)
        expected_max = after + timedelta(seconds=RETRY_DELAY_SECONDS)
        assert expected_min <= retry_log.scheduled_time <= expected_max

    @pytest.mark.anyio
    @given(
        attempt_number=st.integers(min_value=MAX_RETRIES + 1, max_value=MAX_RETRIES + 10),
        twilio_status=st.sampled_from(RETRYABLE_STATUSES),
    )
    @settings(max_examples=20, deadline=None)
    async def test_exhausted_retries_sends_whatsapp(
        self, attempt_number: int, twilio_status: str
    ):
        """When attempt_number > MAX_RETRIES with a retryable status,
        WhatsApp missed_call_encouragement is sent instead of retry."""
        from app.api.voice import _handle_missed_call_retry

        mock_cl = _mock_call_log(attempt_number=attempt_number)

        with patch(
            "app.api.voice._send_missed_encouragement",
            new_callable=AsyncMock,
        ) as mock_send:
            await _handle_missed_call_retry(mock_cl, twilio_status)
            mock_send.assert_called_once_with(mock_cl)

    @pytest.mark.anyio
    @given(
        attempt_number=st.integers(min_value=1, max_value=MAX_RETRIES + 10),
    )
    @settings(max_examples=30, deadline=None)
    async def test_failed_status_never_retries(self, attempt_number: int):
        """'failed' twilio_status should NEVER trigger a retry, regardless
        of attempt_number. It should only send WhatsApp if exhausted."""
        from app.api.voice import _handle_missed_call_retry

        mock_cl = _mock_call_log(attempt_number=attempt_number)

        mock_session = AsyncMock()
        added_objects = []
        mock_session.add = lambda obj: added_objects.append(obj)
        mock_session.commit = AsyncMock()
        mock_session.refresh = AsyncMock()
        mock_ctx = _make_async_session_ctx(mock_session)

        with patch("app.api.voice.async_session_factory", return_value=mock_ctx):
            with patch(
                "app.api.voice._send_missed_encouragement_if_exhausted",
                new_callable=AsyncMock,
            ) as mock_send_if_exhausted:
                await _handle_missed_call_retry(mock_cl, NON_RETRYABLE_STATUS)

                # No retry CallLog should be created
                assert len(added_objects) == 0
                # _send_missed_encouragement_if_exhausted is called
                mock_send_if_exhausted.assert_called_once_with(mock_cl)

    @pytest.mark.anyio
    async def test_boundary_attempt_equals_max_retries_creates_retry(self):
        """Boundary: attempt_number == MAX_RETRIES should still create a retry
        (since attempt_number ≤ MAX_RETRIES means retries remain)."""
        from app.api.voice import _handle_missed_call_retry

        mock_cl = _mock_call_log(
            attempt_number=MAX_RETRIES,
            origin_window_id=None,
        )

        added_objects = []
        mock_session = AsyncMock()
        mock_session.add = lambda obj: added_objects.append(obj)
        mock_session.commit = AsyncMock()
        mock_session.refresh = AsyncMock()
        mock_ctx = _make_async_session_ctx(mock_session)

        with patch("app.api.voice.async_session_factory", return_value=mock_ctx):
            await _handle_missed_call_retry(mock_cl, "no-answer")

        assert len(added_objects) == 1
        retry_log = added_objects[0]
        assert retry_log.attempt_number == MAX_RETRIES + 1
        assert retry_log.occurrence_kind == OccurrenceKind.RETRY.value

    @pytest.mark.anyio
    async def test_boundary_attempt_exceeds_max_retries_sends_whatsapp(self):
        """Boundary: attempt_number == MAX_RETRIES + 1 should send WhatsApp
        instead of creating a retry."""
        from app.api.voice import _handle_missed_call_retry

        mock_cl = _mock_call_log(attempt_number=MAX_RETRIES + 1)

        with patch(
            "app.api.voice._send_missed_encouragement",
            new_callable=AsyncMock,
        ) as mock_send:
            await _handle_missed_call_retry(mock_cl, "busy")
            mock_send.assert_called_once_with(mock_cl)

    @pytest.mark.anyio
    @given(
        attempt_number=st.integers(min_value=1, max_value=MAX_RETRIES),
        twilio_status=st.sampled_from(RETRYABLE_STATUSES),
    )
    @settings(max_examples=20, deadline=None)
    async def test_retry_with_window_check(
        self, attempt_number: int, twilio_status: str
    ):
        """When origin_window_id is set and retry fits in window,
        a retry is still created."""
        from app.api.voice import _handle_missed_call_retry

        mock_cl = _mock_call_log(
            attempt_number=attempt_number,
            origin_window_id=10,
        )

        added_objects = []
        mock_session = AsyncMock()
        mock_session.add = lambda obj: added_objects.append(obj)
        mock_session.commit = AsyncMock()
        mock_session.refresh = AsyncMock()
        mock_ctx = _make_async_session_ctx(mock_session)

        with patch("app.api.voice.async_session_factory", return_value=mock_ctx):
            with patch(
                "app.api.voice._retry_fits_in_window",
                new_callable=AsyncMock,
                return_value=True,
            ):
                await _handle_missed_call_retry(mock_cl, twilio_status)

        assert len(added_objects) == 1
        assert added_objects[0].attempt_number == attempt_number + 1

    @pytest.mark.anyio
    @given(
        attempt_number=st.integers(min_value=1, max_value=MAX_RETRIES),
        twilio_status=st.sampled_from(RETRYABLE_STATUSES),
    )
    @settings(max_examples=20, deadline=None)
    async def test_retry_outside_window_sends_whatsapp(
        self, attempt_number: int, twilio_status: str
    ):
        """When origin_window_id is set but retry does NOT fit in window,
        WhatsApp encouragement is sent instead of retry."""
        from app.api.voice import _handle_missed_call_retry

        mock_cl = _mock_call_log(
            attempt_number=attempt_number,
            origin_window_id=10,
        )

        with patch(
            "app.api.voice._retry_fits_in_window",
            new_callable=AsyncMock,
            return_value=False,
        ):
            with patch(
                "app.api.voice._send_missed_encouragement",
                new_callable=AsyncMock,
            ) as mock_send:
                await _handle_missed_call_retry(mock_cl, twilio_status)
                mock_send.assert_called_once_with(mock_cl)

    @pytest.mark.anyio
    @given(
        attempt_number=st.integers(min_value=MAX_RETRIES + 1, max_value=MAX_RETRIES + 5),
    )
    @settings(max_examples=10, deadline=None)
    async def test_failed_exhausted_sends_whatsapp(self, attempt_number: int):
        """'failed' status with exhausted retries should still send WhatsApp
        via _send_missed_encouragement_if_exhausted."""
        from app.api.voice import _handle_missed_call_retry

        mock_cl = _mock_call_log(attempt_number=attempt_number)

        with patch(
            "app.api.voice._send_missed_encouragement_if_exhausted",
            new_callable=AsyncMock,
        ) as mock_send:
            await _handle_missed_call_retry(mock_cl, NON_RETRYABLE_STATUS)
            mock_send.assert_called_once_with(mock_cl)
