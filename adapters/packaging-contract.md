# Packaging Contract

Adapter packaging describes how setup and doctor flows can project Goal Flight
support into an agent host without rewriting core instructions.

Required fields:

- `checked_in_wrappers`: wrapper files committed to this repo.
- `generated_outputs`: files rendered by setup or doctor flows.
- `install_actions`: gated actions such as copy, copy-or-merge, config merge,
  link, or plugin registration.
- `plugin_manifest`: plugin support status and manifest path when applicable.
- `mcp`: required MCP servers for this adapter, if any.
- `validation`: checks that prove packaging is internally consistent.

Install actions must declare source, target, whether they write repo or user
config, whether a user gate is required, backup path, and rollback action.

Setup is dry-run-first. Any user-config mutation needs an explicit gate,
backup, and rollback. Unsupported plugin APIs remain config-only or
probe-required and cannot promote local readiness.

Discovery is bounded by `discovery.budget`. Probe execution is local-first,
non-network by default, and non-model-consuming. Unsafe probes require a user
gate and still cannot run inside default setup.
