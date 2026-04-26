# Charu AI

AI-powered productivity assistant that conducts accountability calls, captures tasks and goals, and helps with calendar and email follow-up through WhatsApp, backend chat APIs, dashboard pages, and voice calls.

## How It Works

Charu places scheduled voice calls (morning, afternoon, evening) to help users set daily goals, review progress, and stay accountable. Between calls, users can use WhatsApp or the authenticated chat API for task capture, call management, calendar context, and Gmail reply assistance.

```
User ──► WhatsApp ──► Twilio webhook ──► FastAPI ──► Google ADK (Gemini 3.1 Pro)
User ──► Authenticated website chat ──► FastAPI SSE/REST ──► Google ADK
User ◄── Voice call ◄── Twilio Media Streams ◄── Pipecat + Gemini Live
```

## Stack

- Python 3.12, FastAPI, Celery (Redis broker + RedBeat scheduler)
- Google ADK + Gemini 3.1 Pro (text agents), Gemini Live 2.5 Flash (voice)
- Pipecat for voice call audio pipeline (Twilio Media Streams ↔ Gemini Live)
- SQLModel + PostgreSQL (data), Redis (cache, task queue, ephemeral tokens)
- Firebase Phone/OTP auth (web), Twilio signature validation (WhatsApp)
- Google OAuth 2.0 for Calendar and Gmail integration
- Next.js website/dashboard/chat frontend (standalone output)

## Features

- Scheduled accountability voice calls with AI (morning goals, afternoon check-in, evening reflection)
- On-demand callback requests
- Task management with fuzzy and optional Azure OpenAI semantic deduplication
  (create, complete, list pending/completed)
- Goal capture during accountability calls
- Google Calendar integration (today/range reads, event CRUD, available gaps, create time blocks)
- Gmail integration (search/read, compose/send, archive, reviewed reply drafts, send approved replies)
- Web search through the ADK text agent
- Authenticated website chat backed by the same ADK agent and phone identity
- Post-call WhatsApp recaps and midday check-ins
- Weekly progress summaries
- Dashboard with streaks, heatmap, and goal completion stats

## Architecture

```
Internet
  ├─ HTTPS/WSS ──► Nginx (443) ──► Uvicorn (127.0.0.1:8000)  ← FastAPI app
  │                            └──► WebSocket /voice/stream    ← Twilio Media Streams
  └─ Twilio webhooks (HTTPS POST + WSS)

Internal:
  ├─ Celery Worker   ← async tasks (calls, recaps, check-ins)
  ├─ Celery Beat     ← scheduled tasks (RedBeat + Redis)
  ├─ PostgreSQL      ← primary database
  └─ Redis           ← broker + scheduler + ephemeral tokens
```

---

## 1. Prerequisites

- Ubuntu 24.04 LTS (production) or WSL (development)
- Python 3.12+
- PostgreSQL 16+
- Redis 7+
- Nginx (production)
- Domain with DNS A record pointing to server's public IP
- Google Cloud project with Vertex AI API enabled
- Firebase project
- Twilio account (upgraded, not trial)

---

## 2. System Setup (Production Server)

```bash
sudo apt update && sudo apt upgrade -y
sudo hostnamectl set-hostname api.yourdomain.com
echo "127.0.0.1 api.yourdomain.com" | sudo tee -a /etc/hosts
```

Install system packages (PostgreSQL installed separately if you want a specific version):

```bash
sudo apt install -y build-essential libpq-dev python3-dev \
  nginx certbot python3-certbot-nginx \
  redis-server git curl
```

Install uv:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
```

Create app user:

```bash
sudo useradd -m -s /bin/bash charu
sudo mkdir -p /opt/charu
sudo chown charu:charu /opt/charu
```

---

## 3. PostgreSQL

```bash
sudo -u postgres psql <<SQL
CREATE USER charu WITH PASSWORD 'YOUR_STRONG_PASSWORD_HERE';
CREATE DATABASE charu_ai OWNER charu;
\c charu_ai
CREATE EXTENSION IF NOT EXISTS pg_trgm;
SQL
```

Generate a strong password:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
```

---

## 4. Redis

```bash
sudo systemctl enable redis-server
sudo systemctl start redis-server
redis-cli ping  # should return PONG
```

---

## 5. Firebase Setup

