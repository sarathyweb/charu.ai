# Project Notes

## Architecture

- Single custom FastAPI service (not ADK's built-in get_fast_api_app)
- Full control over auth, routing, and middleware
- ADK Runner + Agent wired manually inside FastAPI lifespan
- DatabaseSessionService (PostgreSQL) for persistent sessions

### Endpoints

- `/webhook/whatsapp` — Twilio WhatsApp webhook (Meta signature validation)
- `/api/chat` — Web text chat (Firebase JWT)
- `/api/chat/stream` — Web text chat SSE (Firebase JWT)
- `/ws/live/{session_id}` — Web voice chat WebSocket (Firebase JWT via query param)
- `/twilio/voice` — Twilio voice call TwiML (phase 2)
- `/twilio/stream` — Twilio Media Streams WebSocket + Pipecat (phase 2)
- `/health` — No auth

## Auth

- Universal identity: phone number (same across web and WhatsApp)
- Web users: Firebase Auth Phone/OTP login
  - User enters phone number → Firebase sends OTP via SMS → user verifies → gets JWT
  - JWT contains phone number as the verified identity
  - This means web user_id = phone number = WhatsApp user_id (same person, same sessions)
- WhatsApp users: Phone number = identity (auto-login, verified by WhatsApp/Twilio)
- WebSocket (Live API): Firebase JWT passed as query parameter, verified before accept()
- Twilio webhooks: X-Twilio-Signature header validation
- Live API service is public but token-gated (ephemeral token pattern if split later)
- Cross-channel continuity: customer can start on WhatsApp, continue on web (same session history)

## Twilio + WhatsApp Integration

### How It Works

1. Customer sends WhatsApp message
2. Twilio receives it, POSTs to your webhook with form data
3. Validate X-Twilio-Signature header
4. Extract phone number (From field) and message body (Body field)
5. Phone number = ADK user_id (auto-login)
6. Run ADK agent via runner.run_async()
7. Send reply via Twilio REST API (twilio.rest.Client)

### 24-Hour Customer Service Window

- When a customer sends you a message, a 24-hour window opens
- During this window: you can send free-form replies (any text)
- After 24 hours with no customer message: you can ONLY send pre-approved message templates
- Templates must be submitted to and approved by Meta before use
- Template approval typically takes 1-2 days
- This means: proactive outbound messages (reminders, follow-ups) MUST use templates

### WhatsApp Sender Verification Timeline

- Twilio Sandbox: instant, for development/testing only
- Production sender registration (via Twilio):
  - Meta Business Verification: 1-2 weeks
  - WhatsApp sender number approval: ~1 week
  - Total: 7-14 business days typical
  - Can be faster (1-3 days) with complete documentation
  - 15-30% rejection rate due to incomplete docs
- Requirements:
  - Verified Meta Business Manager account
  - Valid business registration documents
  - Dedicated phone number (not already on WhatsApp)
  - Business website matching your display name
- If rejected: can reapply after 30 days
- Start this process EARLY — don't wait until launch

### WhatsApp Message Limits

- New unverified business: 250 business-initiated messages per 24 hours
- After verification: limits increase based on quality and volume
- Message limit only applies to business-initiated (template) messages
- Customer-initiated conversations (within 24h window) are unlimited
- Max message length: 4096 characters

### Twilio Environment Variables

```
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your_auth_token
TWILIO_WHATSAPP_NUMBER=whatsapp:+14155238886
```

## Google ADK + Gemini

### Models

- Text agents: gemini-3.1-pro-preview (latest, 1M context window)
- Voice/Live API: gemini-live-2.5-flash-native-audio (GA)
- Premium reasoning: gemini-2.5-pro ($1.25/$10 per M tokens)
- Preview models: gemini-3-flash, gemini-3.1-pro-preview (latest, higher cost)

### Live API (Voice)

- Bidirectional WebSocket streaming
- Input: PCM 16-bit 16kHz audio, images/video, text
- Output: PCM 16-bit 24kHz audio, text
- 30 HD voices, 24 languages
- Supports barge-in (interruption), affective dialog, tool use
- Session limit: 10 min (Vertex AI), 15 min audio-only (Google AI)
- Use RunConfig with StreamingMode.BIDI
- Each session needs its own LiveRequestQueue (never reuse)

### Session Storage Options

- InMemorySessionService — dev only, lost on restart
- DatabaseSessionService — PostgreSQL, MySQL, SQLite (production)
- VertexAiSessionService — managed by Google Cloud

## Phone Calls (Phase 2)

- Twilio Media Streams for bidirectional audio
- Audio format mismatch: Twilio sends mulaw 8kHz, Gemini expects PCM 16kHz
- Need audio transcoding in both directions
- Use Pipecat framework for audio pipeline (transcoding, VAD, interruptions)
- Known issue: Gemini Live API latency over telephony can be 5-6 seconds

## Voice Stability Decisions

- 2026-04-07: Live voice startup now treats Calendar enrichment as best-effort and fast-fail for call startup.
  Rationale: `prepare_call_context()` was on the critical path before the first greeting, and Calendar reads could consume the full `1s + 2s + 4s` retry budget before fallback. Live calls should start with local context first and only use Calendar if it returns quickly.
  Source: `.pm/research/53-live-voice-startup-latency.md`

- 2026-04-07: For the Vertex Gemini Live voice path, prefer Gemini server-side VAD over per-call local Silero VAD.
  Rationale: Pipecat's Gemini Live docs say server-side VAD is the default, and the current upstream Vertex function-calling example uses `LLMContextAggregatorPair(context)` with no local `SileroVADAnalyzer()`. Local Silero adds about 1.24s of per-call startup cost in this repo. If local VAD is reintroduced later, Gemini server VAD should be explicitly disabled instead of running a hybrid setup.
  Source: `.pm/research/53-live-voice-startup-latency.md`

- 2026-04-07: Disable Gemini thinking for latency-sensitive phone calls.
  Rationale: Google Gemini thinking docs say 2.5 Flash has thinking enabled by default and that additional thinking can add seconds of latency. Pipecat's Gemini Live starters also set `ThinkingConfig(thinking_budget=0)` for voice flows.
  Source: `.pm/research/53-live-voice-startup-latency.md`

- 2026-04-07: Full pre-call personalization should no longer block the first Live voice greeting.
  Rationale: even after fast-failing Calendar, `voice_stream()` still waits for `prepare_call_context()` before starting the Gemini Live session. Pipecat supports late context appends, so the better tradeoff is to start with a minimal default instruction and inject personalized `[SYSTEM: ...]` context after connect if it misses a small startup budget.
  Source: `.pm/research/53-live-voice-startup-latency.md`

- 2026-04-07: Voice prompts must include a short spoken bridge before tool calls, and voice tool execution should use a shorter timeout budget.
  Rationale: live tool calls inherently pause audio until the tool result is returned. A one-line bridge plus a shorter voice-specific timeout reduces perceived dead air and caps long hangs.
  Source: `.pm/research/54-live-voice-tool-call-audio-gap.md`

- 2026-04-07: The current Vertex Live stack should treat tool-call silence as a synchronous-platform constraint, not a prompt-only bug.
  Rationale: Google and Pipecat docs/source show that the Live tool loop continues only after the client returns a function response, and `google-genai==1.70.0` does not expose `FunctionDeclaration.behavior` for Vertex AI. For now, the correct mitigation is fast local tools, short voice timeouts, and verbal bridges rather than assuming non-blocking tool audio is available.
  Source: `.pm/research/54-live-voice-tool-call-audio-gap.md`

## Goal Tracking Decisions

- 2026-04-25: Goals are stored in a dedicated `goals` table and managed by ID-based tools, while `CallLog.goal` remains the per-call outcome text used by recaps and check-ins.
  Rationale: the full-tools spec defines goals as higher-level objectives that span days or weeks, distinct from atomic tasks. ID-based goal mutations avoid fuzzy-match ambiguity, and keeping `CallLog.goal` preserves existing call-summary behavior.
  Source: `.pm/research/79-goal-model-service-tools-implementation.md`

## Calendar And Gmail Tool Decisions

- 2026-04-25: General Calendar events are separate from task time blocks. Event CRUD lets Google generate event IDs, while task time blocks keep deterministic IDs and Charu metadata.
  Rationale: task blocks need idempotent retry behavior tied to a task and day; general events need normal Calendar semantics for user-managed appointments.
  Source: `.pm/research/80-calendar-gmail-full-tools-expansion.md`

- 2026-04-25: New outbound Gmail compose is a direct send tool but must be used only after explicit user approval; archive removes the `INBOX` label rather than deleting mail.
  Rationale: composing a new email is externally visible and should be confirmation-gated, while archive should preserve mail history and match Gmail's non-destructive inbox cleanup behavior.
  Source: `.pm/research/80-calendar-gmail-full-tools-expansion.md`

## AI Evaluation Decisions

- 2026-04-25: Production-readiness checks now include deterministic ADK eval data, voice eval contracts, exact root-agent tool registration tests, and an explicit deferred-product backlog review.
  Rationale: live model evals are valuable but should be backed by source-controlled contracts that fail deterministically when tool names, schemas, confirmation gates, voice safety flags, or core scenario coverage drift.
  Source: `.pm/research/81-deterministic-adk-voice-evals-and-backlog.md`

- 2026-04-26: The website dashboard should expose the backend dashboard APIs as one authenticated dashboard surface and use Vitest/React Testing Library for deterministic UI/API wiring tests.
  Rationale: the dashboard is a client-component app surface backed by authenticated FastAPI endpoints, so component/integration tests can verify request paths, payloads, and visible controls without a running browser server. This closes the gap between backend API completion and product-facing availability.
  Source: `.pm/research/85-website-dashboard-ui-api-parity.md`, `.pm/research/86-website-frontend-tests.md`

- 2026-04-26: The marketing website should lead with a full-bleed product scene, concrete proof signals, and realistic dashboard/phone previews rather than abstract decorative gradients.
  Rationale: the product is trust-sensitive and behavior-changing, so visitors need to immediately understand what Charu is, how it shows up, and what the working product surface looks like. Structured product visuals and proof hierarchy create more trust than decorative background art.
  Source: `.pm/research/87-website-world-class-polish.md`

- 2026-04-26: Voice Google Search is registered as a Gemini Live custom tool, while voice call-window CRUD is registered as direct Pipecat functions over `CallWindowService`.
  Rationale: Google Search grounding is server-side in Gemini Live and has no local function callback, while recurring call-window mutations are application-owned state changes that need local validation, persistence, and rematerialization side effects.
  Source: `.pm/research/82-voice-full-tools-parity.md`

- 2026-04-26: Voice context is warmed through Redis/Celery, but the cache stores only JSON-safe system instructions and post-call cleanup state.
  Rationale: ORM rows in pre-call context are not portable cache values. The live call needs fast startup, while cleanup only needs anti-habituation fields, so the cache boundary should stay small and deterministic.
  Source: `.pm/research/84-voice-reliability-feature-closure.md`

- 2026-04-26: Browser voice is explicitly de-scoped at `/ws/live/{session_id}` until a browser audio protocol is specified; Twilio voice remains the production voice path.
  Rationale: Twilio Media Streams and browser microphone streaming have different auth, codec, and playback requirements. A machine-readable unsupported WebSocket response is safer than a missing route or an overclaimed implementation.
  Source: `.pm/research/84-voice-reliability-feature-closure.md`

## Tech Stack

- Python 3.10+
- FastAPI (custom, not ADK built-in)
- Google ADK (google-adk)
- SQLModel (ORM, Pydantic + SQLAlchemy)
- Firebase Admin SDK (auth)
- Twilio Python SDK (messaging)
- PostgreSQL (sessions + business data)
- Pipecat (phone call audio pipeline, phase 2)
- APScheduler (scheduled tasks)

## Deployment

- Google Cloud Run (single container)
- Cloud Run provides HTTPS/TLS, auto-scaling, zero-to-N
- Dev: Google AI Studio (API key, free tier)
- Prod: Vertex AI (Google Cloud project, enterprise features)
- Switch via GOOGLE_GENAI_USE_VERTEXAI env var (no code changes)
