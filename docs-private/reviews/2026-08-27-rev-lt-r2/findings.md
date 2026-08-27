# rev-lt-r2 — refutation of the sidecar-hold fix round

Review of `322c51c` `d8da4d1` `00259a4` `9bc19f2` on `sweep-authority`
(round-1 baseline `c2c6aa5`). Analysis only; source was not left edited.
Revert experiments checked out historical `scripts/goalflight_{ledger,journal,status,fleet_console}.py`
then restored (`git status` clean at `9bc19f2` after restore).

Isolated env: fresh `mktemp -d` for `GOALFLIGHT_JOURNAL_DIR`,
`GOALFLIGHT_STATE_DIR`, `GOALFLIGHT_WAKE_LEDGER` / `_DIR`,
`GOALFLIGHT_MESSAGES_DIR`, `GOALFLIGHT_TASK_STORE` / `_DIR`,
`GOALFLIGHT_PIDFILE_DIR`, `GOAL_FLIGHT_PIDFILE_DIR`,
`GOALFLIGHT_DISPATCH_DIR`, `GOALFLIGHT_FLEET_DIR`,
`GOALFLIGHT_CAPACITY_CONF=/dev/null`. Ambient dispatch/controller identity
unset. Live ledger, queue, and journals were not used. Spawned children
were reaped.

Live journals before and after: the same three named dirs
(`goal-flight-d141dcf5bd`, `pm2-e2d8b5f76d`,
`regolith-pyrolysis-simulator-9ffa1c2362`), **zero** `project-*` children.

Counts: **P0=0, P1=0, P2=0, P3=1**. Verdict: **CLEAN**.

Round-1 confirmed-good still holds. Dead-with-matching-identity still
terminalizes (`committed=1`). A terminal sidecar still does not outrank an
identity-live worker. Pid reuse still resolves to dead. `held: unknown` is
named, distinct from `held: live`, on all three operator surfaces — confirmed
by running the CLIs, not by reading. `case-1j` is **FAILING**, identically on
HEAD / `c2c6aa5` / `c2c6aa5^`; it is not a round-2 regression.

---

## P3 — still-indeterminate holds re-probe and rewrite the ledger every reconcile, with no cap

**Anchors:** `scripts/goalflight_ledger.py:449-458`, `:569`, `:584-609`,
`:1425-1443`, `:1499-1549`

Round 2 is not age-based on this path. That part of the claim holds.

**OBSERVED** (isolated env, sidecar `failed`, no pid, no journal attempt,
`started_at` backdated to `2026-07-01T00:00:00+00:00`):

| pass | `sidecar_hold` | `committed` | ledger `state` | `updated_at` |
|---|---|---|---|---|
| 1 | `unknown` | 0 | `running` | `2026-08-27T22:20:31+00:00` |
| 2 | `unknown` | 0 | `running` | `2026-08-27T22:20:32+00:00` |

`sidecar_terminal_hold` on the same row with `started_at=2020-01-01` still
returns `unknown` / `no_pid`. Attention-item text contains
`not resolved by age`. Nothing in `reconcile_terminal_outbox` or
`sidecar_terminal_hold` consults record age, `elapsed_s`, or a timeout to
promote this row. `expired_launches` (`start_deadline_at` on PREPARED/STARTING)
is a different path.

The re-probe loop is **unbounded**. There is no backoff, no attempt budget, no
stop after N still-unknown passes. A permanently indeterminate record (terminal
sidecar, no ledger pid, no RUNNING journal instance) is held forever and
revisited on every reconcile.

Cost, same isolated journal, 25 additional still-unknown records:

| pass | wall | `committed` | still `sidecar_hold=unknown` | `overruled` len |
|---|---|---|---|---|
| 1 | 6.0294s | 0 | 25 | (first write of items + stamps) |
| 2 | 3.2294s | 0 | 25 | 30 (includes other held rows in the journal) |

