"""Voice endpoints — WebSocket stream + Twilio status callback.

Handles:
  - ``/voice/stream`` — bidirectional WebSocket for Twilio Media Streams
  - ``/voice/status-callback`` — Twilio voice status callback (POST)

Design references:
  - Design §2: Voice Call Pipeline (pre-call context injection)
  - Design §3: Dispatch state transitions, retry logic
  - Property 42: WebSocket stream token validation
  - Requirements 4, 6, 14, 20, 22: Core call flow, retry, voice, evening, state tracking
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from time import perf_counter

from fastapi import APIRouter, Depends, Request, Response, WebSocket, WebSocketDisconnect

from app.auth.twilio import verify_twilio_signature
from app.config import get_settings
from app.db import async_session_factory
from app.models.call_log import CallLog
from app.models.call_window import CallWindow
from app.models.enums import CallLogStatus, OccurrenceKind
from app.models.user import User
from app.services.call_log_service import (
    TERMINAL_STATUSES,
    CallLogService,
    InvalidTransitionError,
    StaleVersionError,
)
from app.services.outbound_message_service import (
    OutboundMessageService,
    missed_call_dedup_key,
)
from app.services.scheduling_helpers import MAX_RETRIES, RETRY_DELAY_SECONDS
from app.services.whatsapp_service import WhatsAppService, build_missed_call_params
from app.utils import verify_stream_token
from app.voice.disconnect import EarlyDisconnectDetector

logger = logging.getLogger(__name__)

router = APIRouter()


async def _validate_token_from_query(websocket: WebSocket) -> dict | None:
    """Extract and validate the HMAC stream token from the query string.

    The token is passed as ``?token=...`` on the WebSocket URL by the
    ``trigger_call_task`` when building the TwiML ``<Stream>`` URL.

    Returns the decoded token payload dict on success, or ``None`` on
    failure (invalid, expired, or missing token).
    """
    token = websocket.query_params.get("token")
    if not token:
        logger.warning("voice/stream: missing token query parameter")
        return None

    settings = get_settings()
    payload = verify_stream_token(secret=settings.STREAM_TOKEN_SECRET, token=token)
    if payload is None:
        logger.warning("voice/stream: invalid or expired stream token")
        return None

    return payload


async def _read_start_message(websocket: WebSocket) -> dict | None:
    """Read Twilio's ``connected`` and ``start`` messages from the WebSocket.

    Twilio sends a ``connected`` message first, then a ``start`` message
    containing stream metadata (streamSid, callSid, customParameters).

    Returns the parsed ``start`` message dict, or ``None`` on error.
    """
    # First message: "connected"
    try:
        raw = await websocket.receive_text()
        msg = json.loads(raw)
        if msg.get("event") != "connected":
            logger.warning(
                "voice/stream: expected 'connected' event, got %r",
                msg.get("event"),
            )
            # Some Twilio versions may send start directly — try parsing as start
            if msg.get("event") == "start":
                return msg
            return None
    except (json.JSONDecodeError, WebSocketDisconnect):
        logger.warning("voice/stream: failed to read connected message")
        return None

    # Second message: "start"
    try:
        raw = await websocket.receive_text()
        msg = json.loads(raw)
        if msg.get("event") != "start":
            logger.warning(
                "voice/stream: expected 'start' event, got %r",
                msg.get("event"),
            )
            return None
        return msg
    except (json.JSONDecodeError, WebSocketDisconnect):
        logger.warning("voice/stream: failed to read start message")
        return None


async def _transition_call_to_in_progress(
    call_log_id: int,
) -> CallLog | None:
    """Look up the CallLog and transition it to ``in_progress``.

    Returns the updated CallLog on success, or ``None`` if the lookup
    or transition fails.
    """
    async with async_session_factory() as session:
        call_log = await session.get(CallLog, call_log_id)
        if call_log is None:
            logger.error(
                "voice/stream: CallLog %d not found", call_log_id
            )
            return None

        # Only transition from ringing → in_progress
        if call_log.status != CallLogStatus.RINGING.value:
            logger.warning(
                "voice/stream: CallLog %d status is %r, expected 'ringing'",
                call_log_id,
                call_log.status,
            )
            # Still return the call_log so the pipeline can proceed
            # (the status callback may have already moved it)
            if call_log.status == CallLogStatus.IN_PROGRESS.value:
                return call_log
            return None

        svc = CallLogService(session)
        try:
            call_log = await svc.update_status(
                call_log_id,
                CallLogStatus.IN_PROGRESS,
                expected_version=call_log.version,
                actual_start_time=datetime.now(timezone.utc),
            )
            return call_log
        except (StaleVersionError, InvalidTransitionError) as exc:
            logger.warning(
                "voice/stream: could not transition CallLog %d to in_progress: %s",
                call_log_id,
                exc,
            )
            # Re-read to check if another process already moved it
            await session.refresh(call_log)
            if call_log.status == CallLogStatus.IN_PROGRESS.value:
                return call_log
            return None


async def _handle_early_disconnect_retry(
    call_log_id: int,
    call_type: str,
) -> None:
    """Handle an early disconnect by marking the call as missed and scheduling a retry.

    Creates a new CallLog entry with incremented ``attempt_number`` if
    retries remain (max 2 retries = 3 total attempts) and the retry
    would fit within the call window.

    Design references:
      - Requirement 6: Missed Call Retry Behavior
      - Requirement 14.4: Early disconnect → treat as missed
      - Property 25: Early disconnect detection
    """
    from app.models.enums import OccurrenceKind
    from app.services.scheduling_helpers import (
        MAX_RETRIES,
        RETRY_DELAY_SECONDS,
    )

    try:
        async with async_session_factory() as session:
            call_log = await session.get(CallLog, call_log_id)
            if call_log is None:
                logger.warning(
                    "voice/stream: _handle_early_disconnect_retry: "
                    "CallLog %d not found",
                    call_log_id,
                )
                return

            # Only transition from in_progress → missed
            # (the call was transitioned to in_progress when the pipeline started)
            svc = CallLogService(session)
            try:
                call_log = await svc.update_status(
                    call_log_id,
                    CallLogStatus.MISSED,
                    expected_version=call_log.version,
                    end_time=datetime.now(timezone.utc),
                )
            except (StaleVersionError, InvalidTransitionError) as exc:
                logger.warning(
                    "voice/stream: could not mark CallLog %d as missed "
                    "for early disconnect: %s",
                    call_log_id,
                    exc,
                )
                return

            # Check if retries are available
            if call_log.attempt_number > MAX_RETRIES:
                logger.info(
                    "voice/stream: all retries exhausted for "
                    "call_log_id=%d (attempt %d/%d) — sending WhatsApp",
                    call_log_id,
                    call_log.attempt_number,
                    MAX_RETRIES + 1,
                )
                await _send_missed_encouragement(call_log)
                return

            # Schedule a retry by creating a new CallLog entry
            retry_time = datetime.now(timezone.utc) + timedelta(
                seconds=RETRY_DELAY_SECONDS
            )

            # Check if retry fits within the call window
            if call_log.origin_window_id is not None:
                fits = await _retry_fits_in_window(call_log, retry_time)
                if not fits:
                    logger.info(
                        "voice/stream: early-disconnect retry would fall "
                        "outside window for call_log_id=%d — sending WhatsApp",
                        call_log_id,
                    )
                    await _send_missed_encouragement(call_log)
                    return

            retry_log = CallLog(
                user_id=call_log.user_id,
                call_type=call_log.call_type,
                call_date=call_log.call_date,
                scheduled_time=retry_time,
                scheduled_timezone=call_log.scheduled_timezone,
                status=CallLogStatus.SCHEDULED.value,
                occurrence_kind=OccurrenceKind.RETRY.value,
                attempt_number=call_log.attempt_number + 1,
                root_call_log_id=call_log.root_call_log_id or call_log.id,
                origin_window_id=call_log.origin_window_id,
            )
            session.add(retry_log)
            await session.commit()

            logger.info(
                "voice/stream: scheduled retry for call_log_id=%d → "
                "new call_log_id=%d (attempt %d, scheduled at %s)",
                call_log_id,
                retry_log.id,
                retry_log.attempt_number,
                retry_time.isoformat(),
            )

    except Exception:
        logger.exception(
            "voice/stream: error handling early disconnect retry "
            "for call_log_id=%d",
            call_log_id,
        )


@router.websocket("/voice/stream")
async def voice_stream(websocket: WebSocket) -> None:
    """Twilio Media Stream WebSocket endpoint.

    Protocol:
    1. Accept the WebSocket connection.
    2. Read Twilio's ``connected`` + ``start`` messages.
    3. Extract metadata (streamSid, callSid, customParameters).
    4. Validate HMAC stream token from custom parameters.
    5. Validate that token's call_log_id/user_id match the custom params.
    6. Look up CallLog, transition to ``in_progress``.
    7. Run the Pipecat voice pipeline.
    8. On disconnect, perform cleanup.
    """
    # ------------------------------------------------------------------
    # Step 1: Accept the WebSocket (must accept before reading messages)
    # ------------------------------------------------------------------
    await websocket.accept()

    result = None
    token_call_log_id: int | None = None
    token_user_id: int | None = None
    stream_sid = "unknown"
    call_type = "morning"
    call_ctx: dict = {}
    pipeline_failed = False

    try:
        # ------------------------------------------------------------------
        # Step 2: Read Twilio start message
        # ------------------------------------------------------------------
        start_msg = await _read_start_message(websocket)
        if start_msg is None:
            logger.warning("voice/stream: failed to read start message, closing")
            await websocket.close(code=4002, reason="Missing start message")
            return

        # ------------------------------------------------------------------
        # Step 3: Extract Twilio metadata
        # ------------------------------------------------------------------
        start_data = start_msg.get("start", {})
        stream_sid: str = start_data.get("streamSid", "")
        call_sid: str = start_data.get("callSid", "")
        account_sid: str = start_data.get("accountSid", "")
        custom_params: dict = start_data.get("customParameters", {})

        logger.info(
            "voice/stream: stream_sid=%s, call_sid=%s, account_sid=%s",
            stream_sid,
            call_sid,
            account_sid,
        )

        # ------------------------------------------------------------------
        # Step 4: Validate HMAC stream token from custom parameters
        # ------------------------------------------------------------------
        token = custom_params.get("token")
        if not token:
            logger.warning("voice/stream: missing token in custom parameters")
            await websocket.close(code=4001, reason="Missing stream token")
            return

        settings = get_settings()
        token_payload = verify_stream_token(secret=settings.STREAM_TOKEN_SECRET, token=token)
        if token_payload is None:
            logger.warning("voice/stream: invalid or expired stream token")
            await websocket.close(code=4001, reason="Invalid stream token")
            return

        token_call_log_id: int = token_payload["call_log_id"]
        token_user_id: int = token_payload["user_id"]

        logger.info(
            "voice/stream: token validated — call_log_id=%d, user_id=%d",
            token_call_log_id,
            token_user_id,
        )

        # ------------------------------------------------------------------
        # Step 5: Validate token claims against Twilio metadata
        # ------------------------------------------------------------------
        # Validate AccountSid matches our configured Twilio account
        if account_sid and account_sid != settings.TWILIO_ACCOUNT_SID:
            logger.warning(
                "voice/stream: AccountSid mismatch — token for our account "
                "but stream from %s",
                account_sid,
            )
            await websocket.close(
                code=4003, reason="AccountSid mismatch"
            )
            return

        # Validate call_log_id and user_id from custom params match token
        param_call_log_id = custom_params.get("call_log_id")
        param_user_id = custom_params.get("user_id")

        if param_call_log_id is not None:
            try:
                if int(param_call_log_id) != token_call_log_id:
                    logger.warning(
                        "voice/stream: call_log_id mismatch — "
                        "token=%d, param=%s",
                        token_call_log_id,
                        param_call_log_id,
                    )
                    await websocket.close(
                        code=4004, reason="call_log_id mismatch"
                    )
                    return
            except (ValueError, TypeError):
                pass

        if param_user_id is not None:
            try:
                if int(param_user_id) != token_user_id:
                    logger.warning(
                        "voice/stream: user_id mismatch — "
                        "token=%d, param=%s",
                        token_user_id,
                        param_user_id,
                    )
                    await websocket.close(
                        code=4005, reason="user_id mismatch"
                    )
                    return
            except (ValueError, TypeError):
                pass

        # Extract call_type from custom params (for pipeline configuration)
        call_type: str = custom_params.get("call_type", "morning")

        # ------------------------------------------------------------------
        # Step 6: Look up CallLog and transition to in_progress
        # ------------------------------------------------------------------
        call_log = await _transition_call_to_in_progress(token_call_log_id)
        if call_log is None:
            logger.error(
                "voice/stream: could not find or transition CallLog %d",
                token_call_log_id,
            )
            await websocket.close(
                code=4006, reason="CallLog not found or invalid state"
            )
            return

        logger.info(
            "voice/stream: CallLog %d transitioned to in_progress "
            "(user_id=%d, call_type=%s, call_sid=%s)",
            token_call_log_id,
            token_user_id,
            call_type,
            call_sid,
        )

        # ------------------------------------------------------------------
        # Step 6b: Build pre-call context and system instruction
        # ------------------------------------------------------------------
        from app.voice.context import prepare_call_context

        system_instruction = ""
        context_started_at = perf_counter()
        try:
            async with async_session_factory() as ctx_session:
                system_instruction, call_ctx = await prepare_call_context(
                    user_id=token_user_id,
                    call_type=call_type,
                    session=ctx_session,
                )
            logger.info(
                "voice/stream: built system instruction for "
                "call_log_id=%d (%d chars, %.1fms)",
                token_call_log_id,
                len(system_instruction),
                (perf_counter() - context_started_at) * 1000,
            )
        except Exception:
            logger.exception(
                "voice/stream: failed to build context for "
                "call_log_id=%d, using default instruction",
                token_call_log_id,
            )

        # ------------------------------------------------------------------
        # Step 7: Run Pipecat voice pipeline
        #
        # Assembles and runs the full pipeline:
        #   TwilioFrameSerializer → TranscriptProcessor → CallTimer
        #   → GeminiLiveLLMService → transport output
        # ------------------------------------------------------------------
        from app.voice.pipeline import CallConfig, assemble_pipeline

        pipeline_config = CallConfig(
            stream_sid=stream_sid,
            call_sid=call_sid,
            account_sid=account_sid,
            call_type=call_type,
            call_log_id=token_call_log_id,
            user_id=token_user_id,
            system_instruction=system_instruction,
        )

        # Early disconnect detector — tracks connection timing and
        # consults TranscriptCollector.first_user_utterance_at to
        # determine if the call ended before meaningful interaction.
        disconnect_detector = EarlyDisconnectDetector()
        try:
            pipeline_started_at = perf_counter()
            result = await assemble_pipeline(websocket, pipeline_config)
            logger.info(
                "voice/stream: assembled pipeline for call_log_id=%d in %.1fms",
                token_call_log_id,
                (perf_counter() - pipeline_started_at) * 1000,
            )

            # Mark the moment the pipeline is ready and running
            disconnect_detector.mark_connected()

            logger.info(
                "voice/stream: starting Pipecat pipeline for "
                "call_log_id=%d (call_type=%s)",
                token_call_log_id,
                call_type,
            )

            # Blocks until the call ends (user hangs up, timer expires,
            # or EndFrame is pushed).
            runner = result.runner
            await runner.run(result.task)

            # Mark disconnection for elapsed-time calculation
            disconnect_detector.mark_disconnected()

            logger.info(
                "voice/stream: pipeline finished for call_log_id=%d "
                "(%d transcript entries)",
                token_call_log_id,
                len(result.transcript.entries),
            )
        except WebSocketDisconnect:
            disconnect_detector.mark_disconnected()
            logger.info(
                "voice/stream: WebSocket disconnected during pipeline "
                "for call_log_id=%d",
                token_call_log_id,
            )
        except Exception:
            pipeline_failed = True
            disconnect_detector.mark_disconnected()
            logger.exception(
                "voice/stream: pipeline error for call_log_id=%d",
                token_call_log_id,
            )

        except WebSocketDisconnect:
            logger.info(
                "voice/stream: early disconnect for call_log_id=%s",
                token_call_log_id if token_call_log_id is not None else "unknown",
            )
        except Exception:
            pipeline_failed = True
            logger.exception(
                "voice/stream: unexpected error for call_log_id=%s",
                token_call_log_id if token_call_log_id is not None else "unknown",
            )
    finally:
        # ------------------------------------------------------------------
        # Step 8: Cleanup — early disconnect detection and post-call actions
        # ------------------------------------------------------------------

        # Determine the first_user_utterance_at from the transcript
        # collector (if the pipeline ran far enough to create one).
        first_user_utterance_at = None
        if "result" in dir() and result is not None:
            first_user_utterance_at = result.transcript.first_user_utterance_at

        # Ensure disconnected_at is set even for outer exceptions
        if "disconnect_detector" in dir():
            if disconnect_detector.disconnected_at is None:
                disconnect_detector.mark_disconnected()

            if disconnect_detector.is_early_disconnect(first_user_utterance_at):
                logger.info(
                    "voice/stream: early disconnect detected for "
                    "call_log_id=%d (elapsed=%.1fs, user_utterance=%s) "
                    "— treating as missed, triggering retry",
                    token_call_log_id,
                    disconnect_detector.elapsed_seconds,
                    first_user_utterance_at is not None,
                )
                await _handle_early_disconnect_retry(
                    token_call_log_id, call_type if "call_type" in dir() else "morning",
                )
            elif pipeline_failed:
                logger.info(
                    "voice/stream: pipeline failed for call_log_id=%d "
                    "(elapsed=%.1fs) — marking as missed",
                    token_call_log_id,
                    disconnect_detector.elapsed_seconds,
                )
                # Transition in_progress → missed so the call is not
                # recorded as a successful completion.
                try:
                    async with async_session_factory() as session:
                        call_log = await session.get(CallLog, token_call_log_id)
                        if call_log and call_log.status == CallLogStatus.IN_PROGRESS.value:
                            svc = CallLogService(session)
                            await svc.update_status(
                                token_call_log_id,
                                CallLogStatus.MISSED,
                                expected_version=call_log.version,
                                end_time=datetime.now(timezone.utc),
                            )
                except Exception:
                    logger.exception(
                        "voice/stream: failed to mark call_log_id=%d as missed "
                        "after pipeline error",
                        token_call_log_id,
                    )
            else:
                logger.info(
                    "voice/stream: normal disconnect for call_log_id=%d "
                    "(elapsed=%.1fs)",
                    token_call_log_id,
                    disconnect_detector.elapsed_seconds,
                )

                # ── Post-call cleanup for normal calls ───────────────
                from app.voice.cleanup import post_call_cleanup

                transcript_dicts: list[dict] = []
                if "result" in dir() and result is not None:
                    transcript_dicts = result.transcript.to_dicts()

                try:
                    await post_call_cleanup(
                        call_log_id=token_call_log_id,
                        user_id=token_user_id,
                        call_type=call_type if "call_type" in dir() else "morning",
                        transcript_dicts=transcript_dicts,
                        call_ctx=call_ctx if "call_ctx" in dir() else {},
                    )
                except Exception:
                    logger.exception(
                        "voice/stream: post_call_cleanup failed for "
                        "call_log_id=%d",
                        token_call_log_id,
                    )

        logger.info(
            "voice/stream: connection ended for call_log_id=%s, "
            "stream_sid=%s",
            token_call_log_id if token_call_log_id is not None else "unknown",
            stream_sid,
        )


# ---------------------------------------------------------------------------
# Twilio status-to-internal mapping
# ---------------------------------------------------------------------------

#: Maps Twilio ``CallStatus`` values to internal ``CallLogStatus``.
#: ``queued`` and ``initiated`` are ignored (no state change).
TWILIO_STATUS_MAP: dict[str, CallLogStatus | None] = {
    "queued": None,  # no-op
    "initiated": None,  # no-op
    "ringing": CallLogStatus.RINGING,
    "in-progress": CallLogStatus.IN_PROGRESS,
    "completed": CallLogStatus.COMPLETED,
    "busy": CallLogStatus.MISSED,
    "no-answer": CallLogStatus.MISSED,
    "failed": CallLogStatus.MISSED,
    "canceled": CallLogStatus.CANCELLED,
}

#: Twilio statuses that should trigger retry logic on missed calls.
#: ``failed`` is excluded — it indicates a number/routing issue, not a
#: transient miss, so retrying would be pointless.
_RETRYABLE_MISSED_STATUSES = frozenset({"busy", "no-answer"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _handle_missed_call_retry(
    call_log: CallLog,
    twilio_status: str,
) -> None:
    """Schedule a retry or send WhatsApp encouragement after a missed call.

    Creates a new ``CallLog`` with incremented ``attempt_number`` if retries
    remain and the retry fits within the call window.  Otherwise sends the
    ``missed_call_encouragement`` WhatsApp template via the dedup flow.

    Design references:
      - Design §3: Retry logic
      - Requirement 6: Missed Call Retry Behavior
    """
    # ``failed`` means a number/routing issue — don't retry
    if twilio_status not in _RETRYABLE_MISSED_STATUSES:
        logger.info(
            "voice/status-callback: twilio_status=%r for call_log_id=%d "
            "— not retryable, checking if WhatsApp needed",
            twilio_status,
            call_log.id,
        )
        # For 'failed', still send WhatsApp if this was the last attempt
        await _send_missed_encouragement_if_exhausted(call_log)
        return

    # Check retry budget: attempt_number <= MAX_RETRIES means retries remain
    # (attempt 1 = initial, attempt 2 = 1st retry, attempt 3 = 2nd retry)
    if call_log.attempt_number > MAX_RETRIES:
        logger.info(
            "voice/status-callback: all retries exhausted for "
            "call_log_id=%d (attempt %d/%d) — sending WhatsApp",
            call_log.id,
            call_log.attempt_number,
            MAX_RETRIES + 1,
        )
        await _send_missed_encouragement(call_log)
        return

    # Check if retry fits within the call window
    retry_time = datetime.now(timezone.utc) + timedelta(seconds=RETRY_DELAY_SECONDS)

    if call_log.origin_window_id is not None:
        fits = await _retry_fits_in_window(call_log, retry_time)
        if not fits:
            logger.info(
                "voice/status-callback: retry would fall outside window "
                "for call_log_id=%d — sending WhatsApp",
                call_log.id,
            )
            await _send_missed_encouragement(call_log)
            return

    # Create a new CallLog for the retry
    async with async_session_factory() as session:
        retry_log = CallLog(
            user_id=call_log.user_id,
            call_type=call_log.call_type,
            call_date=call_log.call_date,
            scheduled_time=retry_time,
            scheduled_timezone=call_log.scheduled_timezone,
            status=CallLogStatus.SCHEDULED.value,
            occurrence_kind=OccurrenceKind.RETRY.value,
            attempt_number=call_log.attempt_number + 1,
            root_call_log_id=call_log.root_call_log_id or call_log.id,
            origin_window_id=call_log.origin_window_id,
        )
        session.add(retry_log)
        await session.commit()
        await session.refresh(retry_log)

        logger.info(
            "voice/status-callback: scheduled retry call_log_id=%d → "
            "new call_log_id=%d (attempt %d, at %s)",
            call_log.id,
            retry_log.id,
            retry_log.attempt_number,
            retry_time.isoformat(),
        )


async def _retry_fits_in_window(
    call_log: CallLog,
    retry_time_utc: datetime,
) -> bool:
    """Return True if *retry_time_utc* falls before the call window's end."""
    from zoneinfo import ZoneInfo

    async with async_session_factory() as session:
        window = await session.get(CallWindow, call_log.origin_window_id)
        if window is None:
            # No window info — allow the retry (best-effort)
            return True

        try:
            tz = ZoneInfo(call_log.scheduled_timezone)
        except (KeyError, Exception):
            tz = timezone.utc  # type: ignore[assignment]

        retry_local = retry_time_utc.astimezone(tz)
        window_end_local = datetime.combine(
            retry_local.date(), window.end_time, tzinfo=tz
        )
        return retry_time_utc < window_end_local.astimezone(timezone.utc)


