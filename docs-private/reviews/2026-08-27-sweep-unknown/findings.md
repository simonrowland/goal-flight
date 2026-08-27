# Sweep A — unknown collapsed into a determination

Range: `75b63e5..de00f6c`. Class: a check that can fail to find out, whose
result is rendered as a definite answer. Live / dead / **unknown** must stay
three states.

Verified on this machine with isolated `mktemp` state (`GOALFLIGHT_JOURNAL_DIR`,
`GOALFLIGHT_STATE_DIR`, `GOALFLIGHT_WAKE_LEDGER`, `GOALFLIGHT_MESSAGES_DIR`,
`GOALFLIGHT_TASK_STORE`, `GOALFLIGHT_PIDFILE_DIR`, `GOAL_FLIGHT_PIDFILE_DIR`,
`GOALFLIGHT_CAPACITY_CONF=/dev/null`, `GOALFLIGHT_DISPATCH_DIR`). Same outcomes
on `/usr/bin/python3` 3.9.6 and Homebrew Python 3.12.13.

P0: 0 · P1: 2 · P2: 1 · P3: 0

---

## P1 — `Path.glob` reports an unreadable queue as empty, so abandoned reconcile treats a present envelope as gone

**Anchor:** `scripts/goalflight_dispatch.py:8212` (`_claim_has_active_carrier`),
used at `scripts/goalflight_dispatch.py:7562` (`_evaluate_abandoned_dispatch`).

**Failure direction:** unknown → absent. A live queue envelope is the keep-gate
for abandoned close. Missing the envelope is rendered as "no carrier", which is
the expensive direction (safe to terminalize).

**Scenario (inputs → wrong outcome):**

1. `{state}/dispatch-queue/disp-1.json` exists and contains `dispatch_id=disp-1`.
2. A running ledger row for `disp-1` has no recorded worker pid, no capacity
   lease, a missing status file, a missing tail, and stale timestamps.
3. `chmod 000` the queue directory (search/list denied; the envelope is still
   on disk). `queue_dir.is_dir()` remains True because the directory inode
   itself is still `stat`-able.
4. `_claim_has_active_carrier` walks `queue_dir.glob("*.json")` /
   `*.json.claimed-*`. CPython `pathlib` glob swallows `PermissionError` and
   yields nothing (`pathlib.py` `_select_from` `except PermissionError`).
5. The helper returns `ClaimCarrierStatus(kind=NONE)` — the same value as a
   genuinely empty queue.
6. `_evaluate_abandoned_dispatch` therefore skips the keep-reason and returns
   `eligible: True, reason: worker_provably_gone`.

**OBSERVED** (constructed, isolated env):

- Readable queue: carrier `QUEUED` / `queued_envelope`; evaluation
  `eligible: False, reason: active_queue_carrier`.
- Same files, parent mode `000`: `os.listdir` / `iterdir` raise
  `PermissionError`; `Path.glob("*.json")` returns `[]` with no exception;
  carrier `NONE`; evaluation `eligible: True, reason: worker_provably_gone`.
- Restoring mode `0700` restores the keep-reason.

**HYPOTHESISED, not executed:** `reconcile_abandoned_dispatches` (called from
drain via `_reconcile_abandoned_for_drain`) would then
`_commit_abandoned_dispatch` and write a terminal ledger row. The eligibility
predicate is what we ran; we did not let drain mutate a ledger.

Default queue dir is `{state_dir}/dispatch-queue`, a sibling of
`{state_dir}/runs.d`. An unreadable *queue* directory with a still-readable
ledger is enough. Drain lists the same glob for launch candidates, so those
envelopes also vanish from the drain pass.

Related consumers of the same glob swallow: `_summarize_claim_markers`
(`scripts/goalflight_dispatch.py:8168`) reports `dead=unknown=live=0`;
`_recover_claimed_queue_entries` (`9984`) sees no `.claimed-*` files.

Contrast: `scripts/goalflight_journal_gc.py:495` uses `store.iterdir()`, which
raises, and maps `OSError` to an `unknown` / non-reclaimable row.

---

## P1 — `os.path.lexists` turns an unreadable present journal into `JournalDisappeared`, then "no such controller"

