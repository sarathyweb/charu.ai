# Missing Features TODO / AI Feature Audit

Generated: 2026-04-25

This audit compares the specs, `TODO.md`, `README.md`, implementation, tests, and multiple subagent reviews. Checked items are implemented in the codebase. Unchecked items are partial, missing, or need verification before they should be treated as production-complete.

## Audit Sources

- [x] Reviewed planning docs: `TODO.md`, `README.md`, `notes.md`
- [x] Reviewed specs: `.kiro/specs/accountability-call-onboarding`, `.kiro/specs/full-agent-tools`, `.kiro/specs/minimal-chatbot-app`
- [x] Reviewed backend surfaces: `app/agents`, `app/voice`, `app/services`, `app/api`, `app/tasks`, `app/models`, `migrations`
- [x] Reviewed frontend surface: `website`
- [x] Reviewed test coverage under `tests`
- [x] Used subagents for documentation/planning, agents/voice, backend services/API/tasks/models, and tests
- [x] Wrote supporting research: `.pm/research/76-ai-feature-audit-report.md`, `.pm/research/78-task-tool-parity-implementation.md`, `.pm/research/79-goal-model-service-tools-implementation.md`, `.pm/research/80-calendar-gmail-full-tools-expansion.md`, `.pm/research/82-voice-full-tools-parity.md`, `.pm/research/83-dashboard-api-feature-closure.md`, `.pm/research/84-voice-reliability-feature-closure.md`

## Implemented / Working Features

- [x] FastAPI backend application exists with health, auth sync, chat, WhatsApp, Google OAuth, voice, and dashboard routes.
- [x] SQLModel-backed user, session, call, call-window, task, integration, outbound message, and email draft persistence exists.
- [x] ADK database session service is wired for chat/session persistence.
- [x] Firebase phone auth sync backend exists, and the website has a login/onboarding entry path.
- [x] WhatsApp inbound webhook exists with Twilio signature validation, idempotency handling, onboarding/check-in context, and draft approval routing.
- [x] WhatsApp outbound messaging exists with template/freeform send paths, message splitting, deduplication, and 24-hour-window behavior.
- [x] Root ADK productivity agent is implemented and exported as `root_agent`.
- [x] Onboarding ADK agent is implemented for name, timezone, call windows, Google OAuth handoff, and finalization.
- [x] Onboarding service persists name, timezone, wake/sleep preferences, WhatsApp opt-in, and call windows.
- [x] User preference hydration and write-through tools exist for onboarding and productivity context.
- [x] Call-window model, service, validation, and ADK call-window tools exist.
- [x] Task management exists: save task, fuzzy duplicate detection, complete task by title, list pending tasks, update, delete, snooze, unsnooze, and list completed tasks.
- [x] Task model and pg_trgm migration exist.
- [x] Core call/accountability system exists: `CallWindow`, `CallLog`, scheduling, dispatch, retries, Twilio status callbacks, AMD handling, and call management service.
- [x] Celery call planner, catch-up scheduling, due-call dispatcher, trigger-call task, and stale-call sweeper exist.
- [x] Twilio voice stream endpoint exists with stream token validation and call-context creation.
- [x] Voice early-disconnect detection is wired in the endpoint.
- [x] Pipecat/Gemini Live voice pipeline exists with transcript capture, call timer, tool registration, and cleanup hooks.
- [x] Voice tools exist for saving morning/afternoon call outcomes, evening call outcomes, saving tasks, listing pending tasks, updating tasks, deleting tasks, snoozing tasks, unsnoozing tasks, completing tasks, creating/listing/updating/completing/abandoning/deleting goals, Calendar today/range reads, Calendar event CRUD, Calendar task time blocks, Gmail search/read/compose/archive/reply-draft flows, scheduling callbacks, skipping calls, rescheduling calls, getting the next call, and canceling today's calls.
- [x] Structured call-outcome persistence exists for morning, afternoon, evening, and on-demand call types.
- [x] Transcript storage and retention metadata exist.
- [x] Post-call cleanup exists for call state finalization, transcript persistence, fallback outcome persistence, recap dispatch, draft-review dispatch, and anti-habituation update.
- [x] Post-call recaps exist for non-evening calls, including on-demand calls through the generic post-call recap path.
- [x] Evening recap task exists.
- [x] Midday check-in task exists.
- [x] Weekly summary task exists.
- [x] Missed-call encouragement flow exists.
- [x] Anti-habituation service exists for opener/approach variation, streak tracking, and jitter.
- [x] Google OAuth connection flow exists with encrypted token storage and refresh wrapper.
- [x] Calendar read support exists for today's events and date ranges.
- [x] Calendar write support exists for finding available gaps, creating task time blocks, and creating/updating/deleting general events.
- [x] Gmail read support exists for emails needing reply, searching inbox, reading the top query match, and fetching a selected email for reply.
- [x] Gmail write support exists for composing new emails, archiving messages, and draft-reviewed reply send flows with duplicate-send prevention.
- [x] Dashboard API exists for summary metrics, tasks, schedule, profile, progress history, and integrations.
- [x] Website dashboard exists under `website`, with dashboard, login, onboarding, and integrations pages.
- [x] Backend test suite is broad and currently passes with `817 passed, 1 skipped, 73 warnings`.

