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
- [x] Wrote supporting research: `.pm/research/76-ai-feature-audit-report.md`, `.pm/research/78-task-tool-parity-implementation.md`, `.pm/research/79-goal-model-service-tools-implementation.md`

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
- [x] Voice tools exist for saving morning/afternoon call outcomes, evening call outcomes, saving tasks, listing pending tasks, updating tasks, deleting tasks, snoozing tasks, unsnoozing tasks, completing tasks, creating/listing/updating/completing/abandoning/deleting goals, scheduling callbacks, skipping calls, rescheduling calls, getting the next call, and canceling today's calls.
- [x] Structured call-outcome persistence exists for morning, afternoon, evening, and on-demand call types.
- [x] Transcript storage and retention metadata exist.
- [x] Post-call cleanup exists for call state finalization, transcript persistence, recap dispatch, draft-review dispatch attempt, and anti-habituation update.
- [x] Post-call recaps exist for non-evening calls, including on-demand calls through the generic post-call recap path.
- [x] Evening recap task exists.
- [x] Midday check-in task exists.
- [x] Weekly summary task exists.
- [x] Missed-call encouragement flow exists.
- [x] Anti-habituation service exists for opener/approach variation, streak tracking, and jitter.
- [x] Google OAuth connection flow exists with encrypted token storage and refresh wrapper.
- [x] Calendar read support exists for today's events.
- [x] Calendar write support exists for finding available gaps and creating task time blocks.
- [x] Gmail read support exists for emails needing reply and fetching a selected email for reply.
- [x] Gmail reply workflow exists with draft creation/update, WhatsApp review, approval, duplicate-send prevention, and send-approved-reply support.
- [x] Dashboard API exists for summary metrics, tasks, schedule, profile, progress history, and integrations.
- [x] Website dashboard exists under `website`, with dashboard, login, onboarding, and integrations pages.
- [x] Backend test suite is broad and currently passes with `763 passed, 59 warnings`.

## Partial / Needs Verification

- [ ] Add deterministic ADK evals for root-agent routing, sub-agent handoff, tool selection, and tool argument quality.
- [ ] Add true conversation-level tests for onboarding; current coverage is stronger around services/tools than complete ADK flow.
- [ ] Add voice pipeline integration tests that exercise frames, Gemini Live behavior, barge-in/interruption, tool calls, and cleanup together.
- [ ] Guarantee voice outcome persistence when the LLM does not call the expected save-outcome tool before disconnect; cleanup currently cannot prove the outcome exists in all cases.
- [ ] Decide whether onboarding must force all three call windows and both Google integrations, or add explicit skip/decline paths.
- [x] Fixed dashboard Google integration connect flow so the frontend uses Bearer auth and receives an OAuth URL before navigating.
- [x] Reconciled `README.md` claims with actual implemented tool scope.
- [ ] Add frontend tests for the `website` app; no first-party frontend tests were found.
- [ ] Verify production runtime dependencies end to end: Celery worker, Celery beat, Redis, Twilio webhooks, Google OAuth, Gemini Live, WhatsApp templates, and environment variables.
- [ ] Strengthen call-window rematerialization; service-level code still has a TODO and relies on higher-level catch-up behavior for future planned calls.
- [ ] Make draft-review notification dispatch production-complete; cleanup references a missing `app/tasks/draft_review.py` task.
- [ ] Add real external-integration smoke tests or staging checks for Twilio, WhatsApp, Google Calendar, Gmail, and Gemini Live.

## Missing / TODO From Completed AI Scope

