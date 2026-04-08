# Charu AI

Productivity assistant with WhatsApp, web chat, and voice call channels. Built on Google ADK + Gemini, FastAPI, and Pipecat.

## Stack

- **Backend:** Python 3.11+, FastAPI, Celery, SQLModel (PostgreSQL)
- **AI:** Google ADK, Gemini 3.1 Pro (text), Gemini Live 2.5 Flash (voice)
- **Channels:** Twilio WhatsApp, Twilio Voice (Pipecat), Web chat + WebSocket
- **Auth:** Firebase Phone/OTP (web), Twilio signature validation (WhatsApp)
- **Infra:** Google Cloud Run, Redis, PostgreSQL

## Setup

```bash
cp .env.example .env   # fill in credentials
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Tests

```bash
uv sync --extra test
uv run pytest
```
