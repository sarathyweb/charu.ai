"""Pipecat pipeline assembly for Twilio + Gemini Live voice calls.

Assembles a per-call ``PipelineTask`` with:
  - ``FastAPIWebsocketTransport`` + ``TwilioFrameSerializer`` (µ-law ↔ PCM)
  - ``GeminiLiveLLMService`` (speech-to-speech, Aoede voice, affective dialog)
  - ``TranscriptProcessor`` for transcript collection
  - ``CallTimerProcessor`` for duration enforcement
  - ``MinWordsInterruptionStrategy(min_words=2)`` for barge-in

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

from pipecat.audio.interruptions.min_words_interruption_strategy import (
    MinWordsInterruptionStrategy,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
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
            model="google/gemini-2.5-flash-native-audio",
            system_instruction=system_instruction,
            voice="Aoede",
            temperature=0.7,
            language="en-US",
            enable_affective_dialog=True,
            vad=GeminiVADParams(silence_duration_ms=500),
        ),
    )

    # ── 4. Context aggregators ───────────────────────────────────────

    # ── 4a. Register voice tools ─────────────────────────────────────
    tools = register_voice_tools(
        llm,
        call_log_id=config.call_log_id,
        user_id=config.user_id,
    )

    context = OpenAILLMContext(tools=tools)
    context_aggregator = llm.create_context_aggregator(context)

    user_aggregator = context_aggregator.user()
    assistant_aggregator = context_aggregator.assistant()

    # ── 5. Transcript collection ─────────────────────────────────────
    collector = TranscriptCollector()
    transcript_proc = collector.create_processor()

    # ── 6. Call timer ────────────────────────────────────────────────
    call_timer = create_call_timer(config.call_type)

    # ── 7. Assemble pipeline ─────────────────────────────────────────
    #
    # Order (per design §2):
    #   transport.input → transcript.user → user_aggregator
    #     → call_timer → llm
    #     → transport.output → transcript.assistant → assistant_aggregator
    #
    pipeline = Pipeline(
        [
            transport.input(),
            transcript_proc.user(),
            user_aggregator,
            call_timer,
            llm,
            transport.output(),
            transcript_proc.assistant(),
            assistant_aggregator,
        ]
    )

    # ── 8. Pipeline task with interruption strategy ──────────────────
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            interruption_strategies=[
                MinWordsInterruptionStrategy(min_words=2),
            ],
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