## Partial / Needs Verification

- [x] Add deterministic ADK evals for root-agent routing, sub-agent handoff, tool selection, and tool argument quality.
- [ ] Add true conversation-level tests for onboarding; current coverage is stronger around services/tools than complete ADK flow.
- [ ] Add voice pipeline integration tests that exercise frames, Gemini Live behavior, barge-in/interruption, tool calls, and cleanup together.
- [x] Guarantee voice outcome persistence when the LLM does not call the expected save-outcome tool before disconnect; cleanup now persists explicit `none` confidence instead of leaving null outcomes.
- [ ] Decide whether onboarding must force all three call windows and both Google integrations, or add explicit skip/decline paths.
- [x] Fixed dashboard Google integration connect flow so the frontend uses Bearer auth and receives an OAuth URL before navigating.
- [x] Reconciled `README.md` claims with actual implemented tool scope.
- [ ] Add frontend tests for the `website` app; no first-party frontend tests were found.
- [x] Verify production runtime dependencies end to end via `/health/ready` readiness checks for DB, Redis, Celery worker, required environment variables, Firebase path, and Twilio template SIDs.
- [x] Strengthen call-window rematerialization; service-level code now rematerializes planned calls for onboarded users through a shared materialization helper.
- [x] Make draft-review notification dispatch production-complete with `app/tasks/draft_review.py`, WhatsApp template/freeform overflow sends, dedup keys, and sent-at stamping.
- [x] Add opt-in staging smoke checks for deployed health/readiness via `tests/smoke/test_staging_smoke.py`.

## Missing / TODO From Completed AI Scope

- [x] Implement task update support in `TaskService`, ADK tools, voice tools, and tests.
- [x] Implement task delete support in `TaskService`, ADK tools, voice tools, and tests.
- [x] Implement task snooze support in `TaskService`, ADK tools, voice tools, and tests.
- [x] Implement task unsnooze support in `TaskService`, ADK tools, voice tools, and tests.
- [x] Add `list_pending_tasks` to voice tools for parity with the ADK task tools.
- [x] Add task update/delete/snooze/unsnooze API and dashboard paths outside ADK/voice tools.
- [x] Implement a dedicated goal model, status enum, migration, and service instead of relying only on `CallLog.goal` text.
- [x] Implement goal create/read/update/delete tools for the ADK productivity agent.
- [x] Implement goal create/read/update/delete tools for the voice agent.
- [x] Add goal endpoints or dashboard support outside calls.
- [x] Implement calendar date-range read support.
- [x] Implement general calendar event creation.
- [x] Implement calendar event update.
- [x] Implement calendar event deletion.
- [x] Register calendar CRUD/date-range tools with the ADK agent.
- [x] Register calendar CRUD/date-range tools with the voice agent if calls should support calendar operations.
- [x] Implement Gmail new-email compose/send support.
- [x] Implement Gmail inbox/search support.
- [x] Implement Gmail archive support.
- [x] Implement Gmail read-by-query or read-by-id support outside the reply-only workflow.
- [x] Register expanded Gmail tools with the ADK agent.
- [x] Register expanded Gmail tools with the voice agent if calls should support email operations.
- [x] Add voice Google Search/web-search support if voice is expected to match chat search behavior.
- [x] Add voice call-window CRUD tools for recurring call schedule management.
- [x] Run and enforce ADK/voice tool parity checks so promised tools are registered in every intended channel.
- [x] Implement voice call-context prefetch with Redis/Celery so pickup does not depend only on live database/context assembly.
- [x] Harden Google API cancellation/thread behavior with bounded concurrency and per-attempt timeout handling.
- [x] Added `GOOGLE_CLOUD_LIVE_LOCATION` to `.env.example`.
- [x] Implement `app/tasks/draft_review.py` and wire draft-review WhatsApp notification dispatch as a real task.
- [x] Add outbound-message fencing tokens and stale-token/stale-sending regression tests.
- [x] Fixed dashboard weekly call count to count completed scheduled call rows, not active dates.
- [x] Fixed dashboard weekly call denominator so it uses scheduled/active windows instead of hard-coded `7`.
- [x] Fixed dashboard goal-completion percentage to count only goal-capable morning/afternoon calls with clear or partial completion semantics.
- [x] Fixed dashboard date boundaries to use each user's timezone instead of server-local `date.today()`.
- [x] Fixed dashboard streak calculation so best streak is not capped by the 84-day heatmap window.
- [x] Added tests for dashboard metrics: weekly calls, goal percentage, timezone boundaries, and long streaks.
- [ ] Implement the web chat frontend if `.kiro/specs/minimal-chatbot-app` is still in scope; current UI lives in `website`, not `frontend`.
- [x] Implement `/api/chat/stream` SSE for the minimal chatbot spec.
- [x] Explicitly de-scope browser voice `/ws/live/{session_id}` with a machine-readable unsupported WebSocket response; Twilio voice remains the active production voice path.
- [x] Add dashboard call history API path.
- [x] Add dashboard settings editing API path.
- [x] Add dashboard connection-management polish through authenticated connect/disconnect OAuth paths.
- [x] Explicitly defer hybrid embedding-based task deduplication beyond pg_trgm/fuzzy matching until embedding provider, dimensions, threshold, cost, and privacy behavior are specified.
- [x] Add weekend mode as tone-only local-weekend voice guidance.
- [x] Explicitly defer urgent-email proactive call behavior until escalation rules, opt-in, quiet hours, rate limits, and thread dedupe are specified.
- [x] Explicitly defer auto-task creation from emails until extraction confidence, review policy, ignored senders, and message/thread tracking are specified.
- [x] Defer Notion integration because it is not in active scope; current docs describe it as deferred until OAuth/workspace mapping is specified.
- [x] Defer Google Keep integration because it is not in active scope; current docs describe it as deferred until API viability and mapping are specified.
- [x] Defer Google Tasks/Todoist import because it is not in active scope; current implementation evidence was not found and import semantics need specs.
- [x] Revisited deferred backlog scope and captured product decisions/next steps in `tests/evals/deferred_product_backlog_review.json`.

