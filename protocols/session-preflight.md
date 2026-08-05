# Session Pre-Flight Protocol

Run before non-trivial goal-flight commands. Keep output compact.

0. Register this controller's identity before status, listening, or dispatch.
The host/controller launcher must export the PID of the actual long-lived
controller process. The label defaults to the repository name:

```bash
export GOALFLIGHT_CONTROLLER_PID=<long-lived-controller-pid>
python3 <skill-root>/scripts/goalflight_session_status.py --controller-startup
```

The default is measured from the project root after applying the task store's
managed-worktree stripping, so a main checkout and its managed linked worktrees
use one name. Set `GOALFLIGHT_CONTROLLER_LABEL=battery-main` (or pass
`--session-label battery-main`) only when multiple controllers share one repo;
an explicit label wins. If startup reports `controller_label_conflict`, release
that controller's conflicted beacon by running `--release-session` with
`--session-pid <long-lived-controller-pid>`, set an explicit distinct label,
and run startup again. It never mints a variant.

`--session-pid` explicitly overrides `GOALFLIGHT_CONTROLLER_PID`. Never
substitute the one-shot helper PID, dispatcher PID, project root, or a worker
parent. A missing PID, an unresolvable project root, relabel attempts,
same-label conflicts, and claim errors return `claimed: false` but exit zero:
identity improves observability and must not block work. Re-running this step
for the same live PID and label is idempotent; an existing unlabeled beacon for
that exact process generation may adopt its first resolved label.

Later dispatch commands inherit the PID and any explicit label (or use
`--controller-label <name>`). Without an explicit label they remeasure the repo
default. The resolved label and PID must both match; a failed or skipped claim
therefore cannot capture another controller with the same label. Durable queue
replay carries this resolved pair but re-measures the beacon at launch. No
measured match means honestly unowned.
Different labels coexist. Multiple live beacons with one label remain visible
through `conflicting_beacons` and dispatches under that ambiguous label stay
unowned until the conflict is resolved. Beacon records also store the OS
process-start generation, so a recycled numeric PID does not revive stale
ownership. The repo-name default covers the common single-controller case;
same-repo controllers such as `battery-main` and `battery-bugs` declare distinct
labels.

1. Run procedural status:

```bash
python3 <skill-root>/scripts/goalflight_status.py
```

2. Run doctor when:

- first command in a session
- install/tooling changed
- dispatch/review/capacity behavior looks wrong

```bash
python3 <skill-root>/scripts/goalflight_doctor.py --project-root "$PWD" --json
```

3. Fingerprint behavior-bearing files. Include:

- `SKILL.md`
- `commands/*.md`
- `protocols/*.md`
- `prompts/*.md`
- `templates/*.tpl` and `templates/*.md`
- `scripts/goalflight_*.py`
- `scripts/acp_*.py`

The fingerprint is a drift signal, not a security boundary.

4. Surface only actionable drift:

- multiple goal-flight installs
- loaded fingerprint mismatch vs in-flight queue/resume notes
- missing context-mode on a side that will process large output
- active capacity cooldown or surplus worker-like processes

5. Do not inspect raw logs. If a worker is running, read status JSON or run:

```bash
python3 <skill-root>/scripts/goalflight_status.py
```
