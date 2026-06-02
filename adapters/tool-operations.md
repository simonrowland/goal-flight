# Tool Operations Contract

Goal Flight instructions refer to abstract operations. Adapter manifests map
each operation to host tools and constraints through `tool_name_map`.

Required operations:

- `shell_exec`: run shell or sandboxed command execution.
- `read_file`: read files for edit or bounded analysis.
- `write_file`: create or modify files.
- `search`: search files, indexed content, or host-specific context stores.
- `delegate`: start or message worker agents.
- `ask_user`: request user input or confirmation.
- `plan_update`: report task-plan state.
- `memory_search`: query persisted memory or session context.
- `browser_ui`: drive local or remote browser UI when supported.

Each operation requires:

- `host_tools`: concrete tool or command names available for this agent.
- `constraints`: adapter-specific safety and routing rules.

Host-tool names are adapter data, not core-doc behavior. An orchestrator must read
the manifest before translating abstract operations into host calls.

## Invocation And Output

`invocation.exec` declares how the agent runs: native tool, CLI, ACP, plugin, or
prompt template. It also declares safe required args, forbidden args,
justification-gated args, stdin mode, cwd policy, and output parsing. For ACP,
the manifest describes the wire/dialect contract and wrapper command; it does
not imply the worker implementation uses the same SDK as the orchestrator.

Forbidden args are rejected in invocations, probes, generated wrappers, and
checked-in configs before live setup, orchestrator launch, or worker dispatch.

Goal-mode eligibility requires an output contract with a verified structured
final event or final regex plus an exit/resume policy.
