# Dispatch Routing Protocol

Choose the smallest execution shape that can finish safely.

1. `controller-direct`

Use only for tiny, local edits expected to finish in seconds. If the task grows,
stop and dispatch.

2. `acp`

Prefer ACP when an adapter is available. Use:

```bash
python3 <skill-root>/scripts/goalflight_acp_run.py \
  --agent <codex-acp|grok|cursor|claude> \
  --cwd "$PWD" \
  --prompt <prompt.md> \
  --status-json <status.json>
```

3. `goal-mode loop`

Use for chunks that need iterative implementation. Codex `/goal`, Grok Build
headless loops, or Claude re-dispatch loops are workflow shapes; ACP is the
preferred transport when the worker supports it.

4. `bash-tail`

Fallback only when no ACP path is available. Start the worker with stdout/stderr
to files, then watch via:

```bash
python3 <skill-root>/scripts/goalflight_watch.py \
  --pid "$WORKER_PID" \
  --tail <tail-file> \
  --status-json <status.json> \
  --agent <agent>
```

5. Capacity gate

Before spawning any worker, acquire a machine-global lease:

```bash
python3 <skill-root>/scripts/goalflight_capacity.py acquire \
  --agent <agent> \
  --project-root "$PWD" \
  --dispatch-id <id>
```

If decision is `wait`, do not spawn. Use another agent only if the concern
coverage remains valid.

6. Ledger

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
