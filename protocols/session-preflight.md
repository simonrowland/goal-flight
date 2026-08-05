# Session Pre-Flight Protocol

Run before non-trivial goal-flight commands. Keep output compact.

0. Join or register this controller's durable name before status, listening,
or dispatch. Inspect the roster first; each line is
`<label> | idle <duration> | <incarnation-state> | unread <count> | owned <count>`:

```bash
python3 <skill-root>/scripts/goalflight_session_status.py --list-controllers
python3 <skill-root>/scripts/goalflight_session_status.py --join battery-main
python3 <skill-root>/scripts/goalflight_session_status.py --register battery-bugs
```

Names outlive processes. `--join` succeeds an idle incarnation, while a recent
heartbeat reports `controller_label_conflict` without silently replacing it;
`--acknowledge-controller-conflict` records a cooperative takeover decision.
Both register and join work without a PID and create a legitimate
`heartbeat-only` incarnation. If a long-lived controller PID is available,
export it before register, join, or startup so the roster can also measure
live-pid, dead-pid, or pid-unmeasurable state:

```bash
export GOALFLIGHT_CONTROLLER_PID=<long-lived-controller-pid>
python3 <skill-root>/scripts/goalflight_session_status.py --controller-startup
```

`--controller-startup` and `--claim-session` resolve the repo-name default and
register it when absent or join it when present. Repeating startup for the same
PID generation is idempotent. Activity, not a timer, is the heartbeat: register,
join, claim, dispatch ownership stamping, and listener arming update
`heartbeat_at` in the existing session map. Heartbeat-only ownership resolution
uses a 15-minute recency window, aligned with controller progress cadence; after
that, rejoin before dispatch.

Inside a one-shot agent harness, resolve the durable host from the measured
process ancestry instead of exporting the transient command shell:

```bash
python3 <skill-root>/scripts/goalflight_session_status.py \
  --controller-startup --controller-pid-from-ancestry
```

The resolver skips the helper's transient POSIX session, then claims the
measured leader of the first outer session; it refuses PID 1 and refuses when
that leader is not present in the parent chain. The measured process-start token
is carried into the claim; if the numeric PID is recycled between selection and
claim, the claim fails rather than adopting the replacement. Carry the returned
`session.pid` as `GOALFLIGHT_CONTROLLER_PID` (or dispatch
`--controller-beacon-pid`) on later listener and dispatch invocations so their
ownership lookup uses the same beacon.

The repo-name default is measured from the project root after applying the task store's
managed-worktree stripping, so a main checkout and its managed linked worktrees
use one name. Set `GOALFLIGHT_CONTROLLER_LABEL=battery-main` (or pass
`--session-label battery-main`) only when multiple controllers share one repo;
an explicit label wins. If startup reports `controller_label_conflict`, inspect
the roster and choose a distinct name, wait for idle succession, or explicitly
acknowledge a cooperative takeover. It never mints a variant.

`--session-pid` explicitly overrides `GOALFLIGHT_CONTROLLER_PID`. When supplied,
never
substitute the one-shot helper PID, dispatcher PID, project root, or a worker
parent. A declared helper PID is refused because measurement proves it ends
with the claim invocation. A live parent in the same POSIX session is accepted
with a `controller_pid_lifetime_suspicious` warning: session membership alone
cannot prove whether that parent outlives the helper. Do not combine the ancestry flag with an
explicit PID. A missing PID, unavailable durable ancestry, an unresolvable
project root, relabel attempts, same-label conflicts, and claim errors return
`claimed: false` but exit zero: identity improves observability and must not
block work. Re-running this step for the same live PID and label is idempotent;
an existing unlabeled beacon for that exact process generation may adopt its
first resolved label.

Later dispatch commands inherit the PID and any explicit label (or use
`--controller-label <name>`). PID-backed incarnations require the resolved label,
PID, and start-token pairing; heartbeat-only incarnations require the label and
a heartbeat no older than 15 minutes. Durable queue replay carries the available
lookup fields and re-measures the registry at launch. No measured match means
honestly unowned. Addressed mail resolves the durable registry name, not a live
PID: idle registered names remain deliverable and unread until their next
incarnation joins.
Different labels coexist. Multiple live beacons with one label remain visible
through `conflicting_beacons` and dispatches under that ambiguous label stay
unowned until the conflict is resolved. Beacon records also store the OS
process-start generation, so a recycled numeric PID does not revive stale
ownership. The repo-name default covers the common single-controller case;
same-repo controllers such as `battery-main` and `battery-bugs` declare distinct
labels.

Controller names are scoped by canonical project root. New controller-addressed
envelopes carry both fields; matching a label in another repository never
delivers the envelope. A deliberate cross-project post must name the target root:

```bash
python3 <skill-root>/scripts/goalflight_messages.py post \
  --dispatch-id cross-project-note --type controller-notice \
  --to-controller battery-main \
  --controller-project-root /absolute/path/to/target/repo \
  --text 'message'
```

Heartbeat timestamps are trusted only from 60 seconds in the future through the
15-minute recency window. A farther-future heartbeat-only row shows
`clock skew: future heartbeat` / `future-skew` and does not block succession;
a measured live PID still does, even when its wall-clock heartbeat is old.

Retire a completed side role only after joining it, carrying the returned
incarnation id between CLI processes, reading or verifying its mail, and
checking its owned work:

```bash
join_json="$(python3 <skill-root>/scripts/goalflight_session_status.py --join battery-engine)"
export GOALFLIGHT_CONTROLLER_LABEL=battery-engine
export GOALFLIGHT_CONTROLLER_SESSION_ID="$(printf '%s' "$join_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["session"]["id"])')"
python3 <skill-root>/scripts/goalflight_messages.py relay --new --bodies
python3 <skill-root>/scripts/goalflight_session_status.py --retire battery-engine
```

Retirement authenticates that session id (and the PID generation for PID-backed
incarnations), so an unrelated caller cannot retire a heartbeat-only role or
impersonate it in `retired_by`. It reports other live incarnations,
non-terminal owned dispatches, and a dispatch resolution still between registry
lookup and its first ledger record.
After resolving them, or deliberately accepting them in the cooperative fleet,
repeat with `--acknowledge-retirement`. Retirement writes a mailbox digest with
who/when metadata, commits the retired registry row, then advances only that
addressed snapshot. This digest -> registry -> cursor order makes every crash
prefix honest, and retry reuses the same digest. Source JSONL is never deleted.
Retired names leave the default roster, remain visible with
`--include-retired`, and later addressed mail is preserved-undeliverable.

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
