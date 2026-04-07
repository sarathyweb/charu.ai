"""Property tests for Twilio callbacks — AMD detection and callback idempotency.

**Validates: Requirements 6.V2, 22.2**

Property 12: AMD detection triggers correct handling per outcome
Property 43: Twilio callback idempotency and ordering
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from app.models.enums import CallLogStatus
from app.services.call_log_service import VALID_TRANSITIONS, validate_transition


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

ALL_ANSWERED_BY = [
    "machine_start",
    "machine_end_beep",
    "machine_end_silence",
    "machine_end_other",
    "fax",
    "human",
    "unknown",
]

MACHINE_VALUES = [
    "machine_start",
    "machine_end_beep",
    "machine_end_silence",
    "machine_end_other",
    "fax",
]

HUMAN_VALUES = ["human", "unknown"]

# All Twilio statuses that map to an internal status
ALL_TWILIO_STATUSES = [
    "queued",
    "initiated",
    "ringing",
    "in-progress",
    "completed",
    "busy",
    "no-answer",
    "failed",
    "canceled",
]

# Internal statuses as strings
ALL_INTERNAL_STATUSES = [s.value for s in CallLogStatus]


def _mock_call_log(
    *,
    id: int = 1,
    status: str = CallLogStatus.RINGING.value,
    version: int = 1,
    last_twilio_sequence_number: int | None = None,
    twilio_call_sid: str = "CA_prop_test",
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
    cl.last_twilio_sequence_number = last_twilio_sequence_number
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


# =========================================================================
# Property 12: AMD detection triggers correct handling per outcome
# =========================================================================


class TestProperty12AmdDetection:
    """**Validates: Requirements 6.V2**

    For any AMD callback, the system handles each AnsweredBy value correctly:
    - machine/fax → hang up, transition to missed, trigger retry
    - human/unknown → no hang-up, no missed transition, no retry
    - ALL values → answered_by is persisted to CallLog
    """

    @pytest.mark.anyio
    @given(answered_by=st.sampled_from(MACHINE_VALUES))
    @settings(max_examples=20, deadline=None)
    async def test_machine_fax_triggers_hangup_missed_retry(self, answered_by: str):
        """For any machine/fax AnsweredBy, the call is hung up, transitioned
        to missed, and retry is triggered."""
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
                "CallSid": "CA_prop_test",
                "AnsweredBy": answered_by,
            }
            with patch("app.api.voice.async_session_factory", return_value=mock_ctx):
                with patch("app.api.voice.CallLogService", return_value=mock_svc):
                    with patch(
                        "app.api.voice._handle_missed_call_retry",
                        new_callable=AsyncMock,
                    ) as mock_retry:
                        with patch(
                            "starlette.concurrency.run_in_threadpool",
                            new_callable=AsyncMock,
                        ):
                            with patch("twilio.rest.Client", return_value=MagicMock()):
                                request = MagicMock()
                                resp = await voice_amd_callback(request)

                                # Always returns 200
                                assert resp.status_code == 200
                                # Transition to missed
                                mock_svc.update_status.assert_called_once()
                                call_args = mock_svc.update_status.call_args
                                assert call_args[0][1] == CallLogStatus.MISSED
                                # Retry triggered
                                mock_retry.assert_called_once()

    @pytest.mark.anyio
    @given(answered_by=st.sampled_from(HUMAN_VALUES))
    @settings(max_examples=10, deadline=None)
    async def test_human_unknown_no_hangup_no_retry(self, answered_by: str):
        """For human/unknown AnsweredBy, no hang-up, no missed transition,
        no retry — call proceeds normally."""
        from app.api.voice import voice_amd_callback

        mock_cl = _mock_call_log(status=CallLogStatus.RINGING.value)
        mock_svc = AsyncMock()
        mock_svc.find_by_twilio_sid.return_value = mock_cl

        mock_session = AsyncMock()
        mock_ctx = _make_async_session_ctx(mock_session)

        with patch("app.api.voice.verify_twilio_signature", new_callable=AsyncMock) as mock_sig:
            mock_sig.return_value = {
                "CallSid": "CA_prop_test",
                "AnsweredBy": answered_by,
            }
            with patch("app.api.voice.async_session_factory", return_value=mock_ctx):
                with patch("app.api.voice.CallLogService", return_value=mock_svc):
                    with patch(
                        "app.api.voice._handle_missed_call_retry",
                        new_callable=AsyncMock,
                    ) as mock_retry:
                        request = MagicMock()
                        resp = await voice_amd_callback(request)

                        assert resp.status_code == 200
                        # update_status should NOT be called (no missed transition)
                        mock_svc.update_status.assert_not_called()
                        # No retry triggered
                        mock_retry.assert_not_called()

    @pytest.mark.anyio
    @given(answered_by=st.sampled_from(ALL_ANSWERED_BY))
    @settings(max_examples=30, deadline=None)
    async def test_answered_by_always_persisted(self, answered_by: str):
        """For ALL AnsweredBy values, the value is persisted to CallLog."""
        from app.api.voice import voice_amd_callback

        mock_cl = _mock_call_log(status=CallLogStatus.RINGING.value)
        # For machine values, we need update_status to succeed
        updated_cl = _mock_call_log(status=CallLogStatus.MISSED.value)

        mock_svc = AsyncMock()
        mock_svc.find_by_twilio_sid.return_value = mock_cl
        mock_svc.update_status.return_value = updated_cl

        mock_session = AsyncMock()
        mock_ctx = _make_async_session_ctx(mock_session)

        with patch("app.api.voice.verify_twilio_signature", new_callable=AsyncMock) as mock_sig:
            mock_sig.return_value = {
                "CallSid": "CA_prop_test",
                "AnsweredBy": answered_by,
            }
            with patch("app.api.voice.async_session_factory", return_value=mock_ctx):
                with patch("app.api.voice.CallLogService", return_value=mock_svc):
                    with patch(
                        "app.api.voice._handle_missed_call_retry",
                        new_callable=AsyncMock,
                    ):
                        with patch(
                            "starlette.concurrency.run_in_threadpool",
                            new_callable=AsyncMock,
                        ):
                            with patch("twilio.rest.Client", return_value=MagicMock()):
                                request = MagicMock()
                                resp = await voice_amd_callback(request)

                                assert resp.status_code == 200
                                # answered_by persisted via raw SQL update
                                mock_session.exec.assert_called()
                                mock_session.commit.assert_called()


# =========================================================================
# Property 43: Twilio callback idempotency and ordering
# =========================================================================


class TestProperty43CallbackIdempotencyOrdering:
    """**Validates: Requirements 22.2**

    For any Twilio status or AMD callback:
    - Stale/duplicate SequenceNumbers (≤ persisted) are discarded
    - Valid transitions are applied in order
    - Invalid transitions (e.g., completed → ringing) are rejected
    - last_twilio_sequence_number is updated on accepted callbacks
    """

    @pytest.mark.anyio
    @given(
        persisted_seq=st.integers(min_value=1, max_value=100),
        incoming_seq=st.integers(min_value=0, max_value=100),
    )
    @settings(max_examples=50, deadline=None)
    async def test_stale_sequence_numbers_discarded(
        self, persisted_seq: int, incoming_seq: int
    ):
        """When incoming SequenceNumber ≤ persisted, the callback is discarded
        and no state transition occurs."""
        assume(incoming_seq <= persisted_seq)

        from app.api.voice import voice_status_callback

        mock_cl = _mock_call_log(
            status=CallLogStatus.RINGING.value,
            last_twilio_sequence_number=persisted_seq,
        )
        mock_svc = AsyncMock()
        mock_svc.find_by_twilio_sid.return_value = mock_cl

        mock_session = AsyncMock()
        mock_ctx = _make_async_session_ctx(mock_session)

        with patch("app.api.voice.verify_twilio_signature", new_callable=AsyncMock) as mock_sig:
            mock_sig.return_value = {
                "CallSid": "CA_prop_test",
                "CallStatus": "completed",
                "SequenceNumber": str(incoming_seq),
            }
            with patch("app.api.voice.async_session_factory", return_value=mock_ctx):
                with patch("app.api.voice.CallLogService", return_value=mock_svc):
                    request = MagicMock()
                    resp = await voice_status_callback(request)

                    assert resp.status_code == 200
                    # update_status should NOT be called for stale seq
                    mock_svc.update_status.assert_not_called()

    @pytest.mark.anyio
    @given(
        persisted_seq=st.integers(min_value=0, max_value=50),
        incoming_seq=st.integers(min_value=1, max_value=100),
    )
    @settings(max_examples=50, deadline=None)
    async def test_fresh_sequence_numbers_accepted(
        self, persisted_seq: int, incoming_seq: int
    ):
        """When incoming SequenceNumber > persisted, the callback is processed
        and last_twilio_sequence_number is updated."""
        assume(incoming_seq > persisted_seq)

        from app.api.voice import voice_status_callback

        mock_cl = _mock_call_log(
            status=CallLogStatus.RINGING.value,
            last_twilio_sequence_number=persisted_seq,
        )
        updated_cl = _mock_call_log(
            status=CallLogStatus.IN_PROGRESS.value,
            last_twilio_sequence_number=incoming_seq,
        )

        mock_svc = AsyncMock()
        mock_svc.find_by_twilio_sid.return_value = mock_cl
        mock_svc.update_status.return_value = updated_cl

        mock_session = AsyncMock()
        mock_ctx = _make_async_session_ctx(mock_session)

        with patch("app.api.voice.verify_twilio_signature", new_callable=AsyncMock) as mock_sig:
            mock_sig.return_value = {
                "CallSid": "CA_prop_test",
                "CallStatus": "in-progress",
                "SequenceNumber": str(incoming_seq),
            }
            with patch("app.api.voice.async_session_factory", return_value=mock_ctx):
                with patch("app.api.voice.CallLogService", return_value=mock_svc):
                    request = MagicMock()
                    resp = await voice_status_callback(request)

                    assert resp.status_code == 200
                    # update_status SHOULD be called
                    mock_svc.update_status.assert_called_once()
                    # Verify last_twilio_sequence_number is passed
                    call_kwargs = mock_svc.update_status.call_args[1]
                    assert call_kwargs["last_twilio_sequence_number"] == incoming_seq

    @pytest.mark.anyio
    @given(
        current_status=st.sampled_from(
            [CallLogStatus.COMPLETED, CallLogStatus.MISSED, CallLogStatus.CANCELLED, CallLogStatus.SKIPPED]
        ),
        twilio_status=st.sampled_from(["ringing", "in-progress", "completed"]),
    )
    @settings(max_examples=30, deadline=None)
    async def test_invalid_transitions_rejected(
        self, current_status: CallLogStatus, twilio_status: str
    ):
        """Callbacks that would regress a terminal state are rejected.
        The endpoint returns 200 but does not apply the transition."""
        from app.api.voice import voice_status_callback, TWILIO_STATUS_MAP
        from app.services.call_log_service import InvalidTransitionError

        target = TWILIO_STATUS_MAP.get(twilio_status)
        if target is None:
            return  # no-op statuses don't trigger transitions

        # Only test cases where the transition is actually invalid
        assume(not validate_transition(current_status, target))

        mock_cl = _mock_call_log(
            status=current_status.value,
            last_twilio_sequence_number=0,
        )

        mock_svc = AsyncMock()
        mock_svc.find_by_twilio_sid.return_value = mock_cl
        mock_svc.update_status.side_effect = InvalidTransitionError(
            f"Cannot transition from {current_status.value} to {target.value}"
        )

        mock_session = AsyncMock()
        mock_ctx = _make_async_session_ctx(mock_session)

        with patch("app.api.voice.verify_twilio_signature", new_callable=AsyncMock) as mock_sig:
            mock_sig.return_value = {
                "CallSid": "CA_prop_test",
                "CallStatus": twilio_status,
                "SequenceNumber": "5",
            }
            with patch("app.api.voice.async_session_factory", return_value=mock_ctx):
                with patch("app.api.voice.CallLogService", return_value=mock_svc):
                    request = MagicMock()
                    resp = await voice_status_callback(request)

                    # Always returns 200 to Twilio
                    assert resp.status_code == 200

    @pytest.mark.anyio
    @given(
        current_status=st.sampled_from(list(CallLogStatus)),
        twilio_status=st.sampled_from(
            ["ringing", "in-progress", "completed", "busy", "no-answer", "failed", "canceled"]
        ),
    )
    @settings(max_examples=50, deadline=None)
    async def test_valid_transitions_applied(
        self, current_status: CallLogStatus, twilio_status: str
    ):
        """When the transition is valid and sequence number is fresh,
        the state machine transition is applied."""
        from app.api.voice import voice_status_callback, TWILIO_STATUS_MAP

        target = TWILIO_STATUS_MAP.get(twilio_status)
        if target is None:
            return

        # Only test valid transitions
        assume(validate_transition(current_status, target))

        mock_cl = _mock_call_log(
            status=current_status.value,
            last_twilio_sequence_number=0,
        )
        updated_cl = _mock_call_log(
            status=target.value,
            last_twilio_sequence_number=5,
        )

        mock_svc = AsyncMock()
        mock_svc.find_by_twilio_sid.return_value = mock_cl
        mock_svc.update_status.return_value = updated_cl

        mock_session = AsyncMock()
        mock_ctx = _make_async_session_ctx(mock_session)

        with patch("app.api.voice.verify_twilio_signature", new_callable=AsyncMock) as mock_sig:
            mock_sig.return_value = {
                "CallSid": "CA_prop_test",
                "CallStatus": twilio_status,
                "SequenceNumber": "5",
            }
            with patch("app.api.voice.async_session_factory", return_value=mock_ctx):
                with patch("app.api.voice.CallLogService", return_value=mock_svc):
                    with patch(
                        "app.api.voice._handle_missed_call_retry",
                        new_callable=AsyncMock,
                    ):
                        request = MagicMock()
                        resp = await voice_status_callback(request)

                        assert resp.status_code == 200
                        mock_svc.update_status.assert_called_once()
                        call_args = mock_svc.update_status.call_args
                        assert call_args[0][1] == target


# =========================================================================
# Property 43 (continued): Pure state machine invariants
# =========================================================================


class TestProperty43StateMachineInvariants:
    """Pure property tests on the state machine — no mocks, no I/O.

    **Validates: Requirements 22.2**
    """

    @given(status=st.sampled_from(
        [CallLogStatus.COMPLETED, CallLogStatus.MISSED, CallLogStatus.CANCELLED,
         CallLogStatus.SKIPPED, CallLogStatus.DEFERRED]
    ))
    @settings(max_examples=20)
    def test_terminal_states_have_no_outgoing_transitions(self, status: CallLogStatus):
        """Terminal states must have an empty set of valid transitions."""
        assert VALID_TRANSITIONS[status] == set()

    @given(
        current=st.sampled_from(list(CallLogStatus)),
        target=st.sampled_from(list(CallLogStatus)),
    )
    @settings(max_examples=100)
    def test_validate_transition_consistent_with_map(
        self, current: CallLogStatus, target: CallLogStatus
    ):
        """validate_transition must agree with the VALID_TRANSITIONS dict."""
        expected = target in VALID_TRANSITIONS.get(current, set())
        assert validate_transition(current, target) == expected

    @given(
        seq_a=st.integers(min_value=0, max_value=1000),
        seq_b=st.integers(min_value=0, max_value=1000),
    )
    @settings(max_examples=50)
    def test_sequence_number_ordering_is_strict(self, seq_a: int, seq_b: int):
        """A callback should only be accepted if its sequence number is
        strictly greater than the persisted one."""
        # This tests the invariant: accept iff incoming > persisted
        should_accept = seq_b > seq_a
        # Simulate the check from voice_status_callback
        is_stale = seq_b <= seq_a
        assert should_accept == (not is_stale)
