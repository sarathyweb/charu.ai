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

## Prerequisites

- Ubuntu 24.04 LTS (production) or WSL (development)
- Python 3.12+, PostgreSQL 16+, Redis 7+
- [uv](https://docs.astral.sh/uv/) package manager
- Google Cloud project with Vertex AI enabled
- Firebase project with Phone auth enabled
- Twilio account with WhatsApp sender + US voice number
- Domain with TLS (Nginx + Certbot for production, Cloudflare Tunnel for dev)

## Local Development Setup

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

For WhatsApp webhook development, use a Cloudflare Tunnel to expose your local server with a stable subdomain.

## Environment Variables

See `.env.example` for the full list. Key variables:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection (asyncpg driver) |
| `GOOGLE_CLOUD_PROJECT` | GCP project for Vertex AI |
| `GOOGLE_CLOUD_LOCATION` | Vertex AI region (`global` for Gemini 3 preview models) |
| `GOOGLE_CLOUD_LIVE_LOCATION` | Gemini Live voice API region (default `us-east1`) |
| `GOOGLE_GENAI_USE_VERTEXAI` | Set `TRUE` for Vertex AI, omit for Google AI Studio |
| `FIREBASE_CREDENTIALS_PATH` | Path to Firebase service account JSON |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | Twilio credentials |
| `TWILIO_WHATSAPP_NUMBER` | WhatsApp sender (e.g. `whatsapp:+14155238886`) |
| `TWILIO_VOICE_NUMBER` | Outbound voice call number (US E.164 format) |
| `WEBHOOK_BASE_URL` | Public URL for Twilio webhooks and OAuth callbacks |
| `GOOGLE_OAUTH_CLIENT_ID` / `SECRET` | Google OAuth for Calendar + Gmail |
| `OAUTH_TOKEN_ENCRYPTION_KEY` | Fernet key for encrypting OAuth tokens at rest |
| `STREAM_TOKEN_SECRET` | HMAC secret for voice WebSocket auth |
| `REDIS_URL` | Redis connection string |
| `CORS_ORIGINS` | Comma-separated allowed origins |

Generate secrets:
```bash
# Fernet key (for OAUTH_TOKEN_ENCRYPTION_KEY)
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Random token (for STREAM_TOKEN_SECRET)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Firebase Setup

1. Create a Firebase project at [console.firebase.google.com](https://console.firebase.google.com)
2. Enable Phone authentication (Build → Authentication → Sign-in method → Phone)
3. Set SMS region policy to allow your target regions
4. Add test phone numbers for development (Authentication → Sign-in method → Phone numbers for testing)
5. Download service account key (Project Settings → Service accounts → Generate new private key)
6. Save to `secrets/firebase-credentials.json` and set `FIREBASE_CREDENTIALS_PATH` in `.env`
7. For the web frontend: add your domain to Firebase Console → Authentication → Settings → Authorized domains

## Google Cloud + Vertex AI Setup

1. Create a GCP project and enable the Vertex AI API
2. Enable Google Calendar API and Gmail API (for OAuth integrations)
3. Set up Application Default Credentials:
   ```bash
   gcloud auth application-default login --project your-gcp-project-id
   ```
4. For Gemini 3 preview models, set `GOOGLE_CLOUD_LOCATION=global` in `.env`

## Google OAuth Setup (Calendar + Gmail)

1. Go to GCP Console → APIs & Services → OAuth consent screen → configure as External
2. Publish the app (or add test users while in Testing mode)
3. Create OAuth credentials (APIs & Services → Credentials → OAuth client ID → Web application)
4. Add authorized redirect URI: `https://your-domain.com/auth/google/callback`
5. Set `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REDIRECT_URI` in `.env`
6. Generate a Fernet key for `OAUTH_TOKEN_ENCRYPTION_KEY`

OAuth tokens are encrypted at rest with Fernet. Token refresh is automatic. Authorization links are sent via WhatsApp as ephemeral one-time-use links (Redis, 10-min TTL).

## Twilio WhatsApp Setup

1. For development: use Twilio's WhatsApp sandbox (Messaging → Try it out → Send a WhatsApp message)
2. For production: register a WhatsApp Business sender (Messaging → Senders → WhatsApp senders, requires Meta approval)
3. Set webhook URL to `https://your-domain.com/webhook/whatsapp` (POST)
4. Create Content Templates in Twilio Console for proactive messages (recaps, check-ins, weekly summaries) — these require WhatsApp approval for messages outside the 24-hour conversation window
5. Set `TWILIO_WHATSAPP_NUMBER` in `.env`

## Twilio Voice Setup

1. Buy a US phone number with Voice capability (~$1.15/month)
2. Enable geo permissions for your target country (Console → Voice → Settings → Geo Permissions) — without this, outbound calls fail with error 21215
3. Set `TWILIO_VOICE_NUMBER` in `.env` (E.164 format, e.g. `+12135726721`)
4. Ensure your reverse proxy supports WebSocket upgrades on `/voice/stream`
5. AMD (answering machine detection) is configured automatically by the app

Voice endpoints:
| Endpoint | Purpose |
|---|---|
| `/voice/stream` (WebSocket) | Twilio Media Stream — bidirectional call audio |
| `/voice/status-callback` (POST) | Call status updates |
| `/voice/amd-callback` (POST) | Answering machine detection results |

Typical 5-minute call to India costs ~$0.22–0.27 (Twilio voice + AMD + Media Streams).

## Production Deployment

Runs on a Linux VPS (Ubuntu 24.04) with Nginx, systemd, PostgreSQL, and Redis.

### Database

```bash
sudo -u postgres psql <<SQL
CREATE USER charu WITH PASSWORD 'your_password';
CREATE DATABASE charu_ai OWNER charu;
\c charu_ai
CREATE EXTENSION IF NOT EXISTS pg_trgm;
SQL
```

### App User and Install

```bash
sudo useradd -m -s /bin/bash charu
sudo mkdir -p /opt/charu && sudo chown charu:charu /opt/charu
sudo -iu charu
cd /opt/charu
git clone git@github.com:sarathyweb/charu.ai.git app
cd app
curl -LsSf https://astral.sh/uv/install.sh | sh && source ~/.bashrc
uv venv --python 3.12
uv pip install -e .
cp .env.example .env && chmod 600 .env   # fill in production values
uv run alembic upgrade head
```

### Google Cloud Auth (as charu user)

```bash
gcloud auth application-default login --project your-gcp-project-id
```

### Nginx

Reverse proxy on port 443 → Uvicorn on 127.0.0.1:8000. Use `map $http_upgrade` for dynamic WebSocket upgrade handling (no separate location block needed). TLS via Certbot/Let's Encrypt.

### Systemd Services

Three services, all running as the `charu` user:

| Service | Command |
|---|---|
| `charu-web` | `uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 2 --loop uvloop --ws websockets` |
| `charu-worker` | `celery -A app.celery_app worker --loglevel=info --concurrency=4 --max-tasks-per-child=100` |
| `charu-beat` | `celery -A app.celery_app beat -S redbeat.RedBeatScheduler --loglevel=info` |

### Deploy Updates

```bash
sudo -iu charu && cd /opt/charu/app
git pull origin master
uv pip install -e .
uv run alembic upgrade head
exit
sudo systemctl restart charu-web charu-worker charu-beat
```

### Firewall

Only ports 22, 80, 443 need to be open (`sudo ufw allow OpenSSH && sudo ufw allow 'Nginx Full' && sudo ufw enable`).

## Tests

```bash
uv sync --extra test
uv run pytest
```

Property-based tests use [Hypothesis](https://hypothesis.readthedocs.io/). Test database must be configured separately.

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
```
