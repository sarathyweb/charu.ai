# 81 - Deterministic ADK/Voice Evals and Deferred Backlog Review

## Requirement

Add deterministic ADK and voice evals before calling AI behavior production
complete, then revisit the deferred product backlog items:

- Weekend mode
- Urgent-email proactive calls
- Auto-task creation from emails
- Notion
- Google Keep
- Google Tasks
- Todoist

## Codemigo Knowledge Findings

Command:

```bash
codemigo.exe search --knowledge --max-results 50 --query "deterministic agent evals ADK voice AI behavior tool selection regression tests"
```

Key points synthesized:

- Agent behavior needs regression tests for tool selection, argument quality,
  routing, session state, and final response quality.
- Stable eval sets should cover representative product workflows and known
  failure modes.
- Tool trajectory checks catch regressions that plain response matching misses.
- End-to-end evals are useful, but deterministic contracts should also validate
  local registrations and schemas so CI can fail without a live model call.

## Codemigo Use-Case Findings

Command:

```bash
codemigo.exe search --usecase --max-results 50 --query "implement ADK eval datasets voice eval tests prompt tool registration backlog deferred product scoping"
```

Key points synthesized:

- Start with small end-to-end scenarios, then add component-level contracts.
- Keep eval data in source control and use tests to validate the eval files
  themselves.
- Use exact tool-name assertions for production-critical surfaces.
- Track deferred integrations as explicit product decisions, not vague TODOs.

## Official / Tool-Specific Research

The project requires ADK research through `https://adk.dev/llms.txt`.
That index points to:

- `https://adk.dev/evaluate/index.md`
- `https://adk.dev/evaluate/criteria/index.md`
- `https://adk.dev/evaluate/custom_metrics/index.md`
- `https://adk.dev/evaluate/environment_simulation/index.md`
- `https://adk.dev/evaluate/user-sim/index.md`

Local installed ADK package inspection found:

- Eval set files use the `.evalset.json` suffix.
- `google.adk.evaluation.local_eval_sets_manager.load_eval_set_from_file`
  accepts current Pydantic eval-set JSON or the older list-based JSON format.
- Older format cases contain `name`, `data`, optional `initial_session`, and
  each invocation can specify `query`, `expected_tool_use`,
  `expected_intermediate_agent_responses`, and `reference`.
- Expected tool uses are converted into `genai.types.FunctionCall` objects and
  can be inspected through `Invocation.intermediate_data.tool_uses`.
- ADK's default local eval criteria include tool trajectory and response match
  scoring; deterministic unit tests can validate the eval files and the local
  tool contracts before live model evals run.

## Local Implementation Patterns

- ADK root tools are registered in `app/agents/productivity_agent/agent.py`.
- Voice tools are registered through `app/voice/tools.py` on the Pipecat LLM
  service using `register_direct_function`.
- Existing tests already validate task, goal, Calendar, Gmail, and voice tool
  wrappers. New deterministic eval tests should avoid duplicating service
  behavior and instead validate:
  - Eval data loads through ADK's eval loader.
  - Expected ADK tool trajectories reference real root-agent tools.
  - Expected ADK tool arguments are compatible with registered tool schemas.
  - Risky ADK tools still require confirmation.
  - Voice eval cases reference registered voice tools and their interruption
    safety flags.
  - Known voice parity gaps remain explicit until implemented.

## Decisions

1. Add `tests/evals/productivity_assistant.evalset.json` as an
   ADK-compatible deterministic eval set for core accountability scenarios.
2. Add `tests/evals/voice_productivity_eval_cases.json` as a deterministic
   voice-call contract because the voice stack does not use ADK's eval runner
   directly.
3. Add `tests/evals/deferred_product_backlog_review.json` to make deferred
   product scope explicit and testable.
4. Keep third-party backlog integrations deferred until they have OAuth scope,
   data model, and product specs. This avoids half-building external
   integrations without clear user permissions or operational behavior.

## Gotchas

- Built-in `google_search` has a tool name but no normal function declaration,
  so schema validation must allow built-in tools with no local argument schema.
- Some ADK `FunctionTool` declarations mark defaulted function parameters as
  required. Eval fixtures should include those fields when the local schema
  requires them.
- Voice mutating tools must remain `cancel_on_interruption=False` so a spoken
  interruption does not cancel persistence after the model has committed to a
  save/update/delete/send operation.
- Voice currently has known parity gaps for Google Search and recurring
  call-window CRUD. The eval contract should name those gaps until a later
  implementation closes them.

## Required Packages

- `google-adk` for ADK eval schemas and local eval-set loader.
- `pytest` for deterministic contract tests.
- Existing project dependencies for Pipecat voice tool registration.