async def _send_missed_encouragement_if_exhausted(call_log: CallLog) -> None:
    """Send WhatsApp encouragement only if all retries are exhausted."""
    if call_log.attempt_number > MAX_RETRIES:
        await _send_missed_encouragement(call_log)


async def _send_missed_encouragement(call_log: CallLog) -> None:
    """Send ``missed_call_encouragement`` WhatsApp template via dedup flow."""
    root_id = call_log.root_call_log_id or call_log.id
    dedup = f"missed_encouragement:{root_id}"

    async with async_session_factory() as session:
        user = await session.get(User, call_log.user_id)
        if user is None:
            logger.warning(
                "voice/status-callback: user %d not found for missed "
                "encouragement (call_log_id=%d)",
                call_log.user_id,
                call_log.id,
            )
            return

        user_name = user.name or "there"
        content_variables = build_missed_call_params(user_name)

        # Resolve content SID from settings
        settings = get_settings()
        content_sid = getattr(
            settings,
            "TWILIO_CONTENT_SID_MISSED_CALL_ENCOURAGEMENT",
            None,
        ) or "MISSING_CONTENT_SID:missed_call_encouragement"

        wa = WhatsAppService()
        oms = OutboundMessageService(session, wa)
        try:
            sid = await oms.send_template_dedup(
                user_id=user.id,  # type: ignore[arg-type]
                dedup_key=dedup,
                to=user.phone,
                content_sid=content_sid,
                content_variables=content_variables,
            )
            if sid:
                logger.info(
                    "voice/status-callback: sent missed_call_encouragement "
                    "for call_log_id=%d (twilio_sid=%s)",
                    call_log.id,
                    sid,
                )
            else:
                logger.info(
                    "voice/status-callback: missed_call_encouragement "
                    "dedup hit for call_log_id=%d",
                    call_log.id,
                )
        except Exception:
            logger.exception(
                "voice/status-callback: failed to send "
                "missed_call_encouragement for call_log_id=%d",
                call_log.id,
            )


