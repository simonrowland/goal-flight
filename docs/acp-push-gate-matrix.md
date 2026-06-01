# ACP Push-Gate Matrix

Chunk F validates the live ACP dispatch contract before push. The matrix is
gated because it starts real workers and may consume provider quota.

Run:

```shell
GOALFLIGHT_ACP_LIVE_MATRIX=1 python3 scripts/goalflight_acp_push_gate_matrix.py
```

Optional focused run:

```shell
GOALFLIGHT_ACP_LIVE_MATRIX=1 python3 scripts/goalflight_acp_push_gate_matrix.py --agents codex-acp grok
```

Default `./tests/run.sh` executes
`tests/python/test_dispatch_acp_push_gate_matrix_live.py`, which skips unless
`GOALFLIGHT_ACP_LIVE_MATRIX=1` is set.

## Agents

Rows:

- `codex-acp`
- `cursor`
- `grok`
- `claude-acp`

Boundary classification source of truth:
`scripts/goalflight_acp_boundaries.py::UNRELIABLE_ESCALATION_AGENTS`.
`codex-acp` is not in that set: auto-mode writes route through
`session/request_permission` with `kind` and `locations`, so the app-layer gate is
write-safe. `cursor`/`cursor-agent` and `grok`/`grok-acp` are in that set:
auto-mode is not a write boundary because writes are not routed through the ACP
permission gate. Pair writer dispatches with `--os-sandbox=workspace-write` and
review dispatches with `--os-sandbox=read-only` for a hard boundary.

**Platform caveat (load-bearing): `--os-sandbox` is macOS-only.** It is implemented
via `sandbox-exec` (seatbelt); on Linux/WSL it is unavailable and requesting it is
refused (there is no landlock/bubblewrap backend yet). So the per-platform write-safety
story for `cursor`/`grok` auto-mode is:

| Platform | codex-acp writes | cursor/grok auto-mode writes |
|---|---|---|
| macOS | safe (app-layer gate) | safe ONLY with `--os-sandbox` |
| Linux / WSL | safe (app-layer gate, cross-platform) | **NO hard write boundary available** — the warning fires but `--os-sandbox` cannot be used here |

On Linux/WSL, the only write-safe options for cursor/grok are: use `codex-acp` for
write workers (its gate is cross-platform), run cursor/grok **read-only / review-only**,
or accept the risk in a trusted workspace. A Linux os-sandbox backend is backlog
(see the os-sandbox-macOS-only gap note). Do not assume `--os-sandbox` is a universal remedy.

An unavailable CLI or failed local readiness probe records `SKIP-unavailable`.
`claude-acp` is the `claude-code-cli-acp` PTY shim; headless auth, 401, PTY, or
handshake timeout records `SKIP: claude-acp deferred (headless auth/PTY)` rather
than failing the whole matrix.

## Properties

- `round_trip`: ACP handshake plus trivial prompt completes with
  `terminal_state=complete`.
- `auto_permission`: auto mode allows an in-cwd write and escalates an
  outside-cwd write. Outside target creation is a failure.
- `ledger_stats`: dispatch ledger records terminal state, and
  `goalflight_dispatch.py --stats` sees ACP dispatch history.
- `held_permission`: inline permission held for more than 60 seconds
  auto-declines without `wedged`, `tool_timeout`, or `remote_turn_silence`.
- `locations`: write tool calls or permission requests expose `locations`; no
  locations fails because write scope cannot be proven.
- `silent_turn`: adapter liveness profile has the expected remote-turn tolerance
  armed. `remote_api` adapters must expose at least 1200 seconds.

The runner writes a JSON report and prints a per-agent by-property matrix. Any
`FAIL` exits nonzero. `SKIP` cells do not fail the harness.
