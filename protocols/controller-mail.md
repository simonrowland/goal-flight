# Controller mail

One shared journal authority with JSONL carrier projection, not a private markdown drop.

## Claim the controller lease

Goal Flight controller entry points and compaction resume auto-claim a lease using the
canonical project root. The claim is role-aware: listeners, drainers, mirrors, and
dashboard children never claim or renew. A verified watchdog tick may renew the
controller lease. A live different generation
is never stolen; `label in use` is visible and requires an explicit succession choice.

Inspect or establish identity directly when needed:

```bash
python3 <skill-root>/scripts/goalflight_session_status.py \
  --controller-startup --controller-pid-from-ancestry --project-root "$PWD"
python3 <skill-root>/scripts/goalflight_session_status.py \
  --list-controllers --project-root "$PWD"
```

Carry the returned controller label and `session.lease_nonce` (as
`GOALFLIGHT_CONTROLLER_SESSION_ID` or an explicit `--controller-session-id` on later
entry calls). The lease is keyed by canonical project, label, and nonce; ancestry
without the nonce cannot renew it. Only the controller or a verified watchdog tick
renews it.

## Peek

`relay` is peek-only. It never advances a cursor or acknowledges an item.

```bash
# one FROM/subject headline per journal-pending assignment
python3 <skill-root>/scripts/goalflight_messages.py relay --new
# full pending envelopes
python3 <skill-root>/scripts/goalflight_messages.py relay --new --bodies
# one carrier thread for diagnostics; also read-only
python3 <skill-root>/scripts/goalflight_messages.py read --dispatch-id <id> --last 4 --json
```

Do not hand-parse JSONL or look for `.read-cursor.json` / `.ack-cursor.json`; those
surfaces do not exist. The journal cursor is the sole delivery position.

`relay --drain` is the explicit composed read-receipt path. It prints one bounded
`[type] stream seq=N — payload head` line for every item before the terse cursor
receipt; `--json` carries those same receipted envelopes in `items`. Signal
(`merge-request`, `patch`, `finding`, `controller-question`, `user_need`,
`blocked`) sorts first; worker-terminal notices whose only content is a state
change sort last. Nothing that arrived is hidden, and the drained count still
matches the snapshot. Use it only when reading those headlines settles the work.
If an item needs its body or any other processing, peek first (`relay --new`
keeps arrival order) and run the emitted exact `advance` command only after
that processing finishes. Receipt means settled, not merely observed.

A worker-terminal item's headline is the worker's own `COMPLETE` / `BLOCKED`
marker text when one was harvested — including when the watcher called the run
`idle_timeout`. The state string is the fallback, not the default.

## Send

```bash
python3 <skill-root>/scripts/goalflight_messages.py post \
  --dispatch-id <topic-slug> --type user_need \
  --to-controller <label> --subject '<one scannable line>' \
  --text '<body>'
```

`--to-controller` uses a durable label. `--controller-project-root` defaults to the
current canonical git project and is needed only for explicit cross-project mail.
Producers record the journal assignment before projecting the JSONL carrier; retry
heals an unprojected assignment rather than creating a second store.

## Patch flow — mail is how work leaves a controller

Do not push to the remote to land a branch. Post typed mail; the receiving
controller applies it.

```bash
python3 <skill-root>/scripts/goalflight_messages.py post \
  --dispatch-id <topic-slug> --type merge-request \
  --to-controller <label> --subject '<what the patch does>' \
  --text "$(git format-patch --stdout origin/main)"
```

`patch` is the same apply path with a unified diff. Findings and questions use
the same `post` command with `--type finding` or `--type controller-question`.

`relay --drain` leads with those types. A merge-request/patch line is one line:
what it carries, who sent it, and the next command (`git am` for an mbox
patch, `git apply` otherwise). The body is in the receipted envelope
(`relay --drain --json`, or `read --dispatch-id <id> --last 1`). Worker-terminal
state-change notices still appear; they sort after signal.

## Pop-one listener pool

Arm a pool of four tracked background tasks for the active lease. Four is the default
slot count — depth is resilience, not efficiency, and survives three missed re-arms.
Repeat this same command four times through the host's tracked-background-task
surface:

```bash
python3 <skill-root>/scripts/goalflight_messages.py listen \
  --project-root "$PWD" \
  --controller-label "$GOALFLIGHT_CONTROLLER_LABEL" \
  --lease-nonce "$GOALFLIGHT_CONTROLLER_LEASE_NONCE" \
  --report-pending
```