## Test Coverage TODO

- [x] Ran backend verification: `uv run pytest -q` passed with `817 passed, 1 skipped, 73 warnings`.
- [x] Ran frontend verification: `npm run build` passed for the website app.
- [x] Add ADK eval datasets for core scenarios: onboarding completion, task capture, task completion, calendar scheduling, Gmail reply, call management, and refusal/error handling.
- [x] Add tests that assert task-tool registration and required ADK schemas for update/delete/snooze/unsnooze/list-pending parity.
- [x] Add tests that assert voice task-tool registration and callback payloads for update/delete/snooze/unsnooze/list-pending parity.
- [x] Add tests for GoalService lifecycle behavior plus ADK and voice goal CRUD tool payloads.
- [x] Add tests that assert exact root-agent tool registration against the full-tools spec.
- [x] Add tests that assert exact voice-tool registration against the full-tools spec.
- [ ] Add prompt/tool behavior tests that check semantic outputs instead of only substring presence.
- [x] Add Gmail tests for search, read, compose, and archive once implemented.
- [x] Add Calendar tests for date-range reads and event CRUD once implemented.
- [ ] Add WhatsApp draft-approval endpoint tests through the real route, not only lower-level helpers.
- [ ] Add frontend component or Playwright tests for dashboard, onboarding, integrations, and future chat/voice surfaces.
- [x] Add opt-in staging smoke tests for deployed readiness; live Twilio/Google/Gemini transaction checks remain gated by staging credentials.

## Recommended Priority Order

- [x] First fix small correctness/documentation gaps: `.env.example`, dashboard metrics, integration connect auth mismatch, and README overclaims.
- [x] Then complete task-tool parity: update, delete, snooze, unsnooze, and voice `list_pending_tasks`.
- [x] Then implement the goal model/service/tools, because goals are core to accountability-call semantics.
- [x] Then expand Calendar and Gmail tools to match the full-tools spec.
- [x] Then close voice/ADK parity gaps for voice Google Search, call-window voice tools, and exact parity checks.
- [x] Then close the remaining voice prefetch-context gap.
- [x] Then add deterministic ADK and voice evals before marking AI behavior as production-complete.
- [x] Finally revisit deferred product backlog items: weekend mode, urgent-email calls, auto-task from emails, Notion, Keep, Google Tasks, and Todoist.
