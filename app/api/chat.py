"""POST /api/chat — authenticated web chat endpoint."""

from fastapi import APIRouter, Depends

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
