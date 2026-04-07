"""FastAPI Depends() wiring for services, DB sessions, and ADK runtime."""

from collections.abc import AsyncGenerator

from fastapi import Depends, Request
from google.adk.artifacts import BaseArtifactService
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import async_session_factory
from app.services.agent_service import AgentService
from app.services.user_service import UserService


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async DB session, closing it when the request finishes."""
    async with async_session_factory() as session:
        yield session


def get_runner(request: Request) -> Runner:
    """Return the ADK Runner stored on app state during lifespan."""
    return request.app.state.runner


def get_session_service(request: Request) -> DatabaseSessionService:
    """Return the ADK DatabaseSessionService stored on app state during lifespan."""
    return request.app.state.session_service


def get_artifact_service(request: Request) -> BaseArtifactService:
    """Return the ADK ArtifactService stored on app state during lifespan."""
    return request.app.state.artifact_service


def get_user_service(
    session: AsyncSession = Depends(get_db_session),
) -> UserService:
    """Provide a UserService wired to the current request's DB session."""
    return UserService(session)


def get_agent_service(
    runner: Runner = Depends(get_runner),
    session_service: DatabaseSessionService = Depends(get_session_service),
    session: AsyncSession = Depends(get_db_session),
) -> AgentService:
    """Provide an AgentService wired to the ADK runtime and current DB session."""
    return AgentService(runner, session_service, session)
