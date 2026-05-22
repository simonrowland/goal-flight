# Dispatch Routing Protocol

Choose the smallest execution shape that can finish safely. Routing has two
orthogonal axes: **iteration pattern** (how many turns) and **comms shape**
(how the controller observes the worker). Pick one value from each.

## Axis 1 — Iteration pattern

- `one-shot`: send a single prompt, worker completes the chunk in one turn.
  Default. Use when the chunk has a clear definition of done and fits one
  worker context.
- `goal-mode loop`: worker iterates against a goal across multiple turns,
  either by self-loop (codex `/goal`, Grok Build headless) or by controller
  re-dispatch through the same session. Use when the chunk needs
  review-revise cycles, exceeds one turn, or the worker should keep refining
  until a marker fires.

## Axis 2 — Comms shape

- `controller-direct`: no worker spawned. The controller does the edit itself.
  Use only for tiny local work expected to finish in seconds. If the task
  grows, stop and dispatch.
- `acp`: structured JSON-RPC stream over stdio. Default whenever an adapter
  exists. The controller sees turn boundaries, tool calls, plan entries, and
  stop reasons as discrete events, not text.
  ```bash
  python3 <skill-root>/scripts/goalflight_acp_run.py \
    --agent <codex-acp|grok|cursor|claude> \
    --cwd "$PWD" \
    --prompt <prompt.md> \
    --mode <one-shot|goal> \
    --status-json <status.json>
  ```
  The runner re-execs into `~/.goal-flight/venvs/acp-0.10/bin/python` when
  system `python3` cannot import `acp`; set `GOALFLIGHT_ACP_PYTHON` to override.
  That Python package is the controller-side client implementation. Workers do
  not need to be implemented with that SDK; they need to speak the adapter's
  declared ACP wire contract. A vendor CLI can expose its own `agent stdio`
  implementation while the manifest still owns command args, safe probes,
  liveness profile, and output contract.

  **`--mode` sets the idle-timeout.** `one-shot` (default) uses a 5-minute
  idle ceiling — a short dispatch silent that long is wedged. `goal` uses a
  10-hour idle ceiling because goal-mode loops run multi-hour and a worker
  churning through a big test/compile can emit no events for tens of
  minutes; a tight ceiling would kill it mid-run. Idle-timeout is the gap
  between events, NOT total runtime — it resets on every event, so a healthy
  goal-mode worker emitting periodic STATUS markers never trips it. Override
  with `--idle-timeout <secs>` (or `--idle-timeout 0` for no idle gate,
  relying on PID liveness + the worker's terminal marker).
- `bash-tail`: worker writes stdout/stderr to files; the controller watches
  via marker grep. Fallback only when no ACP adapter is available. See
  `protocols/legacy/bash-tail.md` for recipes and hazards (incl. the
  context-mode-dispatch caveat — never wrap a spawn or `tail -f` in
  `ctx_execute`).
  ```bash
  python3 <skill-root>/scripts/goalflight_watch.py \
    --pid "$WORKER_PID" \
    --tail <tail-file> \
    --status-json <status.json> \
    --agent <agent>
  ```

## Worker/controller candidates

Treat routing candidates as first-class only after their readiness gate passes:

| Candidate | Controller use | Worker use | Readiness gate |
|---|---|---|---|
| Codex | yes | yes | Desktop/CLI available when needed, context-mode registered for large-output work, ACP handshake passes for structured dispatch. |
| Cursor | yes | yes | Cursor Desktop or CLI path present for controller use; `cursor-agent` present and ACP handshake passes for worker use; model-currency probe is current or explicitly accepted as stale. |
| Grok | yes | yes | Grok Build/headless flags present; structured ACP path passes before ACP dispatch; bash-tail is fallback-only and must obey the marker limits in Composition rules. |
| Claude compatibility path | yes | yes | Adapter-owned CLI/plugin probes pass; startup gate applies where the adapter requires serialized initialization. |

If a candidate has static adapter capability but fails local readiness, do not
route work to it. Pick another ready candidate with equivalent concern coverage
or fall back to the legacy watcher when no ACP path is locally ready.

Unknown ACP commands are denied by default. Add a checked-in adapter manifest or
point `GOALFLIGHT_ADAPTERS_DIR` at a machine-local manifest directory for
experiments; do not silently dispatch an unmanifested binary.

## Liveness — a quiet worker is not a dead worker

Event/tail silence alone is NOT a wedge signal. A healthy worker grinding a long
test or compile can emit zero ACP events (or zero tail bytes) for tens of
minutes; treating that as a timeout false-positives it into a retry storm. The
runner and watchers use **process-group CPU** as the false-positive killer:

- The ACP runner (`goalflight_acp_run.py`) writes a *progressive* status JSON
  (`starting → handshaking → running`) and runs a concurrent heartbeat task that
  samples pgroup-CPU every `--heartbeat-interval` seconds (default 15s; env
  `GOALFLIGHT_HEARTBEAT_INTERVAL`). When the ACP stream goes silent past the
  idle window, the runner checks pgroup-CPU *before* cancelling: **CPU > epsilon
  ⇒ `running_quiet`, keep waiting; CPU ≈ 0 ⇒ wedged, cancel.** A busy-but-quiet
  worker is never killed; a genuinely stuck one still is.
- The watchers (`goalflight_watch.py`, `watch-dispatch-tail.sh`) apply the same
  rule to bash-tail dispatches: PID alive + pgroup-CPU > epsilon ⇒ `running_quiet`
  (no idle-timeout exit). A single failed CPU sample is never read as a wedge —
  the runner re-samples and the watchers require consecutive samples, riding out
  a transient `ps` failure before declaring a wedge.
- Heartbeats are **runner-written FILES, never task-notifications.** The
  controller is woken only on an actionable transition (completion / wedge /
  blocked), never per beat — a per-beat wake would re-process the controller's
  whole cached session (ruinous).
- **Handshake retry-once**: if the handshake (`initialize`/`session_new`) stalls
  — the intermittent codex-acp wedge, where the worker spawns but never answers
  even though the handshake works in isolation — the runner kills + respawns the
  worker and retries the handshake once before falling back. The wedged worker is
  always reaped first (never retry while an identity-matched PID is still alive).
- **The heartbeat *acts* (the active backstop, not just a status file).** Beyond
  the idle-path CPU check above, the concurrent heartbeat kills + finalizes a
  worker on a *confirmed* wedge even when `--idle-timeout 0` disables the idle
  gate. A "dead sample" requires ALL of: PID alive, pgroup-CPU ≤ epsilon, event
  count unchanged since the last beat, and zero outstanding tool calls;
  `--wedge-samples` consecutive dead samples (default 4) are required before the
  kill, so a transient `ps` failure or a momentary lull cannot false-positive.
  Terminal state `wedged`. `--max-quiet-s` (default 3600s) is a second wall for a
  CPU-busy worker that emits no events at all.
- **Tool-call grace + an absolute per-tool wall.** A worker that emits a
  `tool_call` (web search, a long test) then goes silent is I/O-bound at ≈0% CPU
  — indistinguishable from a wedge by CPU alone. While a tool is outstanding the
  dead-sample rule is suppressed (it is legitimate work). But a single tool
  outstanding past `--max-tool-s` (default 1800s) is killed *regardless of CPU* —
  the wall is absolute, so a CPU-busy or CPU-unsamplable stuck tool still trips
  it. Terminal state `tool_timeout`.
- **Oversized ACP frame.** An ACP frame larger than the asyncio stream limit no
  longer hangs the reader: the guarded reader drops the over-limit newline frame,
  increments the ACP dropped-frame counter, logs it, and continues. Oversized
  notifications are skipped. If an oversized response is dropped, the pending
  request falls through the existing idle/timeout failure path; no
  `result_too_large` terminal state is emitted for new runs.
- **StartupGate for fragile adapters** (`scripts/goalflight_startup_gate.py`).
  Some adapters starve each other during startup, not steady-state — the Claude
  TUI adapter blows its hardcoded 120s per-turn timeout on a trivial turn when
  several spawn at once (TUI init: hooks/LSP/keychain/auto-memory/MCP). The gate
  serializes the spawn→handshake window per agent via an `flock`. It is
  *handshake-gated, not a fixed stagger* — the next worker starts the instant the
  previous one finishes its handshake, on any machine (no interval baselined to
  one laptop). Default serializes the Claude TUI adapter only (env
  `GOALFLIGHT_SERIALIZE_STARTUP`); fail-open after 600s so a stuck holder cannot
  deadlock the fleet; concurrent *turns* stay parallel.

`wedged` and `tool_timeout` are active ACP terminal lease states — the capacity
gate below frees and prunes the slot the same as `complete`/`failed`.
`result_too_large` is retained only as a legacy pruning state for old 0.4.3
records.

## Worker permissions and context-mode over ACP

A spawned worker's permissions resolve **inside the runner subprocess**, not at
the controller. `goalflight_acp_run.py` answers every `session/request_permission`
itself via `auto_allow_tools=True` (default). The controller is never in the
per-tool permission loop and **cannot be asked to approve a tool call in real
time**. The only worker→controller escalation channel is the text markers
`USER-NEED:` / `USER-CONFIRM:` (`worker-markers.md`): a worker that needs a human
decision stops and emits one; the controller relays it.

Two independent gates govern a codex worker. Do not conflate them:

1. **codex sandbox + approval policy** — file writes, shell, network. Open it with
   `--sandbox workspace-write -c approval_policy=never` (the classifier-safe form
   of "full permissions"). `--dangerously-bypass-approvals-and-sandbox` is rejected
   by some controllers' auto-mode safety classifiers and is unnecessary when the
   worker's edit scope is its workspace.
2. **MCP elicitation** (`request_user_input`) — raised by tools like context-mode's
   `ctx_index`. NOT a sandbox/approval matter, so gate #1 does nothing for it. Left
   unhandled, codex-acp neither forwards nor rejects the elicitation over ACP and
   the tool call wedges at ~0% CPU until the per-tool wall.

**A codex worker can use context-mode over ACP in auto-mode.** The runner
auto-injects `-c features.tool_call_mcp_elicitation=true` for codex-acp at the
single spawn boundary (`ensure_codex_acp_elicitation`); the elicitation then
arrives as a `request_permission` that `auto_allow_tools` grants, and the tool
completes. So a worker may index/search/execute via context-mode in a normal
auto-mode ACP dispatch — **no `tail -f`, no "disable context-mode for ACP."**
Proven by hermetic tests (`test_acp_pipe.py::case_permission_elicitation_unblocks`,
`::case_codex_acp_elicitation_injection_unit`) and a live codex-acp + context-mode
end-to-end run (index + search, completed clean).

Distinct, and still true: do **not** wrap the *dispatch* or a `tail -f` in
`ctx_execute` / `ctx_batch_execute` (the controller-side caveat in Axis 2 and
`legacy/bash-tail.md`). That is the controller offloading a long-running spawn
into context-mode's bounded-command timeout — unrelated to a worker calling
context-mode tools.

## Composition rules

| Iteration | Comms | Supported | Notes |
|---|---|---|---|
| one-shot | controller-direct | yes | tiny edits, no spawn |
| one-shot | acp | yes | default for any spawned worker |
| one-shot | bash-tail | yes | only when no ACP adapter |
| goal-mode | acp | yes | preferred transport for loops |
| goal-mode | bash-tail | depends on worker | Requires the worker to emit a detectable end-of-goal marker in the flat tail (so the watcher knows the loop is complete). **As of 2026-05-19, codex `/goal` is the only worker known to qualify** — its structured "Final response" block is the marker; see `templates/codex-goal-prompt.md.tpl`. Grok and claude headless do not qualify today; a future worker that grows an equivalent marker contract would join this cell. When the worker doesn't qualify, use one-shot + bash-tail with a coarser chunk instead. |
| goal-mode | controller-direct | n/a | controller-direct is single-turn by definition |

## Capacity gate

Before spawning any worker, acquire a machine-global lease:

```bash
python3 <skill-root>/scripts/goalflight_capacity.py acquire \
  --agent <agent> \
  --project-root "$PWD" \
  --dispatch-id <id>
```

If decision is `wait`, do not spawn. Use another agent only if the concern
coverage remains valid.

## Ledger

After spawn, record PID and prompt:

```bash
python3 <skill-root>/scripts/goalflight_ledger.py record \
  --dispatch-id <id> \
  --agent <agent> \
  --transport <acp|bash-tail|file-backed-review> \
  --worker-pid "$WORKER_PID" \
  --prompt-path <prompt.md> \
  --status-path <status.json>
```
