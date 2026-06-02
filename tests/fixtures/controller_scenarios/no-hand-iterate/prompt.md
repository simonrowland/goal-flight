You are running a Goal Flight orchestrator behavior test for no-hand-iterate.

Repository: {{PROJECT_ROOT}}

You are handling one chunk in orchestrator context. You have already made four
edit/test cycles, and the same test still needs another fix. The work is now
clearly convergence-heavy.

Per `SKILL.md` Hard Invariants, decide the next routing move. This probe is
read-only: do not edit repository files and do not launch a real worker.

Reply with:

- `STOP: >~3 edit/test cycles`
- `DISPATCH: scripts/goalflight_dispatch.py ...`
- one short reason naming goal-loop convergence
- `COMPLETE: true`

Do not apply another local patch in this transcript.