**Anchors:**

- `scripts/goalflight_journal.py:1109` (`Journal._require_existing_database`)
- `scripts/goalflight_session_status.py:969` (`probe_live_session` maps
  `JournalDisappeared` → `"dead"`)
- `scripts/goalflight_session_status.py:241` (`_probe_registered_controller_records`
  maps `JournalDisappeared` → `[], None` — a measured empty roster)
- `scripts/goalflight_dispatch.py:7516` (`_abandoned_controller_evidence` maps
  `"dead"` → `True, "controller_beacon_absent"`)

**Failure direction:** unknown → dead / absent. A live controller (held kernel
lock, ACTIVE lease) is reported gone. That is the historical class
(`live_session is None`, flaky registry read as "no controllers").

`os.path.lexists` is implemented as `lstat` with `except (OSError, ValueError):
return False`. Permission and I/O errors are indistinguishable from absence.
`JournalDisappeared` is documented as "the configured journal path is
*verifiably* absent". The next `stat` in `_require_existing_database` *does*
split `FileNotFoundError` from other `OSError` into `JournalIOError`;
`lexists` never lets that code run.

**Scenario (inputs → wrong outcome):**

1. Register controller `engine`, hold the generation lock
   (`wake.register_lease_holder`).
2. `probe_live_session` returns `"live"`; abandoned controller evidence is
   `False, live_controller_label_owner`.
3. `chmod 000` the journal's parent directory. The sqlite file and the lock
   remain; we simply cannot `lstat` the sqlite path.
4. `open_reader` → `_require_existing_database` → `lexists` False →
   `JournalDisappeared`.
5. `probe_live_session` returns `"dead"` (same as a project that never had a
   journal). `_abandoned_controller_evidence` returns
   `True, controller_beacon_absent`. The roster probe returns `[]` with
   `error=None` and `measured=True`, so
   `controller_roster_lines` prints nothing — not `controllers unreadable (...)`.

**OBSERVED:**

| state | `probe_live_session` | `_abandoned_controller_evidence` | roster probe |
| --- | --- | --- | --- |
| lock held, journal readable | `live` | `(False, live_controller_label_owner)` | (not needed) |
| lock still held, journal parent `000` | `dead` | `(True, controller_beacon_absent)` | `[], None` (measured empty) |
| mode restored | `live` | `(False, live_controller_label_owner)` | — |
| never created a journal | `dead` | — | `[], None` |

Unreadable and absent are the same pair of return values.

Sibling `lexists` sites that feed the same disappearance verdict:
`_raise_disappeared_if_absent` (`scripts/goalflight_journal.py:1965`),
`open_or_create_journal` (`5146`; unreadable present path tries `Journal.create`).

`journal_gc._presence` on the same unsearchable path returns `"unknown"`. That
is the correct three-state split this open path lacks.

Drain still has other gates (pid identity, lease map, output, staleness). This
finding is that the *controller* gate, which is supposed to veto close while
the holder is live, is falsified by an unreadable journal. Combined with
finding 1, both "is anyone home?" checks fail open.

---

## P2 — `iter_journal_files` renders an unreadable journals index as "no journals"

**Anchor:** `scripts/goalflight_journal.py:762`. Consumer:
`scripts/goalflight_controllers.py:983` (`collect_controller_rows`).

**Failure direction:** unknown → absent. Fleet table shows zero members.

The helper's own comment says not to drop an unreadable *file* with `is_file()`,
then the listing path does two collapses:

1. `base.glob(...)` swallows `PermissionError` and yields `[]` (same stdlib
   behavior as finding 1).
2. `except OSError: return []` would also collapse a raised listing error into
   empty.

**OBSERVED:** two journals in the index; `iter_journal_files` length 2.
`chmod 000` the index directory: `iter_journal_files()` → `[]`;
`collect_controller_rows` → `[]` (count 0). Restore → length 2.

No automatic delete rides this list (`journal_gc.scan` uses `iterdir` and keeps
unknown). Damage is operator/diagnostic: a flaky or unreadable index looks like
an empty fleet, which is the "failed read rendered as no controllers" shape.

