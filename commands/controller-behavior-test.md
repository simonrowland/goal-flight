---
description: "Run scripted orchestrator behavior scenarios (Codex-first bash-tail harness)."
---

# controller-behavior-test [--controller codex] [--scenario doctor-loads]

Read:

- `docs-private/plans/controller-behavior-harness-plan.md` (when present locally)

Runs the file-backed orchestrator behavior harness. Wave 1 implements **Codex +
bash-tail** with the `doctor-loads` scenario. The Claude Code regression path
uses `claude-code-cli-acp` through Goal Flight ACP so it stays on the
subscription-routed interactive shim. The multi-host live tier enumerates
available orchestrators from `scripts/hosts/controller/probe_matrix.py`, filters
them through `GOALFLIGHT_LIVE_CONTROLLERS`, and runs every registered scenario
against that intersection.

## Host runners

| Orchestrator | Transport | Bash test | Transcript path |
|------------|-----------|-----------|-----------------|
| `codex` | bash-tail `codex exec` | `tests/bash/test-controller-behavior-codex.sh` | harness temp tail |
| `claude-acp` | ACP shim via `claude-code-cli-acp` | `tests/bash/test-controller-behavior-claude-code-acp.sh` | `docs-private/reviews/<date>-chunk-15/<scenario>.transcript.log` |
| env-gated available set | host runner from `behavior_scenario.py` | `tests/bash/test-controller-behavior-multi-host.sh` | `docs-private/reviews/<date>-chunk-16/<host>-<scenario>.transcript.log` |

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

# Full live matrix. Empty env gate skips exit 0.
export GOALFLIGHT_LIVE_CONTROLLERS=codex,claude-acp
bash tests/bash/test-controller-behavior-multi-host.sh
```

## Multi-host summary JSON

The multi-host runner writes
`docs-private/reviews/<date>-chunk-16/summary.json` unless
`GOALFLIGHT_CONTROLLER_SUMMARY_JSON` overrides it.

```json
{
  "schema": "goalflight.controller-behavior.multi-host.v1",
  "generated_at": "2026-05-28T00:00:00+00:00",
  "env_gate": "GOALFLIGHT_LIVE_CONTROLLERS",
  "requested_controllers": ["codex", "claude-acp"],
  "available_controllers": ["codex", "claude-acp"],
  "supported_controllers": ["codex", "claude-acp"],
  "selected_controllers": ["codex", "claude-acp"],
  "unknown_controllers": [],
  "unavailable_controllers": [],
  "available_unsupported_controllers": [],
  "scenarios": ["doctor-loads"],
  "transcript_dir": "docs-private/reviews/2026-05-28-chunk-16",
  "results": [
    {
      "controller": "codex",
      "scenario": "doctor-loads",
      "status": "pass",
      "ok": true,
      "skipped": false,
      "returncode": 0,
      "transcript_path": "docs-private/reviews/2026-05-28-chunk-16/codex-doctor-loads.transcript.log",
      "json_path": "/tmp/controller-behavior-multi-host-results-123/codex-doctor-loads.json",
      "stderr_path": "/tmp/controller-behavior-multi-host-results-123/codex-doctor-loads.err",
      "elapsed_s": 12.3,
      "skip_reason": null,
      "check_ids": ["doctor_invoked_or_cited"]
    }
  ],
  "totals": {"passed": 1, "failed": 0, "skipped": 0}
}
```

## Skip policy

- `./tests/run.sh` includes `tests/bash/test-controller-behavior-codex.sh`, which
  **skips exit 0** unless `GOALFLIGHT_CONTROLLER_BEHAVIOR=1` and `codex` is on PATH.
- `./tests/run.sh` includes `tests/bash/test-controller-behavior-claude-code-acp.sh`, which
  **skips exit 0** unless `GOALFLIGHT_CONTROLLER_BEHAVIOR=1` and `claude-code-cli-acp` is on PATH.
- `./tests/run.sh` includes `tests/bash/test-controller-behavior-multi-host.sh`, which
  **skips exit 0** unless `GOALFLIGHT_LIVE_CONTROLLERS` names at least one available orchestrator.
  Available orchestrators not implemented by `behavior_scenario.py` are reported
  as `available_unsupported_controllers`; an unsupported-only env gate exits nonzero.
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
| `compaction-reload-in-skill-continuation` | After compaction, reloads fresh `SKILL.md`, stays in-skill by dispatching workers, and gates review before commit |
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
