"""FastAPI application entry point with lifespan, router includes, and CORS."""

import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Logging — ADK uses the standard Python logging module internally.
# See: https://adk.dev/observability/logging/
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
for _name in ("httpx", "httpcore", "google.auth", "urllib3"):
    logging.getLogger(_name).setLevel(logging.WARNING)

# Enable DEBUG for ADK internals to see full tool calls, results, and LLM prompts
logging.getLogger("google_adk").setLevel(logging.DEBUG)

# Load .env into os.environ BEFORE any ADK imports — ADK's genai Client
# reads GOOGLE_GENAI_USE_VERTEXAI, GOOGLE_CLOUD_PROJECT, etc. directly
# from os.environ, not from pydantic Settings.
load_dotenv()

import firebase_admin
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from firebase_admin import credentials
from google.adk.artifacts import InMemoryArtifactService
from google.adk.plugins import ReflectAndRetryToolPlugin
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService

from app.agents.productivity_agent import root_agent
from app.api.auth_sync import router as auth_sync_router
from app.api.chat import router as chat_router
from app.api.google_oauth import router as google_oauth_router
from app.api.health import router as health_router
from app.api.voice import router as voice_router
from app.api.dashboard import router as dashboard_router
from app.api.whatsapp import router as whatsapp_router
from app.config import get_settings
from app.db import create_db_tables
from app.services.agent_service import APP_NAME

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown events.

    1. Initialize Firebase Admin SDK (idempotent — skips if already init'd).
    2. Create SQLModel tables in PostgreSQL.
    3. Create ADK DatabaseSessionService backed by the same PostgreSQL.
    4. Create ADK InMemoryArtifactService for transcript storage.
    5. Create ADK Runner wired to the root_agent with session + artifact services.
    6. Store runner, session_service, artifact_service on app.state for DI.
    """
    settings = get_settings()

    # --- Firebase Admin SDK ---------------------------------------------------
    if not firebase_admin._apps:
        cred = credentials.Certificate(settings.FIREBASE_CREDENTIALS_PATH)
        firebase_admin.initialize_app(cred)
        logger.info("Firebase Admin SDK initialized")

    # --- Database tables ------------------------------------------------------
    await create_db_tables()
    logger.info("Database tables created / verified")

    # --- ADK session service + runner -----------------------------------------
    session_service = DatabaseSessionService(db_url=settings.DATABASE_URL)
    artifact_service = InMemoryArtifactService()
    runner = Runner(
        agent=root_agent,
        app_name=APP_NAME,
        session_service=session_service,
        artifact_service=artifact_service,
        plugins=[ReflectAndRetryToolPlugin(max_retries=2)],
    )
    logger.info(
        "ADK Runner, DatabaseSessionService, and InMemoryArtifactService initialized "
        "(with ReflectAndRetry plugin)"
    )

    app.state.runner = runner
    app.state.session_service = session_service
    app.state.artifact_service = artifact_service

    yield


app = FastAPI(title="Charu AI", version="0.1.0", lifespan=lifespan)

# ---------------------------------------------------------------------------
# CORS — allow the frontend origin (and localhost for dev)
# ---------------------------------------------------------------------------
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(health_router)
app.include_router(auth_sync_router)
app.include_router(chat_router)
app.include_router(whatsapp_router)
app.include_router(google_oauth_router)
app.include_router(voice_router)
app.include_router(dashboard_router)