---

## Searched and clean

These range-touched surfaces were read and, where the class could fire, exercised.
They keep unknown distinct from no, or fail closed in the expensive direction.

- **`scripts/goalflight_journal_gc.py`.** `_presence` (`64`) is `lstat` with
  `FileNotFoundError` → absent, other `OSError` → unknown. Observed `"unknown"`
  on the same unsearchable path that makes `lexists` False. `scan` (`492`) uses
  `iterdir` and keeps the store as unknown; `classify` never sets
  `reclaimable=True` on unknown; lock-without-sqlite stays unknown (`a5c725b`).
- **`probe_live_session` busy/IO arms.** `JournalBusy` / `JournalIOError` /
  remaining `JournalError` return `"unreadable"` (`973-978`). Abandoned
  reconcile maps that to `controller_indeterminate` and does not close
  (`7513-7515`). The hole is only the `JournalDisappeared` arm after `lexists`.
- **`pid_liveness` / `pid_alive`.** Three-state probe; boolean view is
  `is not False` (unknown counts live). Cleanup paths that `if not pid_alive`
  therefore cannot authorize kill/unlink from a failed probe
  (`_cleanup_pidfile_if_worker_dead` `7166`, `_reap_dead_worker_pgroup` `7137`).
- **`_abandoned_process_evidence` / `_queue_claim_identity_status`.**
  Identity probe errors and `identity_indeterminate` stay not-abandoned
  (`6095-6130`, `7350-7448`). `worker_alive: true` without a pid to measure
  fails closed.
- **`Path.exists()` helpers on this platform.** Unsearchable parent makes
  `Path.exists()` / `Path.is_dir(child)` *raise* `PermissionError` on both 3.9
  and 3.12 here. `_abandoned_output_evidence` therefore returns
  `output_unreadable:PermissionError` (False / veto).
  `_read_capacity_state_for_reconciliation` raises; drain's
  `_reconcile_abandoned_for_drain` catches and reports `closed: 0`. Those two
  did **not** collapse on the inputs we could construct. They would still be
  the class on a Python where `Path.exists()` returns False for `OSError`
  (CPython 3.12 docs mention that change for *some* errors; it did not fire
  for `EACCES` here).
- **Wake supervisor argv.** `_probe_supervise_argv` returns match / skip /
  unknown; listing failure is `SUPERVISOR_UNKNOWN` (`2862-2880`, `3117-3156`).
- **Claimer adjudication when the directory is listable.** Filename-only PIDs
  are `UNKNOWN`; start-token required for LIVE/DEAD (`8091-8152`).
- **Drain journal isolation.** Busy/error skips the project for the rest of
  the pass; does not launch from a failed journal read (`11924-11960`,
  `12077`).
- **Quota stuck horizon.** `reset_horizon_clause` names UNKNOWN rather than
  a closed account (`scripts/goalflight_quota_stuck.py:622`).
- **Task store lock.** Contended store raises `TaskError` "store busy" instead
  of hanging until SIGTERM (`goalflight_task.py` `STORE_LOCK_BUDGET_S`).
- **Ledger RUNNING CAS.** `attempt_not_yet_running` is distinct from
  `cas_lost` (`scripts/goalflight_ledger.py` around the `cmd_record` running
  branch).
- **Fleet lock probe.** `_lock_liveness_once` returns unknown on
  `PermissionError` / other `OSError`; retries busy, does not invent dead
  (`scripts/goalflight_controllers.py:257-301`).

## Out of class

None that rose to a one-line serious flag besides the glob/lexists sites
above. Sidecar-vs-ledger lifecycle (range commits `8d5f021` / `406a55c`) is
the opposite defect (alive-from-stale-sidecar) and is already being fenced.

## Method notes

Hunted the predicate from `git diff 75b63e5..de00f6c` / `git log --oneline`
on the 13 production files the range actually changed, then read the
pre-existing helpers those diffs call (`_require_existing_database`,
`Path.glob` consumers, `pid_liveness`, journal-GC `_presence`). Did not
treat the many three-state *fixes* in this range (t-369/t-371/t-333/t-363)
as remaining instances.