# ---------------------------------------------------------------------------
# POST /voice/status-callback
# ---------------------------------------------------------------------------


@router.post("/voice/status-callback")
async def voice_status_callback(
    request: Request,
) -> Response:
    """Handle Twilio voice status callbacks.

    Twilio POSTs form data with ``CallSid``, ``CallStatus``,
    ``SequenceNumber``, and optional fields like ``CallDuration``.

    This endpoint:
    1. Validates the Twilio signature.
    2. Looks up the ``CallLog`` by ``twilio_call_sid``.
    3. Discards out-of-order callbacks via ``SequenceNumber``.
    4. Maps the Twilio status to an internal ``CallLogStatus``.
    5. Applies the state machine transition with optimistic locking.
    6. On ``completed``: sets ``end_time`` and ``duration_seconds``.
    7. On ``in-progress``: sets ``actual_start_time``.
    8. On missed (``busy``, ``no-answer``): triggers retry or WhatsApp.
    9. On ``failed``: marks missed, does NOT retry.
    10. Always returns 200 to Twilio (even on internal errors).

    Design references:
      - Design §3: Dispatch state transitions, retry logic
      - Research 37: Call State Tracking
      - Requirements 6, 22
    """
    # ------------------------------------------------------------------
    # Step 1: Validate Twilio signature and parse form data
    # ------------------------------------------------------------------
    try:
        form = await verify_twilio_signature(request)
    except Exception:
        logger.warning("voice/status-callback: invalid Twilio signature")
        # Still return 200 to avoid Twilio retries on auth failures
        return Response(status_code=200)

    call_sid: str = form.get("CallSid", "")
    twilio_status: str = form.get("CallStatus", "")
    seq_raw: str = form.get("SequenceNumber", "")
    duration_raw: str = form.get("CallDuration", "")

    logger.info(
        "voice/status-callback: CallSid=%s CallStatus=%s Seq=%s",
        call_sid,
        twilio_status,
        seq_raw,
    )

    if not call_sid or not twilio_status:
        return Response(status_code=200)

    # Parse SequenceNumber (Twilio sends it as a string)
    try:
        seq_num = int(seq_raw) if seq_raw else None
    except (ValueError, TypeError):
        seq_num = None

    # ------------------------------------------------------------------
    # Step 2: Look up CallLog by twilio_call_sid
    # ------------------------------------------------------------------
    try:
        async with async_session_factory() as session:
            svc = CallLogService(session)
            call_log = await svc.find_by_twilio_sid(call_sid)

            if call_log is None:
                logger.warning(
                    "voice/status-callback: no CallLog for CallSid=%s",
                    call_sid,
                )
                return Response(status_code=200)

            # ----------------------------------------------------------
            # Step 3: SequenceNumber ordering — discard stale callbacks
            # ----------------------------------------------------------
            if (
                seq_num is not None
                and call_log.last_twilio_sequence_number is not None
                and seq_num <= call_log.last_twilio_sequence_number
            ):
                logger.info(
                    "voice/status-callback: stale seq=%d ≤ persisted=%d "
                    "for call_log_id=%d — discarding",
                    seq_num,
                    call_log.last_twilio_sequence_number,
                    call_log.id,
                )
                return Response(status_code=200)

            # ----------------------------------------------------------
            # Step 4: Map Twilio status → internal status
            # ----------------------------------------------------------
            internal_status = TWILIO_STATUS_MAP.get(twilio_status)
            if internal_status is None:
                # queued / initiated → no state change, just update seq
                if seq_num is not None:
                    from sqlalchemy import update

                    stmt = (
                        update(CallLog)
                        .where(CallLog.id == call_log.id)
                        .values(
                            last_twilio_sequence_number=seq_num,
                            updated_at=datetime.now(timezone.utc),
                        )
                    )
                    await session.exec(stmt)  # type: ignore[call-overload]
                    await session.commit()
                return Response(status_code=200)

            # ----------------------------------------------------------
            # Step 5: Build extra fields for the status update
            # ----------------------------------------------------------
            extra: dict[str, object] = {}
            if seq_num is not None:
                extra["last_twilio_sequence_number"] = seq_num

            if twilio_status == "in-progress":
                extra["actual_start_time"] = datetime.now(timezone.utc)

            if twilio_status == "completed":
                extra["end_time"] = datetime.now(timezone.utc)
                if duration_raw:
                    try:
                        extra["duration_seconds"] = int(duration_raw)
                    except (ValueError, TypeError):
                        pass

            if twilio_status in ("busy", "no-answer", "failed", "canceled"):
                extra["end_time"] = datetime.now(timezone.utc)
                if duration_raw:
                    try:
                        extra["duration_seconds"] = int(duration_raw)
                    except (ValueError, TypeError):
                        pass

            # ----------------------------------------------------------
            # Step 6: Apply state machine transition
            # ----------------------------------------------------------
            try:
                updated_log = await svc.update_status(
                    call_log.id,  # type: ignore[arg-type]
                    internal_status,
                    expected_version=call_log.version,
                    **extra,
                )
            except InvalidTransitionError:
                logger.info(
                    "voice/status-callback: invalid transition %s → %s "
                    "for call_log_id=%d — ignoring",
                    call_log.status,
                    internal_status.value,
                    call_log.id,
                )
                # Still update sequence number and duration even if
                # transition is invalid (e.g. cleanup already marked
                # completed before Twilio's callback arrived).
                patch_values: dict[str, object] = {
                    "updated_at": datetime.now(timezone.utc),
                }
                if seq_num is not None:
                    patch_values["last_twilio_sequence_number"] = seq_num
                if duration_raw:
                    try:
                        patch_values["duration_seconds"] = int(duration_raw)
                    except (ValueError, TypeError):
                        pass
                if len(patch_values) > 1:  # more than just updated_at
                    from sqlalchemy import update as sa_update

                    stmt = (
                        sa_update(CallLog)
                        .where(CallLog.id == call_log.id)
                        .values(**patch_values)
                    )
                    await session.exec(stmt)  # type: ignore[call-overload]
                    await session.commit()
                return Response(status_code=200)
            except StaleVersionError:
                logger.info(
                    "voice/status-callback: stale version for "
                    "call_log_id=%d — concurrent update",
                    call_log.id,
                )
                return Response(status_code=200)

    except Exception:
        logger.exception(
            "voice/status-callback: error processing CallSid=%s",
            call_sid,
        )
        return Response(status_code=200)

    # ------------------------------------------------------------------
    # Step 7: Post-transition side effects (outside the DB session)
    # ------------------------------------------------------------------
    try:
        if internal_status == CallLogStatus.MISSED:
            await _handle_missed_call_retry(updated_log, twilio_status)
    except Exception:
        logger.exception(
            "voice/status-callback: error in post-transition logic "
            "for call_log_id=%d",
            call_log.id,
        )

    return Response(status_code=200)