- [x] Implement task update support in `TaskService`, ADK tools, voice tools, and tests.
- [x] Implement task delete support in `TaskService`, ADK tools, voice tools, and tests.
- [x] Implement task snooze support in `TaskService`, ADK tools, voice tools, and tests.
- [x] Implement task unsnooze support in `TaskService`, ADK tools, voice tools, and tests.
- [x] Add `list_pending_tasks` to voice tools for parity with the ADK task tools.
- [ ] Add task update/delete/snooze/unsnooze API and dashboard paths if task mutation should be available outside ADK/voice tools.
- [x] Implement a dedicated goal model, status enum, migration, and service instead of relying only on `CallLog.goal` text.
- [x] Implement goal create/read/update/delete tools for the ADK productivity agent.
- [x] Implement goal create/read/update/delete tools for the voice agent.
- [ ] Add goal endpoints or dashboard support if goals are intended to be user-visible outside calls.
- [ ] Implement calendar date-range read support.
- [ ] Implement general calendar event creation.
- [ ] Implement calendar event update.
- [ ] Implement calendar event deletion.
- [ ] Register calendar CRUD/date-range tools with the ADK agent.
- [ ] Register calendar CRUD/date-range tools with the voice agent if calls should support calendar operations.
- [ ] Implement Gmail new-email compose/send support.
- [ ] Implement Gmail inbox/search support.
- [ ] Implement Gmail archive support.
- [ ] Implement Gmail read-by-query or read-by-id support outside the reply-only workflow.
- [ ] Register expanded Gmail tools with the ADK agent.
- [ ] Register expanded Gmail tools with the voice agent if calls should support email operations.
- [ ] Add voice Google Search/web-search support if voice is expected to match chat search behavior.
- [ ] Run and enforce ADK/voice tool parity checks so promised tools are registered in every intended channel.
- [ ] Implement voice call-context prefetch with Redis/Celery so pickup does not depend only on live database/context assembly.
- [ ] Harden Google API cancellation/thread behavior; no cancellation event or equivalent guard was found.
- [x] Added `GOOGLE_CLOUD_LIVE_LOCATION` to `.env.example`.
- [ ] Implement `app/tasks/draft_review.py` and wire draft-review WhatsApp notification dispatch as a real task.
- [ ] Add outbound-message fencing tokens if duplicate sends can still race across workers.
- [x] Fixed dashboard weekly call count to count completed scheduled call rows, not active dates.
- [x] Fixed dashboard weekly call denominator so it uses scheduled/active windows instead of hard-coded `7`.
- [x] Fixed dashboard goal-completion percentage to count only goal-capable morning/afternoon calls with clear or partial completion semantics.
- [x] Fixed dashboard date boundaries to use each user's timezone instead of server-local `date.today()`.
- [x] Fixed dashboard streak calculation so best streak is not capped by the 84-day heatmap window.
- [x] Added tests for dashboard metrics: weekly calls, goal percentage, timezone boundaries, and long streaks.
- [ ] Implement the web chat frontend if `.kiro/specs/minimal-chatbot-app` is still in scope; current UI lives in `website`, not `frontend`.
- [ ] Implement or explicitly de-scope `/api/chat/stream` SSE if the minimal chatbot spec still requires streaming chat.
- [ ] Implement or explicitly de-scope browser voice `/ws/live/{session_id}` if the minimal chatbot spec still requires web voice.
- [ ] Add dashboard call history view.
- [ ] Add dashboard settings editing.
- [ ] Add dashboard connection-management polish beyond the current integrations page.
- [ ] Add hybrid embedding-based task deduplication if still desired beyond pg_trgm/fuzzy matching.
- [ ] Add weekend mode.
- [ ] Add urgent-email proactive call behavior.
- [ ] Add auto-task creation from emails.
- [ ] Add Notion integration only if it is still in active scope; current docs describe it as deferred.
- [ ] Add Google Keep integration only if it is still in active scope; current docs describe it as deferred.
- [ ] Add Google Tasks/Todoist import only if it is still in active scope; current implementation evidence was not found.

## Test Coverage TODO

- [x] Ran backend verification: `uv run pytest -q` passed with `763 passed, 59 warnings`.
- [x] Ran frontend verification: `npm run build` passed for the website app.
- [ ] Add ADK eval datasets for core scenarios: onboarding completion, task capture, task completion, calendar scheduling, Gmail reply, call management, and refusal/error handling.
- [x] Add tests that assert task-tool registration and required ADK schemas for update/delete/snooze/unsnooze/list-pending parity.
- [x] Add tests that assert voice task-tool registration and callback payloads for update/delete/snooze/unsnooze/list-pending parity.
- [x] Add tests for GoalService lifecycle behavior plus ADK and voice goal CRUD tool payloads.
- [ ] Add tests that assert exact root-agent tool registration against the full-tools spec.
- [ ] Add tests that assert exact voice-tool registration against the full-tools spec.
- [ ] Add prompt/tool behavior tests that check semantic outputs instead of only substring presence.
- [ ] Add Gmail tests for search, read, compose, archive, inbound AI draft generation, and review notification once implemented.
- [ ] Add Calendar tests for date-range reads and event CRUD once implemented.
- [ ] Add WhatsApp draft-approval endpoint tests through the real route, not only lower-level helpers.
- [ ] Add frontend component or Playwright tests for dashboard, onboarding, integrations, and future chat/voice surfaces.
- [ ] Add staging smoke tests for Twilio voice, WhatsApp messages, Google OAuth, Calendar, Gmail, and Gemini Live.

## Recommended Priority Order

- [x] First fix small correctness/documentation gaps: `.env.example`, dashboard metrics, integration connect auth mismatch, and README overclaims.
- [x] Then complete task-tool parity: update, delete, snooze, unsnooze, and voice `list_pending_tasks`.
- [x] Then implement the goal model/service/tools, because goals are core to accountability-call semantics.
- [ ] Then expand Calendar and Gmail tools to match the full-tools spec.
- [ ] Then close voice/ADK parity gaps: voice Google Search, calendar/Gmail, and prefetch context.
- [ ] Then add deterministic ADK and voice evals before marking AI behavior as production-complete.
- [ ] Finally revisit deferred product backlog items: weekend mode, urgent-email calls, auto-task from emails, Notion, Keep, Google Tasks, and Todoist.
