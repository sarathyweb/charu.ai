"""Pipecat pipeline assembly for Twilio + Gemini Live voice calls.

Assembles a per-call ``PipelineTask`` with:
  - ``FastAPIWebsocketTransport`` + ``TwilioFrameSerializer`` (µ-law ↔ PCM)
  - ``GeminiLiveVertexLLMService`` (speech-to-speech, native audio)
  - ``LLMContextAggregatorPair`` for context management
  - ``CallTimerProcessor`` for duration enforcement

Based on the official Pipecat Gemini Live + Twilio example:
  https://github.com/pipecat-ai/pipecat-examples/tree/main/gemini-live-starters/phone-bot

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

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
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
    runner: PipelineRunner
    transcript: TranscriptCollector


# ── Pipeline assembly ────────────────────────────────────────────────────

async def assemble_pipeline(
    websocket: WebSocket,
    config: CallConfig,
) -> PipelineResult:
    """Build and return a ready-to-run pipeline.

    The caller is responsible for ``await result.runner.run(result.task)``.
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
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer,
        ),
    )

    # ── 3. Gemini Live LLM (speech-to-speech via Vertex AI) ─────────
    system_instruction = config.system_instruction or _default_instruction(
        config.call_type
    )

    llm = GeminiLiveVertexLLMService(
        project_id=settings.GOOGLE_CLOUD_PROJECT,
        location=settings.GOOGLE_CLOUD_LIVE_LOCATION,
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

    # ── 4. Register voice tools ──────────────────────────────────────
    tools = register_voice_tools(
        llm,
        call_log_id=config.call_log_id,
        user_id=config.user_id,
    )

    # ── 5. Context + aggregators (following official Pipecat pattern) ─
    # Seed context with an initial user message to trigger the bot's
    # opening greeting when inference_on_context_initialization fires.
    messages = [
        {
            "role": "user",
            "content": "Start the call with your opening greeting.",
        },
    ]
    context = LLMContext(messages, tools=tools)
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
        ),
    )

    # ── 6. Transcript collection via aggregator events ───────────────
    collector = TranscriptCollector()

    @user_aggregator.event_handler("on_user_turn_stopped")
    async def on_user_turn_stopped(aggregator, strategy, message):
        collector.add_user_entry(message.content)

    @assistant_aggregator.event_handler("on_assistant_turn_stopped")
    async def on_assistant_turn_stopped(aggregator, message):
        collector.add_assistant_entry(message.content)

    # ── 7. Call timer ────────────────────────────────────────────────
    call_timer = create_call_timer(config.call_type)

    # ── 8. Assemble pipeline ─────────────────────────────────────────
    pipeline = Pipeline(
        [
            transport.input(),
            user_aggregator,
            call_timer,
            llm,
            transport.output(),
            assistant_aggregator,
        ]
    )

    # ── 9. Pipeline task ─────────────────────────────────────────────
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    # Kick off the conversation when the pipeline starts
    @task.event_handler("on_pipeline_started")
    async def on_pipeline_started(task, frame):
        logger.info(
            "voice/pipeline: pipeline started for call_log_id=%d, "
            "queuing LLMRunFrame to kick off conversation",
            config.call_log_id,
        )
        await task.queue_frames([LLMRunFrame()])

    runner = PipelineRunner(handle_sigint=False)

    return PipelineResult(task=task, runner=runner, transcript=collector)


# ── Helpers ──────────────────────────────────────────────────────────────

def _default_instruction(call_type: str) -> str:
    """Return a minimal default system instruction."""
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
