# 90. Email Automation: Urgent Calls And Auto Tasks

Date: 2026-04-26

## Requirement

Implement the deferred backlog items:

- Urgent-email proactive calls.
- Auto-task creation from emails.

## Research

- Codemigo knowledge search: proactive outreach should be gated by opt-in, timing, quiet hours, and repetition limits so automated contact feels helpful rather than spammy.
- Codemigo use-case search: start email triage with deterministic categories, scores, and structured state before adding model classifiers. Rule baselines are easier to test and isolate from workflow bugs.
- Gmail API docs: `users.messages.list` accepts a Gmail search `q` and `maxResults`; `users.messages.get` can fetch metadata or full message content. This matches Charu's existing `search_emails` and `get_email_for_reply` helpers.
- Gmail filtering docs: server-side Gmail search operators can narrow candidate messages, but product logic should still apply app-level filtering for no-reply senders and confidence gates.
- Twilio Voice docs: outbound calls are created through the Calls resource and are account-rate-limited. Charu already centralizes this through materialized `CallLog` rows and the due-row dispatcher, so email automation should schedule on-demand rows rather than calling Twilio directly.

## Design

- Add per-user opt-ins:
  - `urgent_email_calls_enabled`
  - `auto_task_from_emails_enabled`
- Add per-user quiet hours for email automation, defaulting to 21:00-08:00 local time.
- Add an `email_automation_events` table to track Gmail message/thread handling. Dedupe is scoped by `(user_id, event_type, gmail_thread_id)` so repeat sweeps do not schedule duplicate calls or tasks.
- Add deterministic scoring:
  - Urgent email score from subject/body terms such as `urgent`, `asap`, `action required`, `deadline`, and `blocked`.
  - Task extraction score from action/request terms such as `please`, `can you`, `review`, `approve`, `send`, `schedule`, and `follow up`.
- Keep Gmail search broad enough to catch candidates, then rely on app-level confidence filters.
- Create tasks through `TaskService.save_task(..., source="gmail")` so existing fuzzy and embedding dedupe applies.
- Schedule urgent calls by inserting an `on_demand` `CallLog` for the due-row dispatcher. Do not replace an existing active on-demand call.
- Add call context for proactive email calls by storing the urgent email reason in `CallLog.goal`, `CallLog.next_action`, and `CallLog.commitments`, then injecting it into voice instructions when a call log id is available.

## Safety Gates

- Global env flags can disable the sweep.
- User opt-in is required for each automation.
- Gmail must be connected.
- No-reply/automated senders are skipped.
- Urgent calls respect user-local quiet hours.
- Urgent calls respect cooldown and max-per-day limits.
- Thread-level dedupe prevents repeated action on the same Gmail thread.
- Google API errors do not fail the whole sweep.

## Required Env

```env
EMAIL_AUTOMATION_ENABLED=true
URGENT_EMAIL_CALLS_ENABLED=true
AUTO_TASK_FROM_EMAILS_ENABLED=true
EMAIL_AUTOMATION_LOOKBACK_DAYS=2
EMAIL_AUTOMATION_MAX_MESSAGES_PER_USER=10
URGENT_EMAIL_CALL_DELAY_MINUTES=2
URGENT_EMAIL_CALL_COOLDOWN_MINUTES=240
URGENT_EMAIL_CALL_MAX_PER_DAY=1
URGENT_EMAIL_MIN_SCORE=0.65
AUTO_TASK_EMAIL_MIN_SCORE=0.7
```

## Relevant Links

- https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list
- https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/get
- https://developers.google.com/workspace/gmail/api/guides/filtering
- https://www.twilio.com/docs/voice/api/call-resource
