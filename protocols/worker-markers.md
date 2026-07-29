# Worker Marker Protocol

Workers emit parseable markers on their own lines:

- `STATUS: <current activity>`
- `STEER-ACK: <seq>` — steer mailbox message acknowledged
- `RESULT: <summary of completed work>`
- `USER-NEED: <specific blocker requiring user input>`
- `USER-CONFIRM: <specific confirmation needed before risky action>`
- `BLOCKED: <blocker and evidence>`
- `FAILED: <failure and evidence>`
- `COMPLETE: <finished state>`
- `READY: <findings-path>` — Investigator file-backed findings (path only in the marker line)

ACP transport also recognizes `PERMISSION-OK-PROCEEDED: <reason>` as an
ACP-only non-terminal permission modifier. It is not part of the bash-tail
watcher vocabulary. Use it only when the worker knows it worked around an
auto-declined permission cleanly; otherwise a `COMPLETE` after an auto-declined
permission downgrades to `blocked_permission_denied`.

Rules:

- Recognized terminal vocabulary: `RESULT`, `COMPLETE`, `READY`, `FAILED`,
  `USER-NEED`, `USER-CONFIRM`, `BLOCKED`. Transport policy decides whether a
  recognized marker stops the current loop.
- The live watcher recognizes a terminal marker only as the worker's **final** non-empty line (mid-output / code-fence markers are ignored — the injection guard).
- Dead/stale reconciliation may promote the last valid terminal marker from anywhere in the completed post-prompt tail. This handles workers that emit `READY:` and then a trailing TL;DR after the marker.
- `RESULT` and `COMPLETE` mean done unless the status JSON shows a process error.
- `COMPLETE`, `READY`, and `RESULT` are success terminals; `FAILED` is a failure terminal.
- `USER-NEED`, `BLOCKED`, and `FAILED` stop the dispatch loop and surface to the orchestrator.
- Bash-tail keeps `USER-CONFIRM` terminal. Unattended ACP routes it to the
  controller without cancelling the turn, preserves partial output, and waits
  at the next turn boundary for a correlated steer reply. The guarded action
  remains unauthorized: ACK, unrelated steer, timeout, and silence are never
  approval. The question deadline is measured from routing; timeout supplies
  one explicit `no` continuation at the next safe turn boundary. A worker that
  waits inside the asking turn is not silence-reaped, because ACP cannot deliver
  the answer until that turn returns. A repeated unresolved question blocks.
  Any correlated `no` is sticky and wins over every `yes`, including earlier
  or later conflicting replies. A completed safe-work continuation after a
  denial records `blocked_user_confirm_denied`; a denied ACP permission records
  `blocked_permission_denied`. Neither is an unqualified `complete`/`ok=true`.
- `PERMISSION-OK-PROCEEDED` is non-terminal; it modifies how the
  ACP runner interprets `COMPLETE` in the presence of auto-declined
  permissions. Multiple ACP emissions accumulate in the marker list.
- Watchers and ACP runners extract markers into status JSON. Do not tail raw logs when status JSON exists.

Compact status path:

```bash
python3 <skill-root>/scripts/goalflight_status.py
```