# ---------------------------------------------------------------------------
# AMD AnsweredBy values that indicate a machine/fax — hang up + treat as missed
# ---------------------------------------------------------------------------

_MACHINE_ANSWERED_BY: frozenset[str] = frozenset({
    "machine_start",
    "machine_end_beep",
    "machine_end_silence",
    "machine_end_other",
    "fax",
})


# ---------------------------------------------------------------------------
# POST /voice/amd-callback
# ---------------------------------------------------------------------------


@router.post("/voice/amd-callback")
async def voice_amd_callback(
    request: Request,
) -> Response:
    """Handle Twilio Async AMD (Answering Machine Detection) callbacks.

    Twilio POSTs form data with ``CallSid``, ``AnsweredBy``, and
    optionally ``MachineDetectionDuration`` when async AMD completes.

    This endpoint:
    1. Validates the Twilio signature.
    2. Extracts ``CallSid`` and ``AnsweredBy``.
    3. Looks up the ``CallLog`` by ``twilio_call_sid``.
    4. Persists ``AnsweredBy`` to ``CallLog.answered_by`` for ALL outcomes.
    5. For machine/fax: hangs up the call via Twilio REST API, transitions
       CallLog to missed, and triggers retry logic.
    6. For human/unknown: no further action — call proceeds normally.
    7. Always returns 200 to Twilio.

    Design references:
      - Design §3: AMD callback handling
      - Research 20: Missed Call Retry Behavior (async AMD)
      - Requirements 6.V2: Voicemail Detection
    """
    # ------------------------------------------------------------------
    # Step 1: Validate Twilio signature and parse form data
    # ------------------------------------------------------------------
    try:
        form = await verify_twilio_signature(request)
    except Exception:
        logger.warning("voice/amd-callback: invalid Twilio signature")
        return Response(status_code=200)

    call_sid: str = form.get("CallSid", "")
    answered_by: str = form.get("AnsweredBy", "")
    machine_detection_duration: str = form.get("MachineDetectionDuration", "")

    logger.info(
        "voice/amd-callback: CallSid=%s AnsweredBy=%s Duration=%s",
        call_sid,
        answered_by,
        machine_detection_duration,
    )

    if not call_sid or not answered_by:
        return Response(status_code=200)

    # ------------------------------------------------------------------
    # Step 2: Look up CallLog and persist AnsweredBy for ALL outcomes
    # ------------------------------------------------------------------
    call_log: CallLog | None = None
    try:
        async with async_session_factory() as session:
            svc = CallLogService(session)
            call_log = await svc.find_by_twilio_sid(call_sid)

            if call_log is None:
                logger.warning(
                    "voice/amd-callback: no CallLog for CallSid=%s",
                    call_sid,
                )
                return Response(status_code=200)

            # Persist answered_by regardless of the value
            from sqlalchemy import update as sa_update

            stmt = (
                sa_update(CallLog)
                .where(CallLog.id == call_log.id)
                .values(
                    answered_by=answered_by,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await session.exec(stmt)  # type: ignore[call-overload]
            await session.commit()

            logger.info(
                "voice/amd-callback: persisted answered_by=%r for "
                "call_log_id=%d",
                answered_by,
                call_log.id,
            )

            # Re-read to get fresh state for subsequent logic
            await session.refresh(call_log)

    except Exception:
        logger.exception(
            "voice/amd-callback: error persisting answered_by for "
            "CallSid=%s",
            call_sid,
        )
        return Response(status_code=200)

    # ------------------------------------------------------------------
    # Step 3: For human/unknown — no further action
    # ------------------------------------------------------------------
    if answered_by not in _MACHINE_ANSWERED_BY:
        logger.info(
            "voice/amd-callback: answered_by=%r for call_log_id=%d "
            "— call proceeds normally",
            answered_by,
            call_log.id,
        )
        return Response(status_code=200)

    # ------------------------------------------------------------------
    # Step 4: Machine/fax detected — hang up the call via Twilio REST API
    # ------------------------------------------------------------------
    try:
        from starlette.concurrency import run_in_threadpool
        from twilio.rest import Client as TwilioClient

        settings = get_settings()
        twilio_client = TwilioClient(
            settings.TWILIO_ACCOUNT_SID,
            settings.TWILIO_AUTH_TOKEN,
        )
        await run_in_threadpool(
            twilio_client.calls(call_sid).update,
            status="completed",
        )
        logger.info(
            "voice/amd-callback: hung up call CallSid=%s "
            "(answered_by=%s)",
            call_sid,
            answered_by,
        )
    except Exception:
        logger.exception(
            "voice/amd-callback: failed to hang up CallSid=%s",
            call_sid,
        )
        # Continue to mark as missed even if hang-up fails — the status
        # callback will eventually arrive and handle it.

    # ------------------------------------------------------------------
    # Step 5: Transition CallLog to missed
    # ------------------------------------------------------------------
    try:
        async with async_session_factory() as session:
            svc = CallLogService(session)
            # Re-read to get the latest version
            call_log = await svc.find_by_twilio_sid(call_sid)
            if call_log is None:
                return Response(status_code=200)

            # Only transition if the call is not already in a terminal state
            current_status = CallLogStatus(call_log.status)
            if current_status in TERMINAL_STATUSES:
                logger.info(
                    "voice/amd-callback: call_log_id=%d already in "
                    "terminal state %r — skipping missed transition",
                    call_log.id,
                    call_log.status,
                )
                return Response(status_code=200)

            try:
                updated_log = await svc.update_status(
                    call_log.id,  # type: ignore[arg-type]
                    CallLogStatus.MISSED,
                    expected_version=call_log.version,
                    end_time=datetime.now(timezone.utc),
                )
            except InvalidTransitionError:
                logger.info(
                    "voice/amd-callback: invalid transition %s → missed "
                    "for call_log_id=%d — ignoring",
                    call_log.status,
                    call_log.id,
                )
                return Response(status_code=200)
            except StaleVersionError:
                logger.info(
                    "voice/amd-callback: stale version for "
                    "call_log_id=%d — concurrent update",
                    call_log.id,
                )
                return Response(status_code=200)

    except Exception:
        logger.exception(
            "voice/amd-callback: error transitioning call_log to missed "
            "for CallSid=%s",
            call_sid,
        )
        return Response(status_code=200)

    # ------------------------------------------------------------------
    # Step 6: Trigger retry logic (same as status callback missed path)
    # ------------------------------------------------------------------
    try:
        # Use "busy" as the twilio_status since AMD-detected machine calls
        # are retryable (unlike "failed" which indicates a number issue)
        await _handle_missed_call_retry(updated_log, "busy")
    except Exception:
        logger.exception(
            "voice/amd-callback: error in retry logic for "
            "call_log_id=%d",
            updated_log.id,
        )

    return Response(status_code=200)
