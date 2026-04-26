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
- [x] On-demand calls — user can request an accountability call anytime via WhatsApp ("call me now")
- [x] Weekend mode — tone-only voice prompt mode for local Saturdays/Sundays (planner opt-in behavior remains a future spec)
- [x] Urgent email call — implemented with Gmail scan, per-user opt-in, urgent scoring, quiet hours, cooldown/max-per-day, thread dedupe, and on-demand call scheduling
- [x] Auto-task from emails — implemented with Gmail scan, per-user opt-in, confidence scoring, ignored-sender filtering, thread dedupe, and `TaskService` creation
