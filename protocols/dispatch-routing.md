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
- **Oversized result frame.** An ACP frame larger than the asyncio stream limit
  no longer hangs the reader: it resolves pending requests with a
  `result_too_large` error and reaps the worker (`kill()` skips the calling task,
  so the reader can reap its own connection). Terminal state `result_too_large`.
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

`wedged`, `tool_timeout`, and `result_too_large` are terminal lease states — the
capacity gate below frees and prunes the slot the same as `complete`/`failed`.

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
