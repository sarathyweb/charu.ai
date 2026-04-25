# 80 - Calendar And Gmail Full-Tools Expansion

## Requirement Covered

Implement the checklist item:

- Calendar date-range read support
- Calendar event create, update, and delete tools
- Gmail new-email compose/send
- Gmail inbox/search support
- Gmail archive support
- Gmail read-by-query support
- ADK and voice registration for the expanded Calendar/Gmail tool set

This covers Requirements 8, 9, and the Calendar/Gmail portions of
Requirement 12 in `.kiro/specs/full-agent-tools/requirements.md`.

## Research Inputs

- Codemigo knowledge search:
  `Google Calendar Gmail CRUD tools AI agent ADK SQLModel service design`
- Codemigo use-case search:
  `implement calendar event CRUD gmail search send archive ADK voice tools tests`
- ADK docs index: `https://adk.dev/llms.txt`
- ADK action confirmation docs: `https://adk.dev/tools-custom/confirmation/`
- Google Calendar API docs:
  - `https://developers.google.com/workspace/calendar/api/guides/create-events`
  - `https://developers.google.com/resources/api-libraries/documentation/calendar/v3/python/latest/calendar_v3.events.html`
- Gmail API docs:
  - `https://googleapis.github.io/google-api-python-client/docs/dyn/gmail_v1.users.messages.html`
- Local code:
  - `app/services/google_api_wrapper.py`
  - `app/services/google_calendar_read_service.py`
  - `app/services/google_calendar_write_service.py`
  - `app/services/gmail_read_service.py`
  - `app/services/gmail_write_service.py`
  - `app/agents/productivity_agent/google_tools.py`
  - `app/voice/tools.py`

## Key Concepts

- Calendar event CRUD should use the Calendar `events` resource:
  `events.list`, `events.insert`, `events.patch`, and `events.delete`.
- Date-range calendar reads should reuse the existing filtering behavior:
  remove cancelled events and events declined by the authenticated user.
- General calendar events are separate from Charu task time blocks. Time blocks
  keep deterministic IDs and the `charuai=time_block` metadata, while general
  events let Google generate IDs.
- Gmail search/read/compose/archive should use `users.messages.list`,
  `users.messages.get`, `users.messages.send`, and `users.messages.modify`.
- Archiving a Gmail message means removing the `INBOX` label; it does not
  permanently delete the message.
- All Google calls should continue flowing through `google_api_call` so token
  refresh persistence, auth disconnect handling, and retryable errors remain
  centralized.
- ADK schemas should keep optional fields as non-null primitive defaults where
  practical, matching the existing task and goal tool pattern.
- Destructive or externally visible operations should require explicit user
  confirmation in agent instructions. ADK can additionally wrap delete/archive
  and new-email send tools with `FunctionTool(..., require_confirmation=True)`.

## Implementation Pattern

- Add `fetch_events_for_range(user, session, start_date, end_date)` to the
  Calendar read service.
- Add `create_event`, `update_event`, and `delete_event` to the Calendar write
  service.
- Add `search_emails` and `read_email_by_query` to the Gmail read service.
- Add `send_new_email` and `archive_email` to the Gmail write service.
- Extend `google_tools.py` with ADK wrappers:
  - `get_events_for_date_range`
  - `create_calendar_event`
  - `update_calendar_event`
  - `delete_calendar_event`
  - `compose_email`
  - `search_emails`
  - `archive_email`
  - `read_email`
- Register the expanded Calendar/Gmail set on the root ADK agent, using
  confirmation wrappers for risky actions.
- Register matching voice direct functions and make mutating Google operations
  non-cancellable after invocation.
- Add focused tests for service API-shape behavior, ADK registration/schema,
  and voice registration/callback payloads.

## Gotchas

- Calendar all-day events are not part of this slice's tool schema; tools accept
  RFC 3339 `start_iso` and `end_iso` datetime values.
- `update_calendar_event` cannot clear a field in this slice because empty
  optional strings mean "do not change"; it can set summary, description, start,
  and end when provided.
- `compose_email` sends a new email directly and should only be called after the
  user has approved recipient, subject, and body.
- `read_email` intentionally returns the top search result's full content and
  not an LLM summary. The agent summarizes conversationally after receiving the
  tool result.
- Voice tools expose the same capabilities, but the voice prompt must still
  discourage lengthy email body dictation unless the user explicitly wants it.

## Required Packages

No new packages required.