Each listener holds one `listener-slot-N` lock. When foreign waking mail is newer
than the cursor, the listeners race to stamp that cursor version; exactly one wins
and exits, while the other stays blocked. Cursor advance changes the version, so
leftovers or later arrivals permit exactly one fresh ring from the survivors. The
stamp persists independently of process lock ownership, making crash staleness a
version comparison rather than a guess about which process lived.

Self-authored events carry an author digest derived from the lease capability the
poster actually presented. Only that digest can suppress a ring or plain relay
headline; source labels are descriptive metadata and never prove authorship. Events
without a proven author digest remain visible. Self-authored rows remain cursor items
and are advanced as read receipts by a drain alongside foreign mail.

`--report-pending` prints only terse pending headlines followed by one exact
`advance: <command>` line; the full pending-at-arm object is available only with
`--json`. The listener stays armed above that reported high-water. A normal ring is
still only a doorbell; peek authoritative mail after it when processing needs more
than the drain headlines:

```bash
python3 <skill-root>/scripts/goalflight_messages.py relay --new --json
```

After processing the returned items, advance exactly their server-known positions and
carry the snapshot's cursor version and per-stream tokens, then re-arm. Producer
admission is strictly monotonic per stream: a new explicit sequence at or below that
stream's high-water is renumbered to the next position. Each token fingerprints the
recipient-visible live range at or below its requested position, including projection
state. Advance rejects if that exact range changed or still contains an unprojected
row. A fabricated position, a future version, an already-advanced position, or a stale
lease loses; aggregate version churn from another stream or from an arrival above the
requested position does not invalidate a safe command:

```bash
python3 <skill-root>/scripts/goalflight_messages.py advance \
  --project-root "$PWD" --controller-label "$GOALFLIGHT_CONTROLLER_LABEL" \
  --lease-nonce "$GOALFLIGHT_CONTROLLER_LEASE_NONCE" \
  --cursor-version <version> --stream-snapshot '<stream>=<token>' \
  --position '<stream>=<seq>'
```

The tidy steady-state loop is:

1. Keep four tracked `--report-pending` listeners armed — four separate
   tracked background calls, never a shell `&` loop (one harness task
   cannot own four doorbells).
2. One rings; use `relay --drain` when its headlines settle every item, or peek,
   process bodies, and advance explicitly.
3. Re-arm toward target: the listen exit (and a lease claim while work is
   in flight) prints the exact remaining-depth commands, numbered, one
   per missing slot. Issue each as its own tracked background task.

Never advance before processing is settled. If all slots are occupied, startup
reports their exact PIDs and says not to kill by pattern. If all listeners have rung
and none was re-armed, entry hints report `n=0`; if the default pool is short, they
report `n=1/4` and the exact `--report-pending` re-arm command once per missing slot. A listener
reparented to PID 1 waits through a short startup grace, then refuses with one exact
re-arm command: its exit cannot wake an untracked parent. Superseded, orphaned,
signal, stale-lease, corrupt, upgrade-required, and journal-unavailable exits remain durable
audit rows. A listener never renews the controller lease. Exit 4 is the detached
refusal; it is not POSIX 128+signal. Bulk exit-144 reports (128+16, SIGURG on
macOS) are not detached-listener deaths — detached exits 4.

Supervisors must branch on the listener's exit code instead of blindly restarting:

| Code | Meaning | Supervisor action |
|---:|---|---|
| 0 | Ring: waking mail won the cursor-version claim. | Process the reported or authoritative mail, advance only settled positions, then issue each printed remaining-depth command as its own tracked background task. |
| 1 | Timeout: no waking event arrived before the requested deadline. | Treat it as a clean timer expiry; re-arm only when ongoing coverage is still required. |
| 2 | Infrastructure or corruption failure. | Preserve the one-line diagnostic, repair or escalate the journal/wake substrate, and avoid a restart loop until the fault is cleared. |
| 3 | Contention, supersession, orphaning, or stale lease. | Reconcile the active lease and held slot PIDs; do not kill by pattern, and re-arm only under the current lease. |
| 4 | Detached-listener refusal: its exit cannot wake a tracked controller. | Use the emitted command to launch a tracked background listener; do not detach it again. |
| 128+N | POSIX signal N. On macOS 144 is SIGURG (16). | SIGURG is logged and the listener stays alive (kernel default is discard). A terminating signal prints who/why, releases the slot, and exits 128+N. Empty 144 is a harness report, not this refusal. |

The held-lock ledger, not coverage rows or `ps` output, drives the missing-listener
reminder. Coverage rows retain audit and supersession history only.

Use `goalflight_status.py --wait <ids>` only for an unclaimed fixed-set join. Its mail
watermark is journal-derived and monotonic: admission never creates a new position at
or below the stream high-water, and cursor advancement cannot erase the wake.
