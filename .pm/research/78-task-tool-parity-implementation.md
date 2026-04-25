# 78 — Task Tool Parity Implementation

## Requirement Covered

Complete the checklist item:

- Task update
- Task delete
- Task snooze
- Task unsnooze
- Voice `list_pending_tasks`

This covers Requirements 1-4 in `.kiro/specs/full-agent-tools/requirements.md`.

## Research Inputs

- Codemigo knowledge search: `task management CRUD snooze unsnooze AI agent tools voice assistant SQLModel service tests`
- Codemigo use-case search: `implement update delete snooze unsnooze task tools ADK voice list pending tasks tests`
- ADK docs index: `https://adk.dev/llms.txt`
- Existing focused research:
  - `.pm/research/55-task-update-tool.md`
  - `.pm/research/56-task-delete-tool.md`
  - `.pm/research/57-task-snooze-unsnooze-tools.md`
  - `.pm/research/58-list-pending-tasks-voice-agent.md`
- Local code:
  - `app/models/task.py`
  - `app/models/enums.py`
  - `app/services/task_service.py`
  - `app/agents/productivity_agent/tools.py`
  - `app/agents/productivity_agent/agent.py`
  - `app/voice/tools.py`
  - `tests/test_task_service.py`

## Key Concepts

- Task mutations should live in `TaskService`; ADK and voice tools should be thin wrappers.
- Fuzzy user references should use the existing completion threshold `0.4`, not the stricter save/dedup threshold `0.6`.
- Update/delete/snooze should target pending tasks only.
- Unsnooze should target snoozed tasks only.
- Delete is a hard delete because the spec says permanently delete.
- Tool wrappers should return structured results that include enough data for conversational confirmation.
- Voice tools use closure-captured `user_id` and return results through `FunctionCallParams.result_callback`.
- ADK tools resolve user identity from `ToolContext.state["phone"]` and rely on ADK schema generation from function signatures/docstrings.
- Snoozed tasks need a reactivation path when `snoozed_until` is due; pending-task reads and pending fuzzy matches are the natural place to perform this light cleanup.
- Model-supplied list limits need runtime bounds so ADK/voice cannot request an accidental unbounded task dump.
- Pipecat direct functions default to cancellation on user interruption; mutating tools should opt out so a committed database write still reaches `result_callback`.

## Implementation Pattern

- Add `_find_similar_by_status` to `TaskService` and keep `_find_similar_pending` as a compatibility helper.
- Add `update_task`, `delete_task`, `snooze_task`, and `unsnooze_task` to `TaskService`.
- Parse ISO datetimes in tool wrappers, not in the service.
- Validate that update has at least one field and priority is within `0..100`.
- Reactivate due snoozed tasks before pending list and pending fuzzy-match operations.
- Bound pending-task list limits to `1..50`.
- Add ADK tool functions with the same user-facing names.
- Register expanded task tools in `_task_tools`; wrap the hard-delete ADK tool with confirmation.
- Add voice direct functions and register them in `all_tools`, including `list_pending_tasks`.
- Register mutating voice direct functions with `cancel_on_interruption=False`.
- Add service tests, ADK wrapper/registration tests, and voice registration/callback tests.

## Gotchas

- `TaskStatus.SNOOZED` and `Task.snoozed_until` already exist, but no service behavior currently uses them.
- `list_pending_tasks` should exclude future-snoozed tasks but reactivate due snoozed tasks so they reappear.
- The task-manager agent instruction currently mentions updates, but the tool list does not support them.
- Deleting a task returns a detached ORM object after commit; capture confirmation fields before deletion if needed.
- Voice tool tests need a fake LLM that records direct-function registration.
- ADK's JSON-schema path drops the `required` list when nullable optional parameters are present, so optional update fields use sentinel defaults and wrappers normalize them before calling `TaskService`.

## Required Packages

No new packages required.
