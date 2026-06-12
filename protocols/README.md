# goal-flight protocols

Load these only when the command path needs them. `SKILL.md` stays the router;
protocol files carry the detailed operating procedures.

| Protocol | Load when |
|---|---|
| `session-preflight.md` | command start, drift check, install ambiguity |
| `tool-readiness.md` | `/goal-flight doctor`, `/goal-flight init`, capability probes |
| `dispatch-routing.md` | choosing iteration pattern (one-shot / goal-mode) and comms shape (controller-direct / ACP / bash-tail) |
| `worker-markers.md` | rendering prompts or parsing worker status |
| `state-handoff.md` | compacting, resume notes, status recovery |
| `user-status-cadence.md` | in-flight worker poll + user progress updates (≤15 min) |
| `chunk-review.md` | pre-commit gstack `/review` (default; complementary parallel autoreview optional) before chunk land |
| `dispatched-worker-recovery.md` | orchestrator takeover when an ACP-dispatched worker terminal-blocks before commit |
| `engagement-lint.md` | data contract for engagement-prompt verb patterns (the future PostToolUse/Stop lint; Wave-A) |
| `foreground-duration-hook.md` | data contract for slow-command families that should auto-background (the future PreToolUse rule; Wave-A) |
| `premises.md` | init/decompose needs premise distillation |
| `self-delegation.md` | user explicitly asks for `/fork` or `/branch` |
| `worktrees-parallel.md` | `execute --parallel` or merge orchestration |
| `milestone-review.md` | milestone gstack/Codex review flights |
| `review-mining.md` | a NEW bug class is caught (mint + backwards sweep), or archiving review verdicts |

## Legacy

`legacy/` holds dispatch recipes from the pre-ACP era (bash-tail, `tail -f`).
Do not hot-load these. Consult `legacy/README.md` only when the primary ACP
path is unavailable for a specific worker.
