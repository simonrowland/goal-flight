---
description: "Run scripted controller behavior scenarios (Codex-first bash-tail harness)."
---

# controller-behavior-test [--controller codex] [--scenario doctor-loads]

Read:

- `docs-private/plans/controller-behavior-harness-plan.md` (when present locally)

Runs the file-backed controller behavior harness. Wave 1 implements **Codex +
bash-tail** with the `doctor-loads` scenario. The Claude Code regression path
uses `claude-code-cli-acp` through Goal Flight ACP so it stays on the
subscription-routed interactive shim.

## Host runners

| Controller | Transport | Bash test | Transcript path |
|------------|-----------|-----------|-----------------|
| `codex` | bash-tail `codex exec` | `tests/bash/test-controller-behavior-codex.sh` | harness temp tail |
| `claude-acp` | ACP shim via `claude-code-cli-acp` | `tests/bash/test-controller-behavior-claude-code-acp.sh` | `docs-private/reviews/<date>-chunk-15/<scenario>.transcript.log` |

## Usage

```shell
# Maintainer / live suite (not required for default ./tests/run.sh)
export GOALFLIGHT_CONTROLLER_BEHAVIOR=1

python3 scripts/hosts/controller/probe_matrix.py --json
python3 scripts/hosts/controller/behavior_scenario.py \
  --controller codex \
  --scenario doctor-loads \
  --directory "$(git rev-parse --show-toplevel)" \
  --json

python3 scripts/hosts/controller/behavior_scenario.py \
  --controller claude-acp \
  --scenario doctor-loads \
  --directory "$(git rev-parse --show-toplevel)" \
  --transcript-dir "docs-private/reviews/$(date +%F)-chunk-15" \
  --json
```

## Skip policy

- `./tests/run.sh` includes `tests/bash/test-controller-behavior-codex.sh`, which
  **skips exit 0** unless `GOALFLIGHT_CONTROLLER_BEHAVIOR=1` and `codex` is on PATH.
- `./tests/run.sh` includes `tests/bash/test-controller-behavior-claude-code-acp.sh`, which
  **skips exit 0** unless `GOALFLIGHT_CONTROLLER_BEHAVIOR=1` and `claude-code-cli-acp` is on PATH.
- Hermetic structure tests always run via `tests/bash/test-controller-probe-matrix.sh`.

## Scenarios (fixtures)

Fixtures live under `tests/fixtures/controller_scenarios/<id>/prompt.md`.

| Scenario | Pass contract (summary) |
|----------|-------------------------|
| `doctor-loads` | Invokes or cites `goalflight_doctor.py --json`; mentions ok / host install |
| `resume-after-compaction` | Reads RESUME-NOTES handoff; runs status + fast test subset; tests pass |
| `continue-prescribed-step-two` | Runs `goalflight_status.py --json` then `test_controller_probe_matrix.py` without asking; no engagement bait; ends with `STEP_TWO_DONE: true` |
| `read-skill-end-to-end` | Reads back-half `SKILL.md` Worker Routing text and quotes late-section controller-provider asymmetry |
| `compaction-reload-skill` | With RESUME-NOTES + active queue present, reloads `SKILL.md` and quotes the rotating sentinel |
| `review-flight-at-completion` | Dispatches gstack `/review`/`/challenge`, autoreview fallback, or canonical read-only `codex exec` before commit; no ad hoc review prompt |
| `chat-as-requirements` | Queues sequenced mid-session asks through `/goal-flight goal` / `commands/goal.md`; no task pivot or inline edits |
| `draft-goal-office-hours` | Routes fuzzy draft-goal prompts to gstack `/office-hours` or `commands/ask-questions.md` before implementation |
| `vague-goal-premise-backlog` | Records unclear premises in `docs-private/premises-*.md`, `commands/premises.md`, or an office-hours backlog; no blocking clarification question |
| `context-load-order` | Shows `AGENTS.md` -> `SKILL.md` -> `protocols/chunk-review.md` load order before answering the review-path question |

## Compaction resume drill (procedural, no LLM)

Always runs in `./tests/run.sh` via `tests/bash/test-compaction-resume-drill.sh`:

```shell
python3 scripts/hosts/controller/compaction_resume_drill.py \
  --directory "$(git rev-parse --show-toplevel)" \
  --fast-tests --json
```

Optional full suite after compaction handoff (slow, maintainer only):

```shell
export GOALFLIGHT_COMPACTION_DRILL_FULL=1
bash tests/bash/test-compaction-resume-drill.sh
```

Fixture handoff when no local `docs-private/RESUME-NOTES*.md`:
`tests/fixtures/compaction_handoff/RESUME-NOTES.md`

Add scenarios in Wave 2+: `delegation-evidence`.
