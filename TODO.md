# TODO — Future Plans

## Web Dashboard
- [ ] Progress dashboard — let users view call streaks, goals set vs completed, weekly trends
- [ ] Task list view — browse and manage tasks the agent has collected across conversations
- [ ] Call history — view past call summaries and recaps
- [ ] Settings — update call window, timezone, notification preferences from the web
- [ ] Connection management UI on web dashboard — connect/disconnect integrations

## Task Management Enhancements
- [ ] Hybrid embedding dedup — add pgvector column to tasks table, compute embeddings async (Celery) after save, run second-pass semantic dedup in background. pg_trgm stays as the fast synchronous gate; embeddings catch semantic duplicates like "reply to Sarah" vs "email Sarah back" that trigrams miss. Non-blocking — user gets instant feedback, merge happens silently seconds later.

## Product Evolution
- [zz] On-demand calls — user can request an accountability call anytime via WhatsApp ("call me now")
- [x] Weekend mode — tone-only voice prompt mode for local Saturdays/Sundays (planner opt-in behavior remains a future spec)
- [ ] Urgent email call — AI monitors Gmail for urgent emails and proactively calls the user if they haven't responded, so nothing critical slips through the cracks
- [ ] Auto-task from emails — AI automatically adds pending email replies and follow-ups to the user's task list, so they get surfaced during accountability calls
