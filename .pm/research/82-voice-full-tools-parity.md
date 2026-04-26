# 82 - Voice Full-Tools Parity

## Requirement

Close the remaining voice full-tools parity gaps:

- Voice Google Search support
- Voice call-window CRUD tools:
  - `add_call_window`
  - `update_call_window`
  - `remove_call_window`
  - `list_call_windows`
- Deterministic tests proving parity remains wired

## Codemigo Knowledge Findings

Command:

```bash
codemigo.exe search --knowledge --max-results 50 --query "voice assistant tool parity google search grounding call window CRUD tools production agent"
```

Key points synthesized:

- Voice pipelines should reuse the same core agent/tool concepts as text
  surfaces where possible, so behavior stays consistent across channels.
- Hosted or provider-native search tools are preferable when the product only
  needs grounded current answers, because they avoid a custom scraping/search
  stack.
- Tool activity should be evaluated at the registration and routing level,
  especially when the toolset is large.

## Codemigo Use-Case Findings

Command:

```bash
codemigo.exe search --usecase --max-results 50 --query "implement voice google search tool call window add update remove list voice tools tests"
```

Key points synthesized:

- Treat voice transcript text like ordinary user text for tool routing, but
  keep tool interfaces clear and narrow.
- Add direct implementation tests for each new tool before relying on model
  routing.
- Measure tool selection and registration deterministically with exact contract
  tests.

## Official / Tool-Specific Research

The project requires ADK research through `https://adk.dev/llms.txt`. That
index links to:

- `https://adk.dev/integrations/google-search/index.md`
- `https://adk.dev/streaming/streaming-tools/index.md`
- `https://adk.dev/tools-custom/function-tools/index.md`

Google Cloud's Gemini Live API documentation says Live API tool use can include
function calling and grounding, and that Grounding with Google Search is enabled
by including `{"google_search": {}}` in the Live API `tools` list.

Local Pipecat source confirms:

- `ToolsSchema` accepts both `standard_tools` and provider-specific
  `custom_tools`.
- `AdapterType.GEMINI` is the Gemini custom-tool key.
- The Gemini adapter appends `custom_tools[AdapterType.GEMINI]` to the normal
  function declarations list.

Existing project research `.pm/research/64-web-search-voice-agent.md` already
chose built-in Gemini Live Google Search grounding over a custom Google Custom
Search function because it preserves voice latency and matches the ADK
root-agent search behavior.

## Local Implementation Pattern

- Keep ordinary business actions as direct Pipecat functions registered via
  `llm.register_direct_function`.
- Register Gemini Live search as a provider-specific custom tool:

```python
ToolsSchema(
    standard_tools=all_tools,
    custom_tools={AdapterType.GEMINI: [{"google_search": {}}]},
)
```

- Implement call-window voice functions as thin closures over
  `CallWindowService`, mirroring the ADK wrappers:
  - validate window type
  - validate `HH:MM` times
  - enforce minimum 20-minute windows
  - prevent overlap
  - enforce maximum 3 active windows
  - rematerialize future calls best-effort after changes

## Decisions

1. Voice Google Search is a Gemini custom tool, not a `register_direct_function`
   callback.
2. Voice call-window mutations are non-cancellable after invocation because
   they persist schedule changes and delete/rematerialize future planned calls.
3. Voice exact parity tests should count Google Search as provider custom tool
   parity, while direct-function parity should cover the 40 standard voice
   functions.

## Gotchas

- `ToolsSchema.custom_tools` is separate from `llm.functions`, so tests must
  validate both direct functions and Gemini custom tools.
- Built-in search has no local argument schema to inspect.
- `CallWindowService.save_call_window()` and update/deactivate methods commit
  internally; rematerialization must run after the service call and commit its
  own work.
- `remove_call_window` should be idempotent when the target window is already
  absent.

## Required Packages

- Existing `pipecat` Gemini adapter and `ToolsSchema`.
- Existing `google-adk` root-agent search for comparison and eval contracts.
- Existing SQLModel-backed `CallWindowService`.
