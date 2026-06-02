---
description: "Nondestructive OpenCode orchestrator self-dispatch verification."
---

# self-dispatch-test

Verify the **current host** can act as a Goal Flight orchestrator and dispatch
read-only OpenCode workers to itself on both supported transports (ACP and
bash-tail). This is the OpenCode-specific form of the orchestrator
generalization check in `AGENTS.md`.

Read:

- `protocols/dispatch-routing.md`
- `protocols/worker-markers.md`
- `docs/hosts/opencode.md`

## When to invoke

- User asks to verify OpenCode self-dispatch, orchestrator generalization, or
  "can goal-flight dispatch workers from OpenCode?"
- After `./setup.sh --apply --yes --opencode` on a new machine.
- Before unattended execute on OpenCode when worker readiness is unknown.

## Orchestrator steps

1. Confirm Goal Flight is loaded (`AGENTS.md` → host skill → repository
   `SKILL.md`).

2. Run the procedural harness (read-only; no queue edits, no commits):

```bash
source ~/.config/rpp/litellm.env   # when using litellm/* models
python3 <skill-root>/scripts/hosts/opencode/self_dispatch_test.py --json
```

3. Summarize **only** the JSON fields into the conversation:

| Field | What to report |
|---|---|
| `ok` | overall pass/fail |
| `doctor.opencode_present` / `doctor.acp_sdk_ok` | host readiness |
| `capacity.opencode_acp` / `capacity.opencode_bash_tail` | headroom |
| `transports.acp.state` / `text_excerpt` | ACP worker outcome |
| `transports.bash_tail.complete_marker` | bash-tail watcher saw `COMPLETE:` |

Do **not** paste raw tail files or full doctor JSON.

4. If `ok` is false:

- `skipped: true` → missing opencode/LiteLLM; surface install steps from
  `docs/hosts/opencode.md` and stop.
- ACP failed, bash-tail passed → cite `transports.acp.error`; ACP is the
  default worker path.
- bash-tail failed, ACP passed → cite `transports.bash_tail.error`; execute
  can still use ACP.
- Both failed → run `python3 <skill-root>/scripts/goalflight_doctor.py
  --project-root "$PWD" --json` and fix host install before execute.

## What this proves

- Orchestrator preflight (doctor + capacity) runs from the OpenCode host.
- OpenCode can spawn **itself** as an ACP worker via `goalflight_acp_run.py`.
- OpenCode can spawn **itself** as a bash-tail worker via `scripts/hosts/opencode/bash_tail.py`
  and the standard `watch-dispatch-tail.sh` watcher.

Workers use a read-only arithmetic prompt (`2+2 → 4`) in a temp workdir with
project `opencode.json` copied in. No repository writes.

## Optional flags

```bash
python3 <skill-root>/scripts/hosts/opencode/self_dispatch_test.py --json --skip-bash-tail
python3 <skill-root>/scripts/hosts/opencode/self_dispatch_test.py --json --skip-acp
python3 <skill-root>/scripts/hosts/opencode/self_dispatch_test.py --json -m litellm/frontier-coder
```

## After a passing run

Proceed to `/goal-flight init` or `/goal-flight execute` as appropriate.
Ledger and status evidence from the harness dispatches are written under
`GOALFLIGHT_STATE_DIR` (default `/tmp/goal-flight-<uid>/`).
