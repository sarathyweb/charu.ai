# AI Feature Audit Report Research

Date: 2026-04-25

## Requirement

Audit all completed AI features, identify what is implemented, partial, and missing, and produce a checkbox-based TODO report in `mssing_features_todo.md`.

## Research Inputs

- Codemigo knowledge search: `software feature audit implemented missing checklist report codebase tests specs`
- Codemigo use-case search: `audit AI chatbot agent features implementation tests missing todo markdown checkboxes`
- ADK official documentation index: `https://adk.dev/llms.txt`
- Project specs: `.kiro/specs/accountability-call-onboarding`, `.kiro/specs/full-agent-tools`, `.kiro/specs/minimal-chatbot-app`
- Project planning files: `TODO.md`, `README.md`, `notes.md`
- Implementation surfaces: `app/agents`, `app/voice`, `app/services`, `app/api`, `app/tasks`, `app/models`, `migrations`, `tests`, `website`
- Subagent audits:
  - Documentation and planning audit
  - ADK agents and voice audit
  - Backend services/tasks/API/models audit
  - Test coverage audit

## Key Concepts From Research

- A reliable feature audit should compare promised behavior from specs and documentation against implementation evidence and test coverage.
- AI-agent features need more than unit tests around helpers; they also need route/tool-registration checks, tool invocation tests, transcript or prompt behavior tests, and deterministic evals for core conversational flows.
- ADK-based applications should be audited around agents, tools, sessions, callbacks, streaming, and evaluation surfaces.
- Tool parity matters when the same assistant is exposed through multiple channels. Missing voice tools can make a feature appear complete in chat while unavailable in calls.
- External integrations should be reported separately from local implementation, because tests often mock Twilio, Google APIs, WhatsApp, and LLM responses.

## Code Patterns To Check

- Agent tools registered in `app/agents/productivity_agent/agent.py`
- Voice tools registered by `register_productivity_tools` in `app/voice/tools.py`
- Service methods that back each promised feature
- SQLModel models and migrations for persisted feature state
- FastAPI endpoints and Celery tasks for workflow entry points
- Unit and integration tests for happy paths, edge cases, and mocked external failures
- Frontend pages and frontend tests for user-facing claims

## Findings Summary

Implemented features are strongest around accountability calls, call windows, Twilio voice flow, WhatsApp messaging, onboarding, baseline task capture/completion/listing, Google OAuth, calendar read/time-block creation, Gmail reply workflows, and backend tests.

Partial features include ADK/voice behavior coverage, voice outcome persistence guarantees, dashboard metric correctness, frontend coverage, draft-review notification dispatch, and docs that overstate completed tool scope.

Missing features are concentrated in the full agent tools spec: goals, expanded task CRUD, calendar CRUD, Gmail compose/search/archive/read, voice and ADK tool parity, voice context prefetch, Google API cancellation hardening, and several dashboard correctness fixes.

## Gotchas

- The README claims some full-tool features that are not implemented in services or tool registration.
- `TaskStatus.SNOOZED` and `snoozed_until` exist, but snooze service/tool behavior is missing.
- `CallLog.goal` captures goal text, but there is no dedicated goal model/service/tool system.
- Voice early-disconnect handling is wired in `app/api/voice.py`; it should not be listed as missing.
- The requested output filename is intentionally spelled `mssing_features_todo.md` to match the user request.

## Related Existing Research

The repository already has focused research files for many missing items:

- `.pm/research/57-task-snooze-unsnooze-tools.md`
- `.pm/research/58-list-pending-tasks-voice-agent.md`
- `.pm/research/59-goal-model-and-service.md`
- `.pm/research/60-goal-crud-tools-adk-agent.md`
- `.pm/research/61-goal-crud-tools-voice-agent.md`
- `.pm/research/62-calendar-event-crud-expansion.md`
- `.pm/research/63-gmail-compose-search-expansion.md`
- `.pm/research/64-web-search-voice-agent.md`
- `.pm/research/65-call-window-tools-voice-agent.md`
- `.pm/research/66-agent-tool-parity-registration.md`
- `.pm/research/67-voice-call-context-prefetch.md`
- `.pm/research/68-post-call-whatsapp-recap-all-call-types.md`
- `.pm/research/69-fix-weekly-call-stats-undercounting.md`
- `.pm/research/70-fix-goal-completion-percentage-bias.md`
- `.pm/research/71-fix-dashboard-timezone-handling.md`
- `.pm/research/72-fix-streak-calculation-84-day-cap.md`
- `.pm/research/73-document-google-cloud-live-location.md`
- `.pm/research/74-cancel-leaked-google-api-threads.md`

## Required Packages

No new packages are required for this audit report. Future implementation items should continue to use the existing stack: FastAPI, SQLModel, Alembic, Celery, Redis, ADK, Twilio, Google API clients, pytest, and the existing `website` frontend stack.
