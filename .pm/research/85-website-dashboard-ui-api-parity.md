# Website dashboard UI/API parity

## Requirement

Expose the completed dashboard backend APIs in the customer website UI:

- Tasks: list pending/completed/snoozed plus update, delete, complete, snooze, and unsnooze.
- Goals: list/create/update/complete/abandon/delete.
- Call history: recent calls with basic filters.
- Settings: editable profile preferences and recurring call-window CRUD.

## Research

### Codemigo knowledge/usecase

- Dashboard UI should reuse a small number of consistent list, card, and form patterns to reduce cognitive load.
- Profile/settings content that users revisit should support section-level editing instead of becoming one long undifferentiated form.
- Standard controls are preferred for common interactions: text inputs for text, selects for finite options, time inputs for clock windows, and buttons for clear commands.
- Interactive dashboard rows should match their purpose: read-only lists only when data cannot be changed; row-level editing when users need targeted updates.
- Test the interface with realistic content and repeated rows so truncation, empty states, and actions stay understandable.

### Backend contract

The dashboard API is implemented in `app/api/dashboard.py`:

- `GET /api/tasks?status=pending|completed|snoozed`
- `PATCH /api/tasks/{task_id}`
- `POST /api/tasks/{task_id}/complete`
- `POST /api/tasks/{task_id}/snooze`
- `POST /api/tasks/{task_id}/unsnooze`
- `DELETE /api/tasks/{task_id}`
- `GET /api/goals?status=active|completed|abandoned`
- `POST /api/goals`
- `PATCH /api/goals/{goal_id}`
- `POST /api/goals/{goal_id}/complete`
- `POST /api/goals/{goal_id}/abandon`
- `DELETE /api/goals/{goal_id}`
- `GET /api/call-history`
- `GET /api/user/profile`
- `PATCH /api/user/profile`
- `GET /api/call-windows`
- `POST /api/call-windows`
- `PATCH /api/call-windows/{window_type}`
- `DELETE /api/call-windows/{window_type}`

Important request/response details:

- Task priority is `0..100`.
- Task snooze requires a timezone-aware datetime; the website can send `Date.toISOString()`.
- Goal create/update accepts `title`, optional `description`, and optional `target_date`.
- Profile update accepts `name` and `timezone`; timezone must be an IANA identifier.
- Call-window create/update uses `HH:MM` 24-hour strings and `window_type` of `morning`, `afternoon`, or `evening`.
- Current `GET /api/call-windows` returns display strings such as `8:00 AM`, while create/update returns `HH:MM`; the UI should refresh from GET after mutations.

## Design notes

- Keep the dashboard as the single first-screen app surface, expanding existing sections instead of adding a parallel settings route.
- Add missing sections to the sticky section nav: goals, call history, and settings.
- Fetch all dashboard resources on load and after successful mutations, so UI state stays aligned with backend side effects such as call rematerialization.
- Prefer focused row-level editing for tasks and goals.
- Treat destructive actions as confirmed actions.

## Gotchas

- Do not assume call-window display times are valid `<input type="time">` values; convert display strings to `HH:MM` for edit controls.
- Empty PATCH payloads are rejected by the backend, so only send changed/provided fields.
- Goal `target_date` currently cannot be cleared through the API because `null` is indistinguishable from omitted in the service contract; keep existing values unless a new one is supplied.
- `authFetch` does not add `Content-Type` automatically; JSON mutations need explicit headers.

## Relevant links

- Official Next.js local docs: `website/node_modules/next/dist/docs/01-app/02-guides/testing/vitest.md`
- Official Next.js local docs: `website/node_modules/next/dist/docs/01-app/02-guides/testing/index.md`
- Official Next.js local docs: `website/node_modules/next/dist/docs/01-app/01-getting-started/07-mutating-data.md`

## Required packages

No new runtime packages are required for the UI parity work.
