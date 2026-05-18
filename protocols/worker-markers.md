# Worker Marker Protocol

Workers emit parseable markers on their own lines:

- `STATUS: <current activity>`
- `RESULT: <summary of completed work>`
- `USER-NEED: <specific blocker requiring user input>`
- `USER-CONFIRM: <specific confirmation needed before risky action>`
- `BLOCKED: <blocker and evidence>`
- `COMPLETE: <finished state>`

Rules:

- Terminal markers: `RESULT`, `COMPLETE`, `USER-NEED`, `USER-CONFIRM`, `BLOCKED`.
- `RESULT` and `COMPLETE` mean done unless the status JSON shows a process error.
- `USER-NEED`, `USER-CONFIRM`, and `BLOCKED` stop the dispatch loop and surface to the controller.
- Watchers and ACP runners extract markers into status JSON. Do not tail raw logs when status JSON exists.

Compact status path:

```bash
python3 <skill-root>/scripts/goalflight_status.py --json
```
