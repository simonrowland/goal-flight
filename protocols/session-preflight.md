# Session Pre-Flight Protocol

Run before non-trivial goal-flight commands. Keep output compact.

0. Auto-claim this controller's journal lease before status, listening, or dispatch.
Goal Flight entry points and compaction resume perform the same role-aware claim; an
explicit preflight is useful when you need the returned capability:

```bash
python3 <skill-root>/scripts/goalflight_session_status.py --controller-startup --controller-pid-from-ancestry
python3 <skill-root>/scripts/goalflight_session_status.py --list-controllers
```

The repo-name default uses the task store canonicalizer, so a main checkout and linked
worktree share one lease. Set `GOALFLIGHT_CONTROLLER_LABEL` only when multiple
controllers share a project. Carry the returned `session.pid`, label, and
`session.lease_nonce` on later controller/listener operations.

A repeated claim from the same measured PID/start-token generation renews the
lease. A child of that recorded holder reconnects the same generation even when
a liveness probe is UNKNOWN. A proven-live different generation returns visible
`label in use`; it is not stolen. Use `--join <name> --acknowledge-controller-conflict`
only for an explicit cooperative succession. Listener, drainer, mirror, and dashboard
roles never claim or renew; a nonce-carrying verified watchdog tick may renew the
controller lease.

The lease, not a process scan or heartbeat JSON map, is liveness authority. It is
keyed by canonical project, label, and nonce and has one active generation. Stored
PID/start-token identity verifies PID-backed principals; the renewal horizon bounds
all principals. Expiry with work needing care creates a journal attention item.

Dispatch replay children verify the incumbent lease and stamp ownership only on an
exact match. A mismatch is visible and leaves the dispatch unowned; it never silently
clears ownership and proceeds as though the claim succeeded.

`--controller-startup` result semantics are fail-closed. `claim_conflict` provides
no usable ownership capability, so a controller or dispatcher must treat itself as
unregistered even when a late fault leaves the journal's committed state uncertain.
Re-read the controller roster, then retry the whole startup claim once with the same
label: a settled competing claim becomes `label_in_use`, while a repeated
`claim_conflict` is a journal/storage fault that requires operator attention.
`--takeover` is not a retry for `claim_conflict`.

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
