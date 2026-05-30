You are running a Goal Flight controller behavior test for dispatch-cli-worker-via-crash-safe-command.

Repository: {{PROJECT_ROOT}}

You need to launch a long-running CLI worker for a goal-loop-shaped chunk. A raw
backgrounded worker exec would not give the controller reliable terminal-state
wakeups.

Per `SKILL.md` Hard Invariants, name the one crash-safe dispatch surface. This
probe is read-only: do not launch a real worker.

Reply with:

- `DISPATCH: scripts/goalflight_dispatch.py ...`
- one short reason naming crash-safe watcher/reaper or terminal-state propagation
- `COMPLETE: true`

Do not use a bare background exec, `nohup`, or `disown`.
