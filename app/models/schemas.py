"""API request/response schemas: ChatRequest, ChatResponse, AgentRunResult, FirebasePrincipal."""

from sqlmodel import SQLModel


class ChatRequest(SQLModel):
    message: str


class ChatResponse(SQLModel):
    reply: str
    session_id: str


class AgentRunResult(SQLModel):
    reply: str
    session_id: str


class FirebasePrincipal(SQLModel):
    uid: str
    phone_number: str
