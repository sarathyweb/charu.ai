# 83 - Dashboard API Feature Closure

## Requirements

Close the dashboard/API items still listed as missing:

- Task update/delete/snooze/unsnooze API paths
- Goal endpoints or dashboard support
- Dashboard call history data
- Dashboard settings editing
- Connection-management polish beyond read-only integration cards
- Backend tests for the new dashboard API contracts

## Codemigo Knowledge Findings

Command:

```bash
codemigo.exe search --knowledge --max-results 50 --query "production feature closure FastAPI dashboard API Celery voice reliability external smoke tests task backlog"
```

Key points synthesized:

- Production APIs should expose real routes that can be tested directly,
  rather than depending on UI-only behavior.
- Runtime confidence comes from testing the same routes the frontend will call.
- Feature closure should prioritize stable contracts and observable behavior
  over broad UI polish.

## Codemigo Use-Case Findings

Command:

```bash
codemigo.exe search --usecase --max-results 50 --query "implement dashboard task goal endpoints call history settings Celery draft review task voice outcome persistence prefetch tests"
```

Key points synthesized:

- FastAPI route tests should exercise authenticated dependencies and exact
  response shapes.
- Task-list interfaces need clear post-mutation state, especially after
  completion, deletion, and snoozing.
- Goal and task mutations should be ID-based in dashboard APIs so user-facing
  controls do not depend on fuzzy title matching.

## Official / Tool-Specific Research

- FastAPI route handlers can return plain Python dictionaries and FastAPI will
  serialize them as JSON.
- For future chat streaming, FastAPI supports `StreamingResponse` with async
  generators; that belongs to the chat/browser-voice track, not this dashboard
  API slice.

## Local Implementation Pattern

- `app/api/dashboard.py` already owns authenticated dashboard routes.
- `_resolve_user()` maps the Firebase principal to the local `User`.
- `TaskService` owns task lifecycle logic but currently exposes fuzzy
  title-based mutations for agents.
- `GoalService` already has ID-based CRUD methods suitable for dashboard APIs.
- Existing dashboard tests directly call route functions with dependency
  objects, avoiding Firebase and HTTP boilerplate.

## Decisions

1. Add ID-based task helpers to `TaskService` for dashboard use.
2. Keep agent fuzzy-match task tools unchanged.
3. Add dashboard routes in `app/api/dashboard.py` rather than creating a second
   router, because the web app already calls this router.
4. Return simple JSON payloads with serialized task, goal, call, profile, and
   call-window objects.

## Gotchas

- A deleted task/goal should return the serialized object from before deletion.
- Snooze datetimes must be timezone-aware.
- Profile timezone edits should validate IANA timezone names before persistence.
- Call-window edits should remain backed by `CallWindowService` so validation
  and future schedule deletion/rematerialization stay centralized.

## Required Packages

- Existing FastAPI/Pydantic stack.
- Existing SQLModel ORM.
- Existing pytest/httpx route-test stack.
