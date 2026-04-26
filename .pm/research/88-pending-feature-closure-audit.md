# Pending Feature Closure Audit

Date: 2026-04-26

## Requirement

Audit the remaining unchecked AI/product pending items, implement confirmed
missing product surfaces, and add deterministic tests before updating the
feature checklist.

## Research Inputs

- Codemigo knowledge search:
  `production feature audit pending chatbot frontend voice pipeline onboarding tests`
- Codemigo use-case search:
  `Next.js chat UI Firebase auth SSE FastAPI tests Vitest React Testing Library`
- Local Next 16 docs:
  `website/node_modules/next/dist/docs/01-app/01-getting-started/03-layouts-and-pages.md`
- Local Next 16 docs:
  `website/node_modules/next/dist/docs/01-app/01-getting-started/05-server-and-client-components.md`
- Local Next 16 docs:
  `website/node_modules/next/dist/docs/01-app/02-guides/authentication.md`
- Local Next 16 docs:
  `website/node_modules/next/dist/docs/01-app/02-guides/forms.md`
- Local Next 16 docs:
  `website/node_modules/next/dist/docs/01-app/03-api-reference/05-config/03-testing/vitest.md`
- Existing frontend-test research:
  `.pm/research/86-website-frontend-tests.md`
- Existing full-tools/eval research:
  `.pm/research/81-deterministic-adk-voice-evals-and-backlog.md`
  and `.pm/research/84-voice-reliability-feature-closure.md`

## Audit Findings

- The root and website git worktrees were clean before this pass.
- Backend dashboard APIs and website dashboard parity were already implemented.
- The active missing product surface from the minimal chatbot spec was web
  chat UI exposure. The repository's production frontend is `website`, not a
  separate `frontend` app, so the right implementation target is an
  authenticated `website` chat route.
- The remaining backend gaps are mostly test coverage gaps: onboarding
  conversation contracts, WhatsApp draft approval through the real route,
  voice stream cleanup integration, and semantic eval assertions.
- The onboarding product decision remains intentionally strict: setup requires
  name, timezone, three call windows, and Calendar/Gmail connection before
  production completion. Skip/decline paths should be a new product spec
  because they change call scheduling guarantees and Google-tool availability.
- Deferred backlog items such as urgent-email proactive calls, auto-task from
  emails, Notion, Keep, Google Tasks, Todoist, and embedding dedupe remain
  explicitly deferred until product specs define opt-in, privacy, dedupe,
  integration, and cost behavior.

## Implementation Patterns

- Use a focused client component for the chat page because it needs local
  state, event handlers, scrolling, and streaming response updates.
- Keep the authenticated API boundary in `authFetch`, and put chat-specific
  parsing in a small `chatApi` module.
- Parse SSE by event name (`session`, `delta`, `done`, `error`) so the UI can
  evolve from final-text chunks to token-level deltas without changing page
  code.
- Test client surfaces with Vitest and React Testing Library by mocking API
  modules and auth state.
- Test FastAPI routes through `ASGITransport` while overriding only external
  dependencies, so route-level branching remains exercised.

## Gotchas

- Next App Router pages are Server Components by default; chat UI needs a
  `"use client"` boundary.
- The backend `/api/chat/stream` currently emits a stable SSE contract with
  final text as a delta. The frontend should support incremental deltas anyway.
- `window.location` is awkward to assert in jsdom; integration tests should
  prioritize request path and mutation behavior unless navigation itself is
  wrapped.
- Voice stream tests need a non-null first user utterance timestamp or the
  early-disconnect detector intentionally treats the call as missed.

## Required Packages

No new packages are required. The website already has Next, React, Firebase,
Heroicons, Vitest, jsdom, and React Testing Library installed.