Per still-unknown row each pass does: `identity_matches` (returns `no_pid`
without a process-table probe), `resolve_system_attention` (scans every OPEN
`sidecar_terminal_overruled` row for the project), `record_system_attention`
(`INSERT OR IGNORE`), and `_stamp_nonterminal_fields` → `write_record`.
`write_record` always sets `updated_at = utc_now()` (`:569`) and fsyncs. The
stamp is not gated on "value changed", so a hold whose liveness is unchanged
still rewrites the ledger file. That is write amplification, and it applies to
**live** holds as well as unknown: every `liveness != "dead"` continue at
`:1543-1549` stamps again.

`resolve_system_attention` (`scripts/goalflight_journal.py:4279-4293`) walks
all OPEN items of that type per held record, so the attention half is
O(open-items × held-rows) per reconcile, not O(1) per row. Item count does
not grow (uuid5 + `INSERT OR IGNORE`); ledger `updated_at` and fsync count do.

This is not a blind sweeper. It does not terminalize the leftover shape. It
also does not bound it. The changelog's "Unknown is not a permanent leak" is
true only when a later probe can determine identity (RUNNING journal instance
copied onto the record, OBSERVED below, or the process table becomes
readable). The leftover round-1 shape — terminal sidecar, no pid, no journal
instance — still never converges in this function; a human / higher-authority
pass is still the only closer. That is the design they documented in the
commit body. The P3 is the unbounded cost of keeping it.

`reconcile_terminal_outbox` remains CLI-only in production
(`scripts/goalflight_ledger.py:1660`; grep). Drain/status do not call it.

---

## Attack questions

### Round-1 confirmed-good

**Terminal sidecar does not outrank an identity-live worker. OBSERVED.**

Live child, `idle_timeout` sidecar: `committed=0`, ledger `running`, attempt
`RUNNING`, `sidecar_hold=live`, child `poll() is None`.

**Dead-with-matching-identity still terminalizes. OBSERVED.**

Spawn, capture `process_identity`, write `failed` sidecar, SIGTERM + wait,
`process_identity(pid) is None`, then reconcile: `worker_identity_liveness`
`("dead","dead")`, `committed=1`, ledger `failed` / `terminal_state=error`,
attempt `TERMINAL`, `overruled=[]`. Pytest
`test_terminal_sidecar_terminalizes_genuinely_dead_worker` passed on HEAD.

**Pid reuse resolves to dead. OBSERVED.**

Two real children; recorded identity is the first child's start token with
the second child's pid. `("dead","pid_reused_start_token")`, `committed=1`,
ledger `complete`. Tokens differed
(`darwin:…:812103` vs `darwin:…:815202`). OS-level recycle of the same pid
number was not obtained; do not read the re-key as an OS recycle.

HEAD sidecar-gate suite: **9 passed in 14.27s**.

### 1. Unknown holds re-probe each reconcile, not age?

**Not age: yes. Re-probe bounded: no.** See P3.

The positive resolution path works when a RUNNING journal instance exists.

**OBSERVED** unknown then live: first reconcile `sidecar_hold=unknown`, no
pid; `mark_attempt_running` with a live child's identity; second reconcile
copies pid `32331` onto the record, `sidecar_hold=live`, `committed=0`, child
still alive.

**OBSERVED** unknown then dead: same, but the journal identity is from a
reaped child. Second reconcile `committed=1`, ledger `failed`, attempt
`TERMINAL`, `sidecar_hold` popped, overrule item `RESOLVED`.

In-memory copy of journal identity already existed at `c2c6aa5`. `d8da4d1`
makes that copy durable via `_stamp_nonterminal_fields` (`:1425-1443`) so
later operator surfaces that read the ledger row (not the journal) have a
pid.

### 2. Does copying journal identity onto the record create duplicated authority?

**Two homes, one reader for the gate. The `sidecar_hold` stamp is not what
the three named surfaces consult.**

After the live copy: journal `worker_instance_json.pid`, ledger
`worker_pid` / `worker_identity.pid`, and `identity_matches(record)` all
agreed on `32331`. `mark_attempt_running` is STARTING→RUNNING write-once
(`scripts/goalflight_journal.py:4601-4628`), so the journal instance cannot
later disagree with the copy. Hydrate skips once `record.worker_pid` is set
(`:1425`). The gate then consults the **ledger** copy via
`worker_identity_liveness(record)` (`:1524`). That is recorded expected
identity, not a second liveness authority.

