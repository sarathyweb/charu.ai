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

    # Base URL for Twilio webhook signature validation (proxy-safe)
    WEBHOOK_BASE_URL: str

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance. Cached so .env is read only once."""
    return Settings()
