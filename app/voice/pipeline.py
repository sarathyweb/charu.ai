"""Pipecat pipeline assembly for Twilio + Gemini Live voice calls.

Assembles a per-call ``PipelineTask`` with:
  - ``FastAPIWebsocketTransport`` + ``TwilioFrameSerializer`` (µ-law ↔ PCM)
  - ``GeminiLiveVertexLLMService`` (speech-to-speech, Aoede voice, affective dialog)
  - ``CallTimerProcessor`` for duration enforcement

Design references:
  - Design §2: Voice Call Pipeline
  - Requirement 4: Core Accountability Call Flow
  - Requirement 14: Natural Voice Conversation
  - Requirement 20: Evening Reflection Call
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import WebSocket

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.google.gemini_live import GeminiLiveVertexLLMService
from pipecat.services.google.gemini_live.llm import GeminiVADParams
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from app.config import get_settings
from app.voice.call_timer import create_call_timer
from app.voice.tools import register_voice_tools
from app.voice.transcript_handler import TranscriptCollector

logger = logging.getLogger(__name__)


# ── Data classes ─────────────────────────────────────────────────────────

@dataclass
class CallConfig:
    """Per-call configuration extracted from the WebSocket handshake."""

    stream_sid: str
    call_sid: str
    account_sid: str
    call_type: str  # morning | afternoon | evening | on_demand
    call_log_id: int
    user_id: int
    system_instruction: str = ""


@dataclass
class PipelineResult:
    """Returned after the pipeline finishes running."""

    task: PipelineTask
    transcript: TranscriptCollector


# ── Pipeline assembly ────────────────────────────────────────────────────

async def assemble_pipeline(
    websocket: WebSocket,
    config: CallConfig,
) -> PipelineResult:
    """Build and return a ready-to-run ``PipelineTask``.

    The caller is responsible for ``await result.task.run()``.
    """
    settings = get_settings()

    # ── 1. Twilio serializer (µ-law ↔ PCM, auto hang-up) ────────────
    serializer = TwilioFrameSerializer(
        stream_sid=config.stream_sid,
        call_sid=config.call_sid,
        account_sid=settings.TWILIO_ACCOUNT_SID,
        auth_token=settings.TWILIO_AUTH_TOKEN,
        params=TwilioFrameSerializer.InputParams(auto_hang_up=True),
    )

    # ── 2. FastAPI WebSocket transport ───────────────────────────────
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer,
        ),
    )

    # ── 3. Gemini Live LLM (speech-to-speech) ────────────────────────
    system_instruction = config.system_instruction or _default_instruction(
        config.call_type
    )

    llm = GeminiLiveVertexLLMService(
        project_id=settings.GOOGLE_CLOUD_PROJECT,
        location=settings.GOOGLE_CLOUD_LOCATION,
        settings=GeminiLiveVertexLLMService.Settings(
            model="gemini-live-2.5-flash-native-audio",
            system_instruction=system_instruction,
            voice="Aoede",
            temperature=0.7,
            language="en-US",
            enable_affective_dialog=True,
            vad=GeminiVADParams(silence_duration_ms=500),
        ),
    )

    # ── 4. Register voice tools and create context ───────────────────
    tools = register_voice_tools(
        llm,
        call_log_id=config.call_log_id,
        user_id=config.user_id,
    )

    context = LLMContext(tools=tools)

    # ── 5. Transcript collection ─────────────────────────────────────
    collector = TranscriptCollector()

    # Wire transcript collection via aggregator events
    # (TranscriptProcessor is deprecated in pipecat 0.0.99+)
    async def _on_user_turn(aggregator, text):
        collector.add_user_entry(text)

    async def _on_assistant_turn(aggregator, text):
        collector.add_assistant_entry(text)

    # ── 6. Call timer ────────────────────────────────────────────────
    call_timer = create_call_timer(config.call_type)

    # ── 7. Assemble pipeline ─────────────────────────────────────────
    #
    # For Gemini Live (speech-to-speech), the LLM handles audio
    # directly. We use a minimal pipeline:
    #   transport.input → call_timer → llm → transport.output
    #
    pipeline = Pipeline(
        [
            transport.input(),
            call_timer,
            llm,
            transport.output(),
        ]
    )

    # ── 8. Pipeline task ─────────────────────────────────────────────
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=8000,
            audio_out_sample_rate=8000,
        ),
    )

    return PipelineResult(task=task, transcript=collector)


# ── Helpers ──────────────────────────────────────────────────────────────

def _default_instruction(call_type: str) -> str:
    """Return a minimal default system instruction.

    Pre-call context injection (task 14.5) will replace this with a
    fully personalised instruction.  This fallback ensures the pipeline
    works even before that task is implemented.
    """
    if call_type == "evening":
        return (
            "You are Charu, a warm and supportive accountability companion. "
            "You are conducting a 3-minute evening reflection call. "
            "Ask what the user accomplished today, acknowledge positively, "
            "and ask if there is one thing they want to prioritize tomorrow. "
            "Keep it brief and calming. "
            "When you receive a message starting with [SYSTEM:], treat it "
            "as an internal instruction — do NOT read it aloud."
        )
    return (
        "You are Charu, a warm and supportive accountability companion. "
        "You are conducting a 5-minute morning accountability call. "
        "Greet the user warmly, help them identify their most important "
        "goal for today, and break it down into a concrete next action. "
        "Keep responses short — 1-3 sentences. "
        "When you receive a message starting with [SYSTEM:], treat it "
        "as an internal instruction — do NOT read it aloud."
    )
