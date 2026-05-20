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
