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

The listener holds the well-known generation lock, writes an ARMED audit row,
and terminates as soon as any assignment is newer than the controller cursor. Its
exit is only a doorbell: no envelopes, count, backlog flag, or receipt token are
delivered through the listener. Peek authoritative mail after the wake:

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

Peek again to derive whether more remains. A second same-generation listener loses
the well-known lock before it can supersede the healthy doorbell. Superseded,
orphaned, stale-lease, corrupt, upgrade-required, and journal-unavailable exits remain
durable audit rows. The listener never renews the controller lease.

The held-lock ledger, not coverage rows or `ps` output, drives the missing-listener
reminder. Coverage rows retain audit and supersession history only.

Use `goalflight_status.py --wait <ids>` only for an unclaimed fixed-set join. Its mail
watermark is journal-derived and monotonic: admission never creates a new position at
or below the stream high-water, and cursor advancement cannot erase the wake.