The `sidecar_hold` field *is* a liveness cache. **OBSERVED** after killing the
live-held child *without* a second reconcile:

| reader | value |
|---|---|
| disk `sidecar_hold` | `"live"` (stale stamp) |
| `sidecar_terminal_hold(row)` | `None` (re-probe: identity dead) |
| `status._dispatch_cells(reconcile_fast_plane_record(...))` | `"running codex"` (hold popped in memory) |
| `fleet._worker_row` `authority_resolution` | `None` |
| `ledger.status_payload()` `sidecar_hold` | absent |

The three operator surfaces re-probe:

- status one-liners: `reconcile_fast_plane_record` calls `sidecar_terminal_hold`
  (`scripts/goalflight_status.py:803-812`) then `_dispatch_cells` reads the
  in-memory result (`:1648-1654`)
- fleet `authority_resolution`: `_worker_row` runs `reconcile_fast_plane_record`
  first (`scripts/goalflight_fleet_console.py:1982-1997`) then
  `_authority_snapshot` reads `record.get("sidecar_hold")` (`:936-967`)
- `ledger.status_payload()` calls `sidecar_terminal_hold(r)` itself
  (`scripts/goalflight_ledger.py:1730-1733`); it does not copy the stamp

So the stamp can go stale on disk while readers that matter re-probe. That is
not the t-373 class for those surfaces: they do not treat the stamp as
authority. Next reconcile of the dead identity commits the sidecar and pops
the stamp (`:1605-1606`). Not filed.

### 3. `held: unknown` vs `held: live` on all three surfaces?

**Yes. OBSERVED by running, not by reading.**

Isolated live child + unknown row, then:

```
python3 scripts/goalflight_status.py --dispatch cli-held-live --project …
python3 scripts/goalflight_status.py --dispatch cli-held-unknown --project …
python3 scripts/goalflight_status.py --json --project …
python3 scripts/goalflight_fleet_console.py fleet
python3 -c 'print(json.dumps(goalflight_ledger.status_payload()))'
```

| surface | live | unknown |
|---|---|---|
| status CLI stdout | `expected_live codex unknown held: live (live)` | `unknown_no_pid codex unknown held: unknown (no_pid)` |
| fleet CLI `authority_resolution` | `held: live` | `held: unknown` |
| fleet CLI `is_terminal` | `false` | `false` |
| `status --json` `sidecar_hold` | `live` | `unknown` |
| `ledger.status_payload()` `sidecar_hold` | `live` | `unknown` |

Fleet CLI `authority_detail` also names the reason
(`held: live (live)` vs `held: unknown (no_pid)`). A distinction present on
two of three would have been a partial fix; it is on all three.

### 4. `322c51c` — can an overrule item resolve while the disagreement is still real?

**The current-reason item stays OPEN while the hold holds. An earlier
reason is retired when liveness changes. Converge (dead) resolves then
commits.**

**OBSERVED** unknown → live, child still alive, sidecar still `failed`:

- OPEN before: `worker_identity_unknown`
- OPEN after: `worker_identity_live`
- RESOLVED: `worker_identity_unknown`
- `committed=0`, `sidecar_hold=live`

`keep_reason=worker_identity_{liveness}` (`:1526-1530`) is what does that.
The disagreement is still recorded, under the new reason. That is not
re-hiding the overrule.

**OBSERVED** live hold then kill then reconcile: `committed=1`, ledger
`idle_timeout`, OPEN for that dispatch = 0, RESOLVED = 1 with `resolved_at`
set. The sidecar and the process no longer disagree.

**HYPOTHESISED, not claimed:** on the dead/converge path
`resolve_system_attention` (no `keep_reason`) runs at `:1551-1554` *before*
`commit_terminal_authority`. If that commit returned `cas_lost`, the OPEN
item would already be RESOLVED while the sidecar was still uncommitted. Not
forced in this pass.

### 5. `9bc19f2` — real pin or test-shaped?

**Real pin. OBSERVED.**

