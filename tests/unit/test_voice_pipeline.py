from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.genai.types import ThinkingConfig
from pipecat.frames.frames import LLMRunFrame

from app.voice.pipeline import CallConfig, assemble_pipeline


class _DummyEmitter:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}

    def event_handler(self, name: str):
        def decorator(fn):
            self.handlers[name] = fn
            return fn

        return decorator


class _DummyTransport(_DummyEmitter):
    def __init__(self) -> None:
        super().__init__()
        self._input = object()
        self._output = object()

    def input(self):
        return self._input

    def output(self):
        return self._output


class _DummyTask(_DummyEmitter):
    def __init__(self) -> None:
        super().__init__()
        self.queue_frames = AsyncMock()


@pytest.mark.asyncio
async def test_assemble_pipeline_uses_server_vad_and_zero_thinking_budget():
    websocket = MagicMock()
    config = CallConfig(
        stream_sid="MZ123",
        call_sid="CA123",
        account_sid="AC123",
        call_type="morning",
        call_log_id=7,
        user_id=42,
        system_instruction="Be helpful.",
    )

    settings = SimpleNamespace(
        TWILIO_ACCOUNT_SID="ACtest",
        TWILIO_AUTH_TOKEN="authtoken",
        GOOGLE_CLOUD_PROJECT="project-id",
        GOOGLE_CLOUD_LIVE_LOCATION="global",
    )
    transport = _DummyTransport()
    llm = _DummyEmitter()
    user_aggregator = _DummyEmitter()
    assistant_aggregator = _DummyEmitter()
    task = _DummyTask()
    runner = object()
    context = object()
    tools = object()
    pipeline = object()
    call_timer = object()

    with (
        patch("app.voice.pipeline.get_settings", return_value=settings),
        patch("app.voice.pipeline.TwilioFrameSerializer"),
        patch("app.voice.pipeline.FastAPIWebsocketParams", return_value=object()),
        patch("app.voice.pipeline.FastAPIWebsocketTransport", return_value=transport),
        patch("app.voice.pipeline.GeminiLiveVertexLLMService") as llm_cls,
        patch("app.voice.pipeline.register_voice_tools", return_value=tools),
        patch("app.voice.pipeline.LLMContext", return_value=context),
        patch(
            "app.voice.pipeline.LLMContextAggregatorPair",
            return_value=(user_aggregator, assistant_aggregator),
        ) as aggregator_pair,
        patch("app.voice.pipeline.create_call_timer", return_value=call_timer),
        patch("app.voice.pipeline.Pipeline", return_value=pipeline),
        patch("app.voice.pipeline.PipelineTask", return_value=task),
        patch("app.voice.pipeline.PipelineRunner", return_value=runner),
    ):
        llm_cls.Settings.side_effect = lambda **kwargs: SimpleNamespace(**kwargs)
        llm_cls.return_value = llm

        result = await assemble_pipeline(websocket, config)

    assert result.task is task
    assert result.runner is runner

    settings_kwargs = llm_cls.Settings.call_args.kwargs
    assert settings_kwargs["thinking"] == ThinkingConfig(thinking_budget=0)
    assert llm_cls.call_args.kwargs["function_call_timeout_secs"] == 3.0

    assert aggregator_pair.call_args.args == (context,)
    assert "user_params" not in aggregator_pair.call_args.kwargs

    handler = transport.handlers["on_client_connected"]
    await handler(transport, websocket)
    task.queue_frames.assert_awaited_once()
    queued_frames = task.queue_frames.await_args.args[0]
    assert len(queued_frames) == 1
    assert isinstance(queued_frames[0], LLMRunFrame)
