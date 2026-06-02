You are running a Goal Flight orchestrator behavior test for goal-loop-default.

Repository: {{PROJECT_ROOT}}

A planned chunk needs code changes across several files, focused tests, likely
fixes after those tests, and self-review until clean. It is not a tiny edit and
not a pure judgment call.

Per `SKILL.md` Dispatch Model, choose the routing shape. This probe is read-only:
do not edit repository files and do not launch a real worker.

Reply with:

- `ROUTE: goal-loop default`
- `DISPATCH: scripts/goalflight_dispatch.py ...`
- one short reason naming convergence-heavy implementation
- `COMPLETE: true`

Do not propose controller-direct implementation or start an edit/test cycle in
this transcript.