1. Create a Firebase project at [console.firebase.google.com](https://console.firebase.google.com) (disable Google Analytics, not needed)
2. Enable Phone authentication: Build → Authentication → Get started → Sign-in method → Phone → Enable
3. Set SMS region policy to allow your target regions (Settings tab)
4. Add test phone numbers for development: Sign-in method → Phone numbers for testing (e.g. `+1 650-555-3434` with code `654321`, up to 10 numbers)
5. Download service account key: Project Settings (⚙️) → Service accounts → Generate new private key
6. Save the JSON file — you'll copy it to the server in step 8
7. Get web app config: Project Settings → General → Your apps → Add app (Web) → copy the `firebaseConfig` values for the frontend `.env.local`

---

## 6. Google Cloud + Vertex AI Setup

1. Create a GCP project at [console.cloud.google.com](https://console.cloud.google.com)
2. Enable these APIs (APIs & Services → Library):
   - Vertex AI API
   - Google Calendar API
   - Gmail API
3. For Gemini 3 preview models, set `GOOGLE_CLOUD_LOCATION=global` in `.env` — these models are only available via the global endpoint, not regional ones like `us-central1`

---

## 7. Google OAuth Setup (Calendar + Gmail)

1. Go to GCP Console → APIs & Services → OAuth consent screen
2. Choose External user type
3. Fill in: App name (`Charu AI`), user support email, developer contact email
4. Add scopes (if prompted): `https://www.googleapis.com/auth/calendar` and `https://www.googleapis.com/auth/gmail.modify`
5. Save, then click "Publish App" to allow any Google account to authorize (otherwise only pre-registered test users work)
6. Go to APIs & Services → Credentials → Create Credentials → OAuth client ID
7. Application type: Web application
8. Authorized redirect URIs: `https://api.yourdomain.com/auth/google/callback`
9. Copy the Client ID and Client Secret for `.env`

Users will see a "This app isn't verified" warning — they can proceed via Advanced → "Go to Charu AI (unsafe)". To remove the warning, submit for Google verification (requires privacy policy URL).

OAuth tokens are encrypted at rest with Fernet. Token refresh is automatic. Authorization links are sent via WhatsApp as ephemeral one-time-use links (Redis, 10-min TTL, consumed atomically via GETDEL).

---

## 8. Application Install

### SSH deploy key (as charu user)

```bash
sudo -iu charu
ssh-keygen -t ed25519 -C "charu@api.yourdomain.com" -N "" -f ~/.ssh/id_ed25519
cat ~/.ssh/id_ed25519.pub
```

Add the public key as a deploy key in GitHub repo Settings → Deploy keys. Test: `ssh -T git@github.com`

### Clone and install

```bash
cd /opt/charu
git clone git@github.com:sarathyweb/charu.ai.git app
cd app
curl -LsSf https://astral.sh/uv/install.sh | sh && source ~/.bashrc
uv venv --python 3.12
uv pip install -e .
```

### Firebase credentials

From your local machine:

```bash
scp -i your-key.pem secrets/firebase-credentials.json ubuntu@YOUR_SERVER_IP:/tmp/firebase.json
```

On the server:

```bash
sudo mkdir -p /opt/charu/app/secrets
sudo cp /tmp/firebase.json /opt/charu/app/secrets/firebase-credentials.json
sudo chown -R charu:charu /opt/charu/app/secrets
sudo chmod 700 /opt/charu/app/secrets
rm /tmp/firebase.json
```

### Google Cloud credentials (as charu user)

```bash
sudo -iu charu
curl https://sdk.cloud.google.com | bash
exec -l $SHELL
gcloud init
gcloud auth application-default login --project your-gcp-project-id
```

No `GOOGLE_APPLICATION_CREDENTIALS` env var needed — ADC credentials are stored in `~/.config/gcloud/application_default_credentials.json` automatically.

### Environment file

```bash
cd /opt/charu/app
nano .env
chmod 600 .env
```

Full `.env` contents:

```env
DATABASE_URL=postgresql+asyncpg://charu:YOUR_DB_PASSWORD@localhost:5432/charu_ai
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
GOOGLE_CLOUD_LOCATION=global
GOOGLE_CLOUD_LIVE_LOCATION=us-east1
GOOGLE_GENAI_USE_VERTEXAI=1
FIREBASE_CREDENTIALS_PATH=/opt/charu/app/secrets/firebase-credentials.json
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your_auth_token
TWILIO_WHATSAPP_NUMBER=whatsapp:+1234567890
TWILIO_VOICE_NUMBER=+1234567890
WEBHOOK_BASE_URL=https://api.yourdomain.com
GOOGLE_OAUTH_CLIENT_ID=your_client_id
GOOGLE_OAUTH_CLIENT_SECRET=your_client_secret
GOOGLE_OAUTH_REDIRECT_URI=https://api.yourdomain.com/auth/google/callback
OAUTH_TOKEN_ENCRYPTION_KEY=GENERATE_NEW_FERNET_KEY
STREAM_TOKEN_SECRET=GENERATE_NEW_SECRET
REDIS_URL=redis://localhost:6379/0
CORS_ORIGINS=https://yourdomain.com,https://www.yourdomain.com

# Optional semantic task deduplication through Azure OpenAI embeddings
TASK_EMBEDDING_DEDUP_ENABLED=false
AZURE_OPENAI_API_KEY=your_azure_openai_key
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_API_VERSION=2025-03-01-preview
AZURE_OPENAI_MODEL=gpt-5.4
AZURE_OPENAI_EMBEDDING_MODEL=text-embedding-3-large
TASK_EMBEDDING_SIMILARITY_THRESHOLD=0.88
TASK_EMBEDDING_BACKFILL_LIMIT=25
```

Generate secrets:

```bash
# Fernet key (for OAUTH_TOKEN_ENCRYPTION_KEY)
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Random token (for STREAM_TOKEN_SECRET and DB password)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### Run database migrations

```bash
uv run alembic upgrade head
```

---

## 9. Twilio WhatsApp Setup

1. For development: use Twilio's WhatsApp sandbox (Messaging → Try it out → Send a WhatsApp message)
2. For production: register a WhatsApp Business sender (Messaging → Senders → WhatsApp senders)
   - Requires Meta Business Verification (1-2 weeks) and WhatsApp sender number approval (~1 week)
   - Need: verified Meta Business Manager, business registration docs, dedicated phone number, business website
3. Set webhook URL in Twilio console: `https://api.yourdomain.com/webhook/whatsapp` (POST)
4. Create Content Templates in Twilio Console (Messaging → Content Editor) for proactive messages:
   - Missed call encouragement, daily recap, weekly summary, midday check-in
   - Templates require WhatsApp approval for messages outside the 24-hour conversation window
   - Within the 24-hour window (after user messages), free-form messages are sent directly
5. Set `TWILIO_WHATSAPP_NUMBER` in `.env` (format: `whatsapp:+1234567890`)

WhatsApp message limits: 250 business-initiated messages per 24h (new unverified), increases after verification. Customer-initiated conversations are unlimited. Max message length: 4096 chars.

---

## 10. Twilio Voice Setup

1. Buy a US phone number with Voice capability (~$1.15/month): Phone Numbers → Manage → Buy a number → Country: US, check Voice
2. Enable geo permissions for your target country: Console → Voice → Settings → [Geo Permissions](https://console.twilio.com/us1/develop/voice/settings/geo-permissions) — without this, outbound calls fail with error 21215
3. Set `TWILIO_VOICE_NUMBER` in `.env` (E.164 format, e.g. `+12135726721`)
4. No webhook configuration needed on the number itself — the app only makes outbound calls and sends TwiML inline via the REST API
5. AMD (answering machine detection) is configured automatically by the app — no separate Twilio setup needed

Voice endpoints (URLs built dynamically from `WEBHOOK_BASE_URL`):

| Endpoint | Purpose |
|---|---|
| `/voice/stream` (WebSocket) | Twilio Media Stream — bidirectional call audio |
| `/voice/status-callback` (POST) | Call status updates (initiated, ringing, answered, completed) |
| `/voice/amd-callback` (POST) | Answering machine detection results |

Typical 5-minute call to India costs ~$0.22–0.27 (voice $0.04/min + AMD $0.0075/call + Media Streams $0.004/min).

---

## 11. DNS + TLS

Point your domain's A record to the server's public IP (not the private EC2 IP). If using Cloudflare, set the record to "DNS only" (gray cloud).

```bash
nslookup api.yourdomain.com 8.8.8.8   # verify propagation
sudo certbot certonly --nginx -d api.yourdomain.com
```

Certbot installs a systemd timer for auto-renewal: `sudo systemctl status certbot.timer`

---

## 12. Nginx

Create `/etc/nginx/sites-available/charu`:

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

upstream charu_backend {
    server 127.0.0.1:8000;
}

server {
    listen 80;
    server_name api.yourdomain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name api.yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/api.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.yourdomain.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    location / {
        proxy_pass http://charu_backend;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
        client_max_body_size 10M;
    }
}
```

The `map` block dynamically handles WebSocket upgrades — no separate location block needed.

```bash
sudo ln -s /etc/nginx/sites-available/charu /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

---

## 13. Systemd Services

### charu-web (FastAPI / Uvicorn)

Create `/etc/systemd/system/charu-web.service`:

```ini
[Unit]
Description=Charu AI FastAPI
After=network.target postgresql.service redis-server.service

[Service]
Type=exec
User=charu
Group=charu
WorkingDirectory=/opt/charu/app
EnvironmentFile=/opt/charu/app/.env
ExecStart=/opt/charu/app/.venv/bin/uvicorn app.main:app \
  --host 127.0.0.1 \
  --port 8000 \
  --workers 2 \
  --loop uvloop \
  --ws websockets \
  --log-level info
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### charu-worker (Celery Worker)

Create `/etc/systemd/system/charu-worker.service`:

```ini
[Unit]
Description=Charu AI Celery Worker
After=network.target redis-server.service

[Service]
Type=exec
User=charu
Group=charu
WorkingDirectory=/opt/charu/app
EnvironmentFile=/opt/charu/app/.env
ExecStart=/opt/charu/app/.venv/bin/celery -A app.celery_app worker \
  --loglevel=info \
  --concurrency=4 \
  --max-tasks-per-child=100
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### charu-beat (Celery Beat Scheduler)

Create `/etc/systemd/system/charu-beat.service`:

```ini
[Unit]
Description=Charu AI Celery Beat
After=network.target redis-server.service

[Service]
Type=exec
User=charu
Group=charu
WorkingDirectory=/opt/charu/app
EnvironmentFile=/opt/charu/app/.env
ExecStart=/opt/charu/app/.venv/bin/celery -A app.celery_app beat \
  -S redbeat.RedBeatScheduler \
  --loglevel=info
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable charu-web charu-worker charu-beat
sudo systemctl start charu-web charu-worker charu-beat
sudo systemctl status charu-web charu-worker charu-beat
```

---

## 14. Firewall

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
```

Only ports 22, 80, and 443 need to be open.

---

## 15. Twilio Console Configuration

Update your Twilio console to point to the production domain:

| Setting | Value |
|---|---|
| WhatsApp webhook URL | `https://api.yourdomain.com/webhook/whatsapp` |

Voice callbacks are set dynamically via `WEBHOOK_BASE_URL` in the TwiML — no manual Twilio console config needed for voice.

---

## 16. Verify

```bash
curl -s https://api.yourdomain.com/health
# Should return {"status": "ok"}
```

Send a WhatsApp message to your Twilio number — the bot should respond.

---

## 17. Frontend Deployment (Next.js)

```
Internet → HTTPS → Nginx (443) → Node.js (127.0.0.1:3000) ← Next.js standalone
```

### Install Node.js

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
```

### Build

```bash
sudo -iu charu
cd /opt/charu/website
npm install
npm run build
cp -r public .next/standalone/
cp -r .next/static .next/standalone/.next/
```

The `cp` steps are required — Next.js standalone output excludes `public/` and `.next/static/` by design.

### Environment file

```bash
nano .env.local
```

```env
NEXT_PUBLIC_API_BASE_URL=https://api.yourdomain.com
NEXT_PUBLIC_FIREBASE_API_KEY=your_key
NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=your_project.firebaseapp.com
NEXT_PUBLIC_FIREBASE_PROJECT_ID=your_project_id
NEXT_PUBLIC_FIREBASE_APP_ID=your_app_id
```

### TLS

```bash
sudo certbot certonly --nginx -d yourdomain.com -d www.yourdomain.com
```

### Nginx

Create `/etc/nginx/sites-available/charu-website`:

```nginx
server {
    listen 80;
    server_name yourdomain.com www.yourdomain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name yourdomain.com www.yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/charu-website /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

### Systemd service

Create `/etc/systemd/system/charu-website.service`:

```ini
[Unit]
Description=Charu AI Website (Next.js)
After=network.target

[Service]
Type=exec
User=charu
Group=charu
WorkingDirectory=/opt/charu/website/.next/standalone
Environment=NODE_ENV=production
Environment=PORT=3000
Environment=HOSTNAME=127.0.0.1
ExecStart=/usr/bin/node server.js
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable charu-website
sudo systemctl start charu-website
```

### Firebase authorized domain

Add `yourdomain.com` in Firebase Console → Authentication → Settings → Authorized domains.

Also ensure `CORS_ORIGINS` in the backend `.env` includes `https://yourdomain.com`.

---

## 18. Deploy Updates

### Backend

```bash
sudo -iu charu
cd /opt/charu/app
git pull origin master
uv pip install -e .
uv run alembic upgrade head
exit
sudo systemctl restart charu-web charu-worker charu-beat
```

If file ownership gets mixed up (e.g. pulling as ubuntu instead of charu):

```bash
sudo chown -R charu:charu /opt/charu/app
```

### Frontend

```bash
sudo -iu charu
cd /opt/charu/website
git pull origin master
npm install
npm run build
cp -r public .next/standalone/
cp -r .next/static .next/standalone/.next/
exit
sudo systemctl restart charu-website
```

---

## 19. Monitoring & Logs

```bash
sudo journalctl -u charu-web -f
sudo journalctl -u charu-worker -f
sudo journalctl -u charu-beat -f
sudo journalctl -u charu-website -f
sudo journalctl -u 'charu-*' --since today
```

---

## 20. Local Development (Cloudflare Tunnel)

For WhatsApp/voice webhook development, use a Cloudflare Tunnel to expose localhost with a stable subdomain.

1. Install cloudflared:
   ```bash
   sudo mkdir -p --mode=0755 /usr/share/keyrings
   curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
   echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/cloudflared.list
   sudo apt update && sudo apt install cloudflared
   ```

2. Authenticate: `cloudflared tunnel login` (opens browser, select your domain)

3. Create tunnel:
   ```bash
   cloudflared tunnel create charu-api-dev
   ```

4. Create config (`~/.cloudflared/config.yml`):
   ```yaml
   url: http://localhost:8000
   tunnel: YOUR_TUNNEL_UUID
   credentials-file: /home/youruser/.cloudflared/YOUR_TUNNEL_UUID.json
   ```

5. Create DNS route:
   ```bash
   cloudflared tunnel route dns charu-api-dev api-dev.yourdomain.com
   ```

6. Run (in a separate terminal):
   ```bash
   cloudflared tunnel run charu-api-dev
   ```

7. Set `WEBHOOK_BASE_URL=https://api-dev.yourdomain.com` in `.env`

The tunnel URL is stable across restarts. Free tier, no limits.

---

## Tests

```bash
uv sync --extra test
uv run pytest
```

Property-based tests use [Hypothesis](https://hypothesis.readthedocs.io/). Test database must be configured separately.

---

## Production Checklist

- [ ] `.env` has production values, `chmod 600`
- [ ] `WEBHOOK_BASE_URL` matches your domain
- [ ] Firebase credentials at configured path, `secrets/` dir is `chmod 700`
- [ ] `gcloud auth application-default login` done as charu user
- [ ] `STREAM_TOKEN_SECRET` and `OAUTH_TOKEN_ENCRYPTION_KEY` are fresh random values
- [ ] `alembic upgrade head` ran successfully
- [ ] `pg_trgm` extension enabled in PostgreSQL
- [ ] TLS certificate valid and auto-renewing
- [ ] Firewall: only 22, 80, 443 open
- [ ] Twilio WhatsApp webhook points to production domain
- [ ] Twilio Voice geo permissions enabled for target country
- [ ] DNS A record points to public IP (not private), Cloudflare DNS-only mode if applicable
- [ ] All systemd services enabled and running (`charu-web`, `charu-worker`, `charu-beat`, `charu-website`)
- [ ] `CORS_ORIGINS` in backend `.env` includes frontend domain
- [ ] Frontend domain added as Firebase authorized domain

---

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
