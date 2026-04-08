# Charu AI

AI-powered productivity assistant that conducts accountability calls, manages tasks, goals, calendar, and email — accessible via WhatsApp, web chat, and voice calls.

## How It Works

Charu places scheduled voice calls (morning, afternoon, evening) to help users set daily goals, review progress, and stay accountable. Between calls, users interact via WhatsApp or web chat to manage tasks, calendar events, and emails.

```
User ──► WhatsApp ──► Twilio webhook ──► FastAPI ──► Google ADK (Gemini 3.1 Pro)
User ──► Web chat ──► FastAPI (REST/SSE/WebSocket) ──► Google ADK
User ◄── Voice call ◄── Twilio Media Streams ◄── Pipecat + Gemini Live
```

## Stack

- Python 3.12, FastAPI, Celery (Redis broker + RedBeat scheduler)
- Google ADK + Gemini 3.1 Pro (text agents), Gemini Live 2.5 Flash (voice)
- Pipecat for voice call audio pipeline (Twilio Media Streams ↔ Gemini Live)
- SQLModel + PostgreSQL (data), Redis (cache, task queue, ephemeral tokens)
- Firebase Phone/OTP auth (web), Twilio signature validation (WhatsApp)
- Google OAuth 2.0 for Calendar and Gmail integration

## Features

- Scheduled accountability voice calls with AI (morning goals, afternoon check-in, evening reflection)
- Task management with fuzzy matching (create, complete, update, delete, snooze)
- Goal tracking (create, complete, abandon, list)
- Google Calendar integration (read, create, update, delete events, time blocking)
- Gmail integration (check, reply with draft review, compose, search, archive)
- Web search (built-in Google Search grounding on both text and voice agents)
- Post-call WhatsApp recaps and midday check-ins
- Weekly progress summaries
- Dashboard with streaks, heatmap, and goal completion stats

## Architecture

```
Internet
  ├─ HTTPS/WSS ──► Nginx (443) ──► Uvicorn (127.0.0.1:8000)  ← FastAPI
  │                            └──► WebSocket /voice/stream    ← Twilio Media Streams
  └─ Twilio webhooks (HTTPS POST + WSS)

Internal:
  ├─ Celery Worker   ← async tasks (calls, recaps, check-ins)
  ├─ Celery Beat     ← scheduled tasks (RedBeat + Redis)
  ├─ PostgreSQL      ← primary database
  └─ Redis           ← broker + scheduler + ephemeral tokens
```

## Local Development Setup

### Prerequisites

- Python 3.12+, PostgreSQL 16+, Redis 7+
- [uv](https://docs.astral.sh/uv/) package manager
- Google Cloud project with Vertex AI enabled
- Firebase project with Phone auth enabled
- Twilio account (WhatsApp sandbox for dev, voice number for calls)

### Install and Run

```bash
git clone git@github.com:sarathyweb/charu.ai.git
cd charu.ai
cp .env.example .env          # fill in all credentials
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

For Celery (separate terminals):
```bash
uv run celery -A app.celery_app worker --loglevel=info
uv run celery -A app.celery_app beat -S redbeat.RedBeatScheduler --loglevel=info
```

For WhatsApp webhook development, use a Cloudflare Tunnel — see `docs/cloudflare-tunnel-setup.md`.

### Environment Variables

See `.env.example` for the full list. Key variables:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection (asyncpg driver) |
| `GOOGLE_CLOUD_PROJECT` | GCP project for Vertex AI |
| `GOOGLE_CLOUD_LOCATION` | Vertex AI region (`global` for Gemini 3 preview models) |
| `GOOGLE_GENAI_USE_VERTEXAI` | Set `TRUE` for Vertex AI, omit for Google AI Studio |
| `FIREBASE_CREDENTIALS_PATH` | Path to Firebase service account JSON |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | Twilio credentials |
| `TWILIO_WHATSAPP_NUMBER` | WhatsApp sender (e.g. `whatsapp:+14155238886`) |
| `TWILIO_VOICE_NUMBER` | Voice call number (US E.164 format) |
| `WEBHOOK_BASE_URL` | Public URL for Twilio webhooks |
| `GOOGLE_OAUTH_CLIENT_ID` / `SECRET` | Google OAuth for Calendar + Gmail |
| `OAUTH_TOKEN_ENCRYPTION_KEY` | Fernet key for encrypting OAuth tokens at rest |
| `STREAM_TOKEN_SECRET` | HMAC secret for voice WebSocket auth |
| `REDIS_URL` | Redis connection string |
| `CORS_ORIGINS` | Comma-separated allowed origins |

## Tests

```bash
uv sync --extra test
uv run pytest
```

Property-based tests use [Hypothesis](https://hypothesis.readthedocs.io/). Test database must be configured separately.

## Production Deployment

Full deployment guide: `docs/deployment.md`

Runs on a Linux VPS (Ubuntu 24.04) with Nginx, systemd, PostgreSQL, and Redis. Three systemd services: `charu-web` (Uvicorn), `charu-worker` (Celery), `charu-beat` (Celery Beat).

## Setup Guides

| Guide | Description |
|---|---|
| `docs/deployment.md` | Full production deployment (VPS, Nginx, systemd, TLS) |
| `docs/firebase-setup.md` | Firebase project + Phone auth setup |
| `docs/google-oauth-setup.md` | Google OAuth for Calendar + Gmail |
| `docs/twilio-whatsapp-setup.md` | Twilio WhatsApp sender + webhook config |
| `docs/twilio-voice-setup.md` | Twilio voice number + geo permissions + Media Streams |
| `docs/cloudflare-tunnel-setup.md` | Cloudflare Tunnel for local dev webhooks |

## Project Structure

```
app/
├── agents/productivity_agent/  ← ADK agent (root + sub-agents, tools)
├── api/                        ← FastAPI routes (whatsapp, chat, dashboard, oauth)
├── auth/                       ← Firebase + Twilio auth
├── models/                     ← SQLModel data models
├── services/                   ← Business logic (tasks, calendar, gmail, calls)
├── tasks/                      ← Celery tasks (calls, recaps, check-ins, weekly)
├── voice/                      ← Pipecat voice pipeline + tools
├── config.py                   ← Settings (Pydantic)
├── main.py                     ← FastAPI app + lifespan
└── celery_app.py               ← Celery config
migrations/                     ← Alembic migrations
tests/                          ← Unit + property-based tests
docs/                           ← Setup and deployment guides
```
