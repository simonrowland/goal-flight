# Agent Adapters

`adapters/` defines the Goal Flight agent-adapter contract. Each future
`adapters/<agent>.json` manifest must validate against
`agent-adapter.schema.json` and must use schema name
`goalflight.agent-adapter.v1`.

The manifest is the source of truth for adapter shape. Core instructions use
abstract Goal Flight operations. Adapter manifests map those operations onto
host tools, invocation form, permission surface, memory behavior, packaging,
discovery probes, status/heartbeat parsing, provider policy, and host-specific
projection.

## Two-Layer Gate Model

Static capability and machine-local readiness are separate.

- `support.controller` and `support.worker` are checked-in static capability
  declarations. They say whether an agent can serve as orchestrator or worker in
  principle: `supported`, `candidate`, or `unsupported`.
- `local_readiness_state` describes the machine-local readiness record shape.
  Probe executors, setup, and doctor flows own actual readiness files. Checked-
  in manifests must not treat static support as proof that a local binary,
  auth/config, safe args, or status path is ready.

Live work requires both layers to pass. `candidate`, `unsupported`,
`config_only`, `probe_required`, `not_installed`, and `forbidden-arg` paths
deny orchestrator launch and worker dispatch.

## Validation Flow

Future runtime code will call:

```text
validate_adapter_gate(agent_id, role, live_entry, requested_transport, local_state)
```

The gate consumes the manifest plus machine-local readiness. It returns whether
live orchestrator launch or worker dispatch is allowed, a denial reason, required
probe ids, blocked fields, and a safe next action. Default result is deny.

## Files

- `agent-adapter.schema.json`: JSON Schema contract for per-agent manifests.
- `tool-operations.md`: abstract operation to host-tool mapping contract.
- `memory-contract.md`: repo-canonical memory and host mirror rules.
- `packaging-contract.md`: wrapper, plugin, MCP, install, and validation rules.
