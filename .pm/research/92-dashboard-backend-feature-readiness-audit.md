# Dashboard + Backend Feature Readiness Audit

Date: 2026-04-26

## Requirement

Verify that completed backend AI/product features are actually exposed through the
website dashboard, backed by deterministic tests, and not overclaimed by docs.

## Research Inputs

- Codemigo knowledge search: `software feature completion audit backend frontend dashboard parity testing release readiness`
- Codemigo use-case search: `audit implemented features against specs tests dashboard backend API parity checklist`
- Official Pydantic documentation: model field-set / unset tracking for partial updates.
- Official Celery documentation: `apply_async` supports ETA/countdown scheduling for delayed jobs.
- Google OAuth documentation: Google OAuth clients should use well-debugged OAuth libraries and can revoke tokens through Google's OAuth revocation flow.
- Existing project research: `85-website-dashboard-ui-api-parity.md`, `86-website-frontend-tests.md`, `84-voice-reliability-feature-closure.md`, `90-email-automation-urgent-calls-auto-tasks.md`.

## Key Concepts

- Feature completion must be evaluated across product surfaces, not only backend
  APIs. A backend endpoint is not customer-complete until the website exposes the
  workflow and tests pin the expected request paths and payloads.
- Partial update APIs must distinguish omitted fields from explicit `null`.
  Pydantic v2 exposes the set of fields provided by the caller, which is the
  right contract for clearing optional dashboard fields such as goal description
  and target date.
- Scheduled voice-context prefetch should be keyed by call identity and scheduled
  time. This prevents an old cache entry from being reused after reschedules, and
  a 30-minute TTL bounds Redis growth.
- OAuth disconnect should separate local scope removal from provider revocation.
  Revoking the refresh token is correct only when no remaining Google service
  needs that token.
- Test infrastructure is part of product readiness. A full-suite failure caused
  by schema setup churn invalidates any "production-complete" claim even when
  focused feature tests pass.

## Code Patterns

- Dashboard API wrapper first, component second: add typed helpers in
  `website/src/lib/dashboardApi.ts`, then use those helpers from dashboard and
  integrations pages.
- Explicit clears:
  - API request uses `request.model_fields_set`.
  - Service receives `update_fields`.
  - Service only clears optional fields when the client explicitly sent them.
- Voice prefetch:
  - Record materialized calls on `session.info`.
  - Enqueue after commit where possible.
  - Include `scheduled_time` in both task kwargs and Redis key.
  - Skip stale task execution when DB `scheduled_time` no longer matches the task
    argument.
- Gmail automation UI:
  - Fetch `/api/integrations`.
  - Disable urgent-email and auto-task toggles when Gmail is disconnected.
  - Provide a Gmail connect action next to the blocked controls.

## Gotchas

- `datetime-local` inputs expect local wall-clock values, not ISO UTC strings.
  Using `toISOString().slice(0, 16)` shifts the displayed value for users outside
  UTC.
- In tests, broker publishes must be disabled unless the test explicitly covers
  enqueue behavior; otherwise service-level call creation can accidentally depend
  on Redis availability.
- Provider OAuth revocation should not run when disconnecting Gmail while Calendar
  still has scopes, because both services share the stored Google refresh token.
- Full-suite DB isolation should not repeatedly create/drop tables in a shared
  dirty schema without resetting orphaned PostgreSQL types.

## Required Packages

- Existing: `fastapi`, `sqlmodel`, `pydantic`, `celery`, `redis`, `google-auth-oauthlib`, `google-api-python-client`.
- Existing website: `next`, `react`, `vitest`, `@testing-library/react`.
- No new package required.

## Decisions

- Treat dashboard integrations as part of the dashboard surface, not only a
  separate `/integrations` page.
- Keep email automation preferences under settings, but gate them by Gmail
  connection status to avoid presenting toggles that the backend will skip.
- Keep transcript retention local-file aware because current voice cleanup writes
  JSON files under `TRANSCRIPT_DIR`.
- Disable voice prefetch broker dispatch during tests via
  `VOICE_CONTEXT_PREFETCH_ENABLED=false`; unit tests opt in when validating ETA
  construction.

## Verification Plan

- Backend targeted tests for goal clears, call-me-now, OAuth disconnect,
  prefetch keying/enqueue, transcript cleanup, dashboard routes.
- Backend full suite: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -q -p no:cacheprovider`.
- Website tests: `npm test`.
- Website lint/build: `npm run lint`, `npm run build`.
