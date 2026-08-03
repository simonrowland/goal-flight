# Session Pre-Flight Protocol

Run before non-trivial goal-flight commands. Keep output compact.

0. Register this controller's declared identity before status, listening, or
dispatch. The host/controller launcher must export the PID of the actual
long-lived controller process and one stable, meaningful label:

```bash
export GOALFLIGHT_CONTROLLER_PID=<long-lived-controller-pid>
export GOALFLIGHT_CONTROLLER_LABEL=battery-main
python3 <skill-root>/scripts/goalflight_session_status.py --controller-startup
```

`--session-pid` and `--session-label` explicitly override the two environment
values. Never substitute the one-shot helper PID, dispatcher PID, project root,
or a worker parent. Missing inputs, relabel attempts, same-label conflicts, and
claim errors return `claimed: false` but exit zero: identity improves
observability and must not block work. Re-running this step for the same live
PID and label is idempotent; an existing unlabeled beacon for that exact process
generation may adopt its first explicit label.

Later dispatch commands inherit both variables (or use `--controller-label
<name>` while inheriting the PID) to select this exact beacon. The label and
PID must both match; a failed or skipped claim therefore cannot capture another
controller with the same label. Durable queue replay carries this declared pair
but re-measures the beacon at launch. No measured match means honestly unowned.
Different labels coexist. Multiple live beacons with one label remain visible
through `conflicting_beacons` and dispatches under that ambiguous label stay
unowned until the conflict is resolved. Beacon records also store the OS
process-start generation, so a recycled numeric PID does not revive stale
ownership. There is deliberately no project-derived label default: one project
may have `battery-main` and `battery-bugs` controllers at the same time.

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
