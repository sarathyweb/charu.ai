"""Authenticated web chat endpoints."""

import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from app.auth.firebase import get_firebase_user
from app.dependencies import get_agent_service, get_user_service
from app.models.schemas import ChatRequest, ChatResponse, FirebasePrincipal
from app.services.agent_service import AgentService
from app.services.user_service import UserService

router = APIRouter()


@router.post("/api/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    principal: FirebasePrincipal = Depends(get_firebase_user),
    user_service: UserService = Depends(get_user_service),
    agent_service: AgentService = Depends(get_agent_service),
):
    """Process a web chat message through the ADK agent.

    Ensures the Firebase-authenticated user has a DB record, then routes the
    message to the productivity agent and returns the reply.
    """
    await user_service.ensure_from_firebase(principal.phone_number, principal.uid)
    result = await agent_service.run(
        user_id=principal.phone_number,
        message=request.message,
        channel="web",
    )
    return ChatResponse(reply=result.reply, session_id=result.session_id)


@router.post("/api/chat/stream")
async def chat_stream(
    request: ChatRequest,
    principal: FirebasePrincipal = Depends(get_firebase_user),
    user_service: UserService = Depends(get_user_service),
    agent_service: AgentService = Depends(get_agent_service),
):
    """Server-sent-events variant of the web chat endpoint.

    The ADK runner currently yields final text events rather than token-level
    deltas through ``AgentService.run``. This endpoint still gives the frontend
    a stable SSE contract and can switch to finer-grained deltas later without
    changing the route.
    """
    await user_service.ensure_from_firebase(principal.phone_number, principal.uid)

    async def events():
        result = await agent_service.run(
            user_id=principal.phone_number,
            message=request.message,
            channel="web",
        )
        yield f"event: session\ndata: {json.dumps({'session_id': result.session_id})}\n\n"
        if result.reply:
            yield f"event: delta\ndata: {json.dumps({'text': result.reply})}\n\n"
        yield (
            "event: done\ndata: "
            f"{json.dumps({'session_id': result.session_id, 'reply': result.reply})}\n\n"
        )

    return StreamingResponse(events(), media_type="text/event-stream")


@router.websocket("/ws/live/{session_id}")
async def browser_voice_live(websocket: WebSocket, session_id: str) -> None:
    """Explicit browser-voice scope response.

    Twilio voice is implemented through ``/voice/stream``. Browser microphone
    audio still needs a product protocol for codec, auth, and playback, so this
    endpoint returns a machine-readable de-scope message instead of a silent 404.
    """
    await websocket.accept()
    try:
        await websocket.send_json(
            {
                "type": "unsupported",
                "session_id": session_id,
                "error": "browser_voice_not_in_active_scope",
                "message": (
                    "Browser voice audio is not enabled; use Twilio calls or "
                    "the text chat endpoint."
                ),
            }
        )
    except WebSocketDisconnect:
        return
    await websocket.close(code=1000)
