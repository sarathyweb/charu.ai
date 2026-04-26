"""Runtime dependency readiness checks."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import redis.asyncio as aioredis
from sqlalchemy import text

from app.celery_app import celery_app
from app.config import get_settings
from app.db import async_session_factory


@dataclass(frozen=True, slots=True)
class RuntimeCheck:
    name: str
    ok: bool
    message: str

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "ok": self.ok, "message": self.message}


def _looks_placeholder(value: str | None) -> bool:
    if not value:
        return True
    lowered = value.lower()
    return any(
        marker in lowered
        for marker in ("your-", "change-me", "generate-", "xxxxxxxx", "path/to/")
    )


async def _check_db() -> RuntimeCheck:
    try:
        async with async_session_factory() as session:
            await session.exec(text("SELECT 1"))
        return RuntimeCheck("database", True, "ok")
    except Exception as exc:
        return RuntimeCheck("database", False, str(exc))


async def _check_redis() -> RuntimeCheck:
    settings = get_settings()
    client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        await client.ping()
        return RuntimeCheck("redis", True, "ok")
    except Exception as exc:
        return RuntimeCheck("redis", False, str(exc))
    finally:
        await client.aclose()


async def _check_celery() -> RuntimeCheck:
    try:
        replies = await asyncio.to_thread(
            celery_app.control.ping,
            timeout=1.0,
            limit=1,
        )
        if replies:
            return RuntimeCheck("celery_worker", True, "ok")
        return RuntimeCheck("celery_worker", False, "no worker ping response")
    except Exception as exc:
        return RuntimeCheck("celery_worker", False, str(exc))


def _check_environment() -> RuntimeCheck:
    settings = get_settings()
    required = {
        "DATABASE_URL": settings.DATABASE_URL,
        "GOOGLE_CLOUD_PROJECT": settings.GOOGLE_CLOUD_PROJECT,
        "FIREBASE_CREDENTIALS_PATH": settings.FIREBASE_CREDENTIALS_PATH,
        "TWILIO_ACCOUNT_SID": settings.TWILIO_ACCOUNT_SID,
        "TWILIO_AUTH_TOKEN": settings.TWILIO_AUTH_TOKEN,
        "TWILIO_WHATSAPP_NUMBER": settings.TWILIO_WHATSAPP_NUMBER,
        "TWILIO_VOICE_NUMBER": settings.TWILIO_VOICE_NUMBER,
        "WEBHOOK_BASE_URL": settings.WEBHOOK_BASE_URL,
        "REDIS_URL": settings.REDIS_URL,
        "CORS_ORIGINS": settings.CORS_ORIGINS,
        "OAUTH_TOKEN_ENCRYPTION_KEY": settings.OAUTH_TOKEN_ENCRYPTION_KEY,
        "STREAM_TOKEN_SECRET": settings.STREAM_TOKEN_SECRET,
    }
    missing = [name for name, value in required.items() if _looks_placeholder(value)]

    template_names = [
        "TWILIO_CONTENT_SID_DAILY_RECAP",
        "TWILIO_CONTENT_SID_DAILY_RECAP_NO_GOAL",
        "TWILIO_CONTENT_SID_EVENING_RECAP",
        "TWILIO_CONTENT_SID_EVENING_RECAP_NO_ACCOMPLISHMENTS",
        "TWILIO_CONTENT_SID_MIDDAY_CHECKIN",
        "TWILIO_CONTENT_SID_MIDDAY_CHECKIN_V2",
        "TWILIO_CONTENT_SID_MIDDAY_CHECKIN_V3",
        "TWILIO_CONTENT_SID_WEEKLY_SUMMARY",
        "TWILIO_CONTENT_SID_MISSED_CALL_ENCOURAGEMENT",
        "TWILIO_CONTENT_SID_EMAIL_DRAFT_REVIEW",
    ]
    missing.extend(
        name for name in template_names if _looks_placeholder(getattr(settings, name, ""))
    )

    firebase_path = settings.FIREBASE_CREDENTIALS_PATH
    if firebase_path and not _looks_placeholder(firebase_path) and not os.path.exists(firebase_path):
        missing.append("FIREBASE_CREDENTIALS_PATH:file_not_found")

    if missing:
        return RuntimeCheck("environment", False, "missing_or_placeholder: " + ", ".join(missing))
    return RuntimeCheck("environment", True, "ok")


async def run_runtime_checks(*, include_celery: bool = True) -> dict[str, object]:
    """Run dependency checks and return a readiness payload."""
    checks = [_check_environment()]
    db_check, redis_check = await asyncio.gather(_check_db(), _check_redis())
    checks.extend([db_check, redis_check])
    if include_celery:
        checks.append(await _check_celery())

    ok = all(check.ok for check in checks)
    return {
        "status": "ready" if ok else "not_ready",
        "checks": [check.as_dict() for check in checks],
    }
