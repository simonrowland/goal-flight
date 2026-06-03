# Worker Marker Protocol

Workers emit parseable markers on their own lines:

- `STATUS: <current activity>`
- `RESULT: <summary of completed work>`
- `USER-NEED: <specific blocker requiring user input>`
- `USER-CONFIRM: <specific confirmation needed before risky action>`
- `BLOCKED: <blocker and evidence>`
- `COMPLETE: <finished state>`
- `PERMISSION-OK-PROCEEDED: <reason>` — opt-out of the
  denied-permission terminal-state downgrade. Use ONLY when the worker
  KNOWS it worked around an auto-declined permission cleanly (e.g.,
  the requested write was redundant, or the worker chose an alternate
  in-cwd path that succeeded). Without this marker, a `COMPLETE` after
  any auto-declined permission downgrades to `blocked_permission_denied`
  per the sweep B P1 fix (commit bd4ba68 + follow-up).

Rules:

- Terminal markers: `RESULT`, `COMPLETE`, `USER-NEED`, `USER-CONFIRM`, `BLOCKED`.
- `RESULT` and `COMPLETE` mean done unless the status JSON shows a process error.
- `USER-NEED`, `USER-CONFIRM`, and `BLOCKED` stop the dispatch loop and surface to the orchestrator.
- `PERMISSION-OK-PROCEEDED` is non-terminal; it modifies how the
  runner interprets `COMPLETE` in the presence of auto-declined
  permissions. Multiple emissions accumulate in the marker list.
- Watchers and ACP runners extract markers into status JSON. Do not tail raw logs when status JSON exists.

Compact status path:

```bash
python3 <skill-root>/scripts/goalflight_status.py
```
