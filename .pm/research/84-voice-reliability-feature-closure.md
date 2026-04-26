# 84. Voice Reliability Feature Closure

Date: 2026-04-26

## Requirement

Close the remaining backend reliability gaps from the AI feature audit:

- deterministic fallback outcome persistence when a voice call ends without a save-outcome tool call
- Redis/Celery prefetch for voice call context before pickup
- service-level call-window rematerialization
- production draft-review notification task
- runtime dependency readiness checks
- explicit handling of minimal-chat streaming/browser-voice scope

## Research Summary

Codemigo knowledge and use-case searches emphasized keeping real-time voice startup work deterministic, caching or precomputing repeated context where latency matters, preserving durable traces of tool outcomes, and evaluating the full production path rather than prompt text alone.

Redis async docs show that `redis.asyncio` clients should be explicitly closed with `aclose()` and support URL-based connection pools via `ConnectionPool.from_url`. This project already uses short-lived async Redis clients for OAuth ephemeral tokens, so voice context cache should follow that pattern.

Celery docs emphasize idempotent tasks when `acks_late=True`; this repo already configures late acknowledgements and worker prefetch multiplier one. New prefetch and draft-review tasks should be safe to rerun, skip terminal rows, and use existing dedup keys.

FastAPI WebSocket docs use `await websocket.accept()`, receive/send methods, and explicit WebSocket close semantics. The browser voice endpoint should therefore either implement a protocol or return an explicit, machine-readable unsupported/de-scoped message instead of silently 404ing.

## Implementation Pattern

- Store only JSON-safe voice context in Redis: the system instruction and the anti-habituation fields needed after cleanup. Do not attempt to cache ORM objects.
- Use `voice_context:{call_log_id}` as the cache key with a short TTL.
- In the Twilio stream endpoint, read Redis first, then fall back to live `prepare_call_context()`.
- In call dispatch, perform a last-chance warm before placing the Twilio call so pickup can hit Redis.
- For post-call cleanup, persist `OutcomeConfidence.NONE` when the model never called the save-outcome tool. Do not overwrite clear/partial outcomes.
- Move rematerialization into `app/services` so dashboard, ADK, voice, and direct service calls all get the same behavior.
- Send draft-review notifications through `OutboundMessageService` using `draft_review:{draft_id}` and `draft_review_overflow:{draft_id}` dedup keys.
- Keep health liveness cheap; add a separate readiness/dependency route for DB/Redis/Celery/env checks.

## Gotchas

- Cached context cannot contain `Task`, `CallLog`, `date`, or `datetime` objects without serialization.
- A draft-review retry after a worker crash may see a dedup hit but an unstamped draft; the task must stamp `draft_review_sent_at` when the dedup row is already sent.
- Rematerialization must happen in the same DB transaction as the call-window edit when possible, but duplicate planned rows must be skipped idempotently.
- Browser audio over WebSocket is not equivalent to Twilio Media Streams; until an audio protocol is specified, expose an explicit de-scoped endpoint rather than claiming support.

## Required Packages

Already present:

- `redis`
- `celery[redis]`
- `fastapi`
- `sqlmodel`

## Relevant Links

- Redis asyncio examples: https://redis.readthedocs.io/en/stable/examples/asyncio_examples.html
- Celery task guide: https://docs.celeryq.dev/en/stable/userguide/tasks.html
- FastAPI WebSockets: https://fastapi.tiangolo.com/advanced/websockets/
