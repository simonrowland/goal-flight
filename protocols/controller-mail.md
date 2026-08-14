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

## One-shot listener

Arm exactly one background listener for the active lease:

```bash
python3 <skill-root>/scripts/goalflight_messages.py listen \
  --project-root "$PWD" \
  --controller-label "$GOALFLIGHT_CONTROLLER_LABEL" \
  --lease-nonce "$GOALFLIGHT_CONTROLLER_LEASE_NONCE"
```

The listener writes an ARMED coverage row, waits for a waking journal assignment,
returns at most `K` envelopes plus `more_pending`, prints a generation-stamped
`cursor_token`, writes its EXITED row, and terminates. Process the batch, then re-arm
with the previous token to CAS-advance `(registry_generation, cursor_version)`:

```bash
python3 <skill-root>/scripts/goalflight_messages.py listen \
  --project-root "$PWD" --controller-label "$GOALFLIGHT_CONTROLLER_LABEL" \
  --lease-nonce "$GOALFLIGHT_CONTROLLER_LEASE_NONCE" \
  --cursor-token '<previous-token>'
```

A stale token loses without advancing. If backlog remains, the re-armed listener wakes
immediately. A second listener supersedes the first coverage row. Superseded,
orphaned, stale-lease, corrupt, upgrade-required, and journal-unavailable exits are
all durable rows. The listener never renews the controller lease.

Coverage rows, not `ps` output, drive the missing-listener reminder. PID/start-token
measurement only verifies a stored row. Lease expiry with work needing care creates a
journal attention item whether detected by the listener or by the renewal-horizon
sweep.

Use `goalflight_status.py --wait <ids>` only for an unclaimed fixed-set join. Its mail
watermark is journal-derived and monotonic; cursor advancement cannot erase the wake.
