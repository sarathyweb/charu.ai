# TODO — Future Plans

## Web Dashboard
- [x] Progress dashboard — let users view call streaks, goals set vs completed, weekly trends
- [x] Task list view — browse and manage tasks the agent has collected across conversations
- [x] Call history — view past call summaries and recaps
- [x] Settings — update call window, timezone, notification preferences from the web
- [x] Connection management UI on web dashboard — connect/disconnect integrations
- [x] Authenticated web chat — browser chat surface backed by the backend chat API

## Task Management Enhancements
- [x] Hybrid embedding dedup — implemented as an opt-in Azure OpenAI semantic fallback after pg_trgm using `text-embedding-3-large`, configurable threshold/backfill limits, JSONB task embeddings, and deterministic tests.

## Product Evolution
- [zz] On-demand calls — user can request an accountability call anytime via WhatsApp ("call me now")
- [x] Weekend mode — tone-only voice prompt mode for local Saturdays/Sundays (planner opt-in behavior remains a future spec)
- [x] Urgent email call — deferred until opt-in, escalation rules, quiet hours, rate limits, and thread dedupe are specified
- [x] Auto-task from emails — deferred until extraction confidence, review policy, ignored senders, and message/thread tracking are specified
