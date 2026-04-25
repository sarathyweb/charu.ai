# 79 — Goal Model, Service, And Tools Implementation

## Requirement Covered

Implement the checklist item:

- Goal model and status enum
- GoalService lifecycle methods
- ADK goal CRUD tools
- Voice goal CRUD tools

This covers Requirements 5, 6, and 7 in
`.kiro/specs/full-agent-tools/requirements.md`.

## Research Inputs

- Codemigo knowledge search:
  `goal model accountability calls CRUD AI agent tools SQLModel`
- Codemigo use-case search:
  `implement goals service model migration ADK tools voice tools tests SQLModel`
- ADK docs index: `https://adk.dev/llms.txt`
- ADK action confirmation docs: `https://adk.dev/tools-custom/confirmation/`
- Existing research:
  - `.pm/research/59-goal-model-and-service.md`
  - `.pm/research/60-goal-crud-tools-adk-agent.md`
  - `.pm/research/61-goal-crud-tools-voice-agent.md`
- Local code:
  - `app/models/task.py`
  - `app/services/task_service.py`
  - `app/agents/productivity_agent/tools.py`
  - `app/agents/productivity_agent/agent.py`
  - `app/voice/tools.py`
  - `tests/test_task_service.py`
  - `tests/unit/test_adk_task_tools.py`
  - `tests/unit/test_voice_tools.py`

## Key Concepts

- Goals are higher-level objectives that can span days or weeks. They are
  separate from atomic tasks and should not require task-level fuzzy matching.
- Goal mutations should be ID-based. The agent can call `list_goals` to get IDs
  internally, while responses should refer to goal titles.
- Goal persistence should follow existing SQLModel patterns: model + enum,
  DB-level CHECK constraint, `TimestampMixin`, service layer, Alembic migration,
  and explicit user ownership checks.
- ADK tools should use primitive arguments and non-null defaults for optional
  fields so generated schemas retain the required fields.
- Destructive delete tools should use ADK `FunctionTool(...,
  require_confirmation=True)`.
- Voice tools should mirror ADK payloads and should register mutating functions
  with `cancel_on_interruption=False` so a committed DB write is not cancelled
  before the callback result is delivered.

## Implementation Pattern

- Add `GoalStatus` with `active`, `completed`, and `abandoned`.
- Add `Goal` with `id`, `user_id`, `title`, optional `description`, `status`,
  optional `target_date`, timestamps, and optional `completed_at`.
- Add an Alembic migration for the `goals` table, status CHECK constraint,
  user/status/created index, and `updated_at` trigger.
- Add `GoalService` methods:
  - `create_goal`
  - `update_goal`
  - `complete_goal`
  - `abandon_goal`
  - `list_goals`
  - `delete_goal`
- Add ADK tools in a dedicated `goal_tools.py` and register them on the root
  agent.
- Add voice direct functions in `register_voice_tools` and include goal
  mutations in the non-cancellable tool set.
- Add focused service, ADK wrapper/schema, and voice callback tests.

## Gotchas

- `CallLog.goal` remains as the per-call outcome text for recaps and check-ins.
  The new `goals` table is for persistent objectives; it does not replace the
  call outcome fields in this slice.
- Clearing goal description or target date is not implemented because the spec
  only requires optional update values, and `None` currently means "no change".
- No task-goal foreign key is added yet. The spec defines goals as distinct from
  tasks but does not require linking tasks to goals in this iteration.
- API and dashboard goal surfaces are still a separate TODO.

## Required Packages

No new packages required.
