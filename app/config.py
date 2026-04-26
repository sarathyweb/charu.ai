"""Pydantic Settings configuration — loads environment variables from .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file."""

    # PostgreSQL connection string (asyncpg driver)
    DATABASE_URL: str

    # Google ADK / Vertex AI
    GOOGLE_CLOUD_PROJECT: str
    GOOGLE_CLOUD_LOCATION: str = "global"
    GOOGLE_GENAI_USE_VERTEXAI: bool = True

    # Path to Firebase service account credentials JSON
    FIREBASE_CREDENTIALS_PATH: str

    # Twilio credentials for WhatsApp integration
    TWILIO_ACCOUNT_SID: str
    TWILIO_AUTH_TOKEN: str
    TWILIO_WHATSAPP_NUMBER: str

    # Twilio voice number for outbound calls (E.164 format)
    TWILIO_VOICE_NUMBER: str = ""

    # Twilio Content API template SIDs
    TWILIO_CONTENT_SID_DAILY_RECAP: str = ""
    TWILIO_CONTENT_SID_DAILY_RECAP_NO_GOAL: str = ""
    TWILIO_CONTENT_SID_EVENING_RECAP: str = ""
    TWILIO_CONTENT_SID_EVENING_RECAP_NO_ACCOMPLISHMENTS: str = ""
    TWILIO_CONTENT_SID_MIDDAY_CHECKIN: str = ""
    TWILIO_CONTENT_SID_MIDDAY_CHECKIN_V2: str = ""
    TWILIO_CONTENT_SID_MIDDAY_CHECKIN_V3: str = ""
    TWILIO_CONTENT_SID_WEEKLY_SUMMARY: str = ""
    TWILIO_CONTENT_SID_MISSED_CALL_ENCOURAGEMENT: str = ""
    TWILIO_CONTENT_SID_EMAIL_DRAFT_REVIEW: str = ""

    # Google OAuth 2.0 client credentials
    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: str = ""
    GOOGLE_OAUTH_REDIRECT_URI: str = ""

    # Azure OpenAI credentials and deployments. Keep real values in .env only.
    AZURE_OPENAI_API_KEY: str = ""
    AZURE_OPENAI_ENDPOINT: str = ""
    AZURE_OPENAI_API_VERSION: str = "2025-03-01-preview"
    AZURE_OPENAI_MODEL: str = "gpt-5.4"
    AZURE_OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-large"
    AZURE_OPENAI_EMBEDDING_DIMENSIONS: int | None = None

    # Semantic task deduplication is opt-in because it sends task text to Azure.
    TASK_EMBEDDING_DEDUP_ENABLED: bool = False
    TASK_EMBEDDING_SIMILARITY_THRESHOLD: float = 0.88
    TASK_EMBEDDING_BACKFILL_LIMIT: int = 25

    # Gmail automation. Per-user opt-in is still required in addition to
    # these global switches.
    EMAIL_AUTOMATION_ENABLED: bool = True
    URGENT_EMAIL_CALLS_ENABLED: bool = True
    AUTO_TASK_FROM_EMAILS_ENABLED: bool = True
    EMAIL_AUTOMATION_LOOKBACK_DAYS: int = 2
    EMAIL_AUTOMATION_MAX_MESSAGES_PER_USER: int = 10
    URGENT_EMAIL_CALL_DELAY_MINUTES: int = 2
    URGENT_EMAIL_CALL_COOLDOWN_MINUTES: int = 240
    URGENT_EMAIL_CALL_MAX_PER_DAY: int = 1
    URGENT_EMAIL_MIN_SCORE: float = 0.65
    AUTO_TASK_EMAIL_MIN_SCORE: float = 0.7

    # Fernet key for encrypting OAuth tokens at rest.
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    OAUTH_TOKEN_ENCRYPTION_KEY: str = ""

    # HMAC secret for signing WebSocket stream tokens
    STREAM_TOKEN_SECRET: str = "change-me-in-production"

    # Google Cloud location override for Gemini Live API (requires regional endpoint)
    GOOGLE_CLOUD_LIVE_LOCATION: str = "us-east1"

    # Base URL for Twilio webhook signature validation (proxy-safe)
    WEBHOOK_BASE_URL: str

    # Redis URL for Celery broker and RedBeat scheduler
    REDIS_URL: str = "redis://localhost:6379/0"

    # Comma-separated allowed CORS origins (e.g. "https://app.example.com,http://localhost:3000")
    CORS_ORIGINS: str

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
    )


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance. Cached so .env is read only once."""
    return Settings()