HEAD tests call `identity_matches` for pre-reconcile checks
(`tests/python/test_ledger_sidecar_terminal_gate.py:74-81`, `:194-196`),
not `worker_identity_liveness`. Against `c2c6aa5^` ledger+journal (gate
absent, helper absent) the live-hold test fails
`assert 3 == 0` (`committed=3` over three identity-live sidecars), and the
dead-worker test still passes (`committed=1`). That is committed-over-live,
not `AttributeError: worker_identity_liveness`. Round 1's P3 was that the
old tests failed on the missing helper. This commit closes that.

New coverage (unknown still-indeterminate, journal-identity converge,
three-surface naming, overrule RESOLVED on converge) matches behavior
independently probed above.

### 6. Re-run every revert

**OBSERVED.** Author's messages, with the revert that produced them:

| author | this pass | how |
|---|---|---|
| `assert None == 'unknown'` | yes | HEAD tests, `c2c6aa5` ledger+journal (and `d8da4d1^` ledger): `stamped.get("sidecar_hold") == "unknown"` |
| `AssertionError: running codex` | yes | HEAD tests, `00259a4^` status+fleet: `assert 'held: live' in 'running codex'` |
| OPEN remains | yes | HEAD tests, `c2c6aa5` ledger+journal: after converge, `_overrule_items` still `state: OPEN` |
| `not resolved by age` | yes | HEAD tests, `c2c6aa5` ledger: `assert 'not resolved by age' in "<old text without that phrase>"` |
| `committed 3 and 1` | yes | HEAD tests, `c2c6aa5^` ledger+journal: live-hold `committed=3` (expect 0); other hold tests `committed=1` (expect 0). Dead-with-identity test still passed. |

`322c51c` journal-only revert (ledger still calls `resolve_system_attention`)
is `AttributeError: 'Journal' object has no attribute 'resolve_system_attention'`,
not OPEN remains. OPEN remains is the paired revert (no method *and* no
call), i.e. round-1 source.

Source restored to `9bc19f2` after every revert.

### 7. `case-1j` and dashboard-pidfile

**dashboard-pidfile: both params passed.** OBSERVED.

`test_dashboard_pidfile_write_distinguishes_contract_from_transient`
parametrized `[error0-True]` and `[error1-False]`: **2 passed in 0.83s**.

**`case-1j` is FAILING, not unverified, and not a round-2 regression.**
OBSERVED.

Isolated pidfile dir, same `run_dead_tail_case` shape as
`tests/bash/test-watch-dispatch-tail.sh:853-868`: tail already contains
`BLOCKED: x` plus `post-marker summary`, worker is `sleep 5`, expected
watcher exit 4. Got exit 1:

```
[watcher] worker PID … is gone (no terminal marker seen after pid-dead grace)
WATCHER-EXIT: pid-dead exit_code=1
```

The same pid-dead exit 1 happens for `FAILED: x` (case-1i shape) and is
**identical** on HEAD, `c2c6aa5`, and `c2c6aa5^` copies of
`scripts/watch-dispatch-tail.sh`. Round 2 does not touch that script.
Carry forward as **FAILING (pre-existing)**; do not treat as fine, and do
not hang it on this fix round.

---

## What is not a finding

- Live worker + terminal sidecar holds, records `overruled`, leaves journal
  RUNNING. OBSERVED.
- Unknown + RUNNING journal identity of a live worker becomes `held: live`
  and does not terminalize. OBSERVED.
- Unknown + RUNNING journal identity of a dead worker terminalizes.
  OBSERVED. This is the round-1 leftover's intended closer when a journal
  instance exists.
- Already-terminal records still re-commit (gate skipped when the record or
  attempt is already final). Read in source; p2/p3 idempotency tests were
  not re-run as a set.
- `sidecar_hold` stamp on disk can lag a just-died worker; the three named
  surfaces re-probe and do not use the stamp as authority.

---

## Method notes

- Interpreter: `/opt/homebrew/bin/python3` (3.14.5, pytest 9.0.3).
- Probe: `docs-private/reviews/2026-08-27-rev-lt-r2/probe_r2.py` and
  `probe-results.json`.
- Pytest: `GOALFLIGHT_ISOLATED_TEST_FILE=test_ledger_sidecar_terminal_gate.py`
  (conftest otherwise collect-ignores the file).
- Revert experiments used `git checkout <rev> -- <files>` and restored with
  `git checkout HEAD -- …`. Working tree clean at `9bc19f2` after.
