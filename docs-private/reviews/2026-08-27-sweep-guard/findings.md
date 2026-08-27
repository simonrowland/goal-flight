# Sweep B — guards that report without protecting; actions that report intent as outcome

Range: `75b63e5..de00f6c` (HEAD = `de00f6c`). Analysis only. Claims below are
**OBSERVED** unless marked **HYPOTHESISED**.

Class predicates:

- **B1** — a check detects an unsafe condition and proceeds anyway (warn where
  it must refuse; two writers, lost work, or corruption if the line is unread).
- **B2** — a report describes what was attempted, not what was observed
  (especially: a live thing reported as absent, or unsafe reported as safe).

---

## P1 — B1: second bash worker into the same `--cwd` is launched, not refused

**Anchor:** `scripts/goalflight_dispatch.py:1481` (`_worker_cwd`) and
`:14284` (`_spawn_daemonized_process`). Contrast:
`scripts/goalflight_worktree_pool.py:372` (`WorktreeSeatUnavailable` names the
incumbent).

**Failure:** Two bash-shape dispatches with the same `--cwd` both print
`DISPATCH-START` / `DISPATCH-LAUNCHED` and leave two live workers in one tree.
No occupancy warning, no incumbent named, no override flag. If the operator
does not notice, that is two writers with no merge discipline — the live
incident this class is named for, still open on the default bash `--cwd` path.

The managed ACP `--worktree create` path *does* refuse (see searched-and-clean).
This range walked past the bash `--cwd` hole while adding drain/ledger/fleet
work in `goalflight_dispatch.py`.

**Verified (OBSERVED):** Isolated env, `--unregistered-forced --launch-detached`,
dummy `python3 -c 'import time; time.sleep(25)'`, same `--cwd`.

| dispatch | rc | outcome |
|---|---|---|
| `occ-a` | 0 | `DISPATCH-LAUNCHED`, worker_pid 81553 |
| `occ-b` | 0 | `DISPATCH-LAUNCHED`, worker_pid 83400 |

Stderr had the unregistered-forced consent text (we passed the override) and
no occupancy language. Both workers were kernel-live until SIGKILL.

`--unregistered-forced` was required because the *controller-registration*
gate now refuses (clean B1). Occupancy is a separate gate that is missing
on this path.

---

## P1 — B2: unreadable journals index renders as an empty fleet

**Anchor:** `scripts/goalflight_journal.py:762` (`iter_journal_files`) and
`scripts/goalflight_controllers.py:983` (`collect_controller_rows`).

**Failure:** `Path.glob` on a journals index that exists but cannot be listed
returns `[]` without raising. `collect_controller_rows` then emits zero rows
and no unknown/error. The new fleet table looks like “no controllers” when
the index was unreadable. That is a live thing reported as absent — the
expensive direction. An operator who trusts the empty table can start a
second controller into a project that already has one.

The sibling GC scanner does this correctly: `goalflight_journal_gc.scan`
uses `store.iterdir()` and on `OSError` returns a single `unknown` entry
(`:498-504`). `iter_journal_files` was added in this range and uses glob
instead.

**Verified (OBSERVED):** Same tree, `chmod 000` on the journals index:

| probe | result |
|---|---|
| `gc.scan(index)` | `[unknown / journal store unreadable (PermissionError)]` |
| `journal.iter_journal_files()` | `[]` |
| `collect_controller_rows(...)` | `[]` (zero rows, no error) |
| `os.listdir(index)` | `PermissionError` |
| `gc._presence(index)` | `present` (lstat succeeds; the dir exists) |

Restored mode `0755` after the probe. Live machine journals were not touched
(`GOALFLIGHT_JOURNAL_DIR` pointed at a temp root).

---

## P2 — B1: git-base-pin mismatch warns, then launches

**Anchor:** `scripts/goalflight_dispatch.py:2377` (`_git_pin_warning`),
`:2419` (`_dispatch_warnings` skips the check entirely when `raw_argv` is
set), `:2433` (`_emit_dispatch_warnings`), `:13973` (emit, then spawn).

**Failure:** A prompt whose pin does not match `--cwd` HEAD prints
`WARN: GIT BASE PIN MISMATCH ... workers on stale clones will build on the
wrong base` and then `DISPATCH-START`. `_validate_before_side_effects` does
not raise. `--ignore-git-warn` exists as an opt-out, but it is not required.
If the operator does not read the line, the worker builds on the wrong SHA.

`-- <cmd>` workers never even get the warning: `_dispatch_warnings` returns
`[]` whenever `raw_argv` is non-empty.

**Verified (OBSERVED):**

1. Function probe: `_git_pin_warning` returned the mismatch WARN;
   `_validate_before_side_effects(args, [])` raised nothing;
   `_dispatch_warnings(args, ["python3","-c","pass"])` returned `[]`.
2. Preset launch: `--agent grok-code --prompt-file` with pin `deadbee` vs
   HEAD `9e2663c`, `--unregistered-forced`. Stderr contained the WARN line,
   stdout contained `DISPATCH-START` with a live `grok` worker_pid (8517).
   No `DISPATCH-REFUSED`. The process was killed after 8s; the worker had
   already been spawned.

---

## P2 — B2: capacity-status failure reports zero live leases

**Anchor:** `scripts/goalflight_session_status.py:2582` (`_active_leases_for`)
and `:2412` (`aggregate_status` unions `bool(leases_for_project)` into
`active`).

**Failure:** Any non-zero exit, JSON error, timeout, or `OSError` from
`goalflight_capacity.py status --json` returns `[]`. The docstring says
“Best-effort: empty list on any failure.” That is a failed observation
rendered as absence. `aggregate_status` then reports
`active_capacity_leases_in_project: 0`. If queue frontmatter and resume
notes are also inactive, the session verdict is inactive while leases are
live. Live session reported as absent.

Pre-existing helper; this range edited `goalflight_session_status.py` and
walked past it.

**Verified (OBSERVED):** Monkeypatched `subprocess.run` inside the helper to
return `returncode=1`. `_active_leases_for(Path("/tmp"))` returned `[]`.

---

## P3 — B2: journal-gc `--apply` JSON still lists deleted journals as reclaimable

**Anchor:** `scripts/goalflight_journal_gc.py:555` (`apply_deletes` increments
`deleted` after `shutil.rmtree` without re-classifying) and `:704-717`
(`main` reuses the pre-delete `entries` snapshot for counts).

**Failure:** After a successful apply, `deleted` is 1 and the path is gone,
but `counts.reclaimable` is still 1 and the same entry remains
`reclaimable: true` / `state: root_gone`. A consumer that keys on
`reclaimable` after `--apply` thinks work remains. The `deleted` field is
the honest observation; the rest of the report is the scan plan.

`scan()` itself is sound (four-state, re-verify immediately before
`rmtree`, `--apply` required). This is report-shape, not a delete of a live
journal.

**Verified (OBSERVED):** Create a journal, delete its project root, classify
`reclaimable=True`, `apply_deletes`, then inspect the snapshot `main`
would emit: `deleted=1`, `path_exists_after=False`,
`counts_reclaimable=1`, entry still `reclaimable=True`.

---

## Out of class

- `scripts/goalflight_acp_boundaries.py:15` / `goalflight_acp_run.py:2466`:
  cursor/grok auto-mode write dispatches print a permission-boundary WARNING
  and proceed. File was **not** in this range. One line only.

---

## Searched and clean

What was grepped and then read/run, with no live class hit:

- **Managed worktree occupancy (ACP `--worktree create`).**
  `acquire_worktree_seat` with `GOALFLIGHT_WORKTREE_SEATS=1` raised
  `WorktreeSeatUnavailable: all 1 worktree seats are held: wt-1=first-dispatch pid=…`
  (incumbent named, no warning-and-proceed). OBSERVED.
- **Unregistered controller at launch.** Default path refuses with
  `DispatchUsageError` unless `--unregistered-forced`. OBSERVED (first
  occupancy probe returned 64 before the override).
- **Drain launch accounting.** `goalflight_dispatch.py:12209-12255`:
  `DISPATCH-LAUNCHED` in stdout is not launch proof; ledger token match is
  required; stdout-without-ledger leaves the claim pending.
- **Reconcile-abandoned dry-run.** Range added
  `RECONCILE-ABANDONED dry-run: no ledger record was changed`.
- **journal_gc.scan listing.** `iterdir` + `OSError` → `unknown`, not empty.
  Four-state classify; re-verify before delete; default is report-only.
- **Quota-stuck reap.** `reap_quota_stuck_workers` uses
  `_termination_incomplete` and does not count a kill as reaped unless the
  target is gone. Horizon text now says UNKNOWN rather than implying the
  account is dead (`reset_horizon_clause`, this range).
- **Watcher idle kill.** `goalflight_watch.py:2057-2070`: `worker_alive`
  follows whether the group is gone, not whether SIGKILL was sent.
- **Supervisor identity.** `goalflight_wake.py` now requires
  `goalflight_messages … supervise` in executable position; trailing tokens
  are not a supervisor.
- **CAS vs not-yet-RUNNING.** `goalflight_ledger.py` `cmd_record` reports
  `attempt_not_yet_running` / `retryable: true` instead of fabricating
  `cas_lost` (`de00f6c`).
- **status.json is not a second lifecycle.** Ledger-terminal rows ignore
  sidecar `state` / `liveness_state` (`goalflight_status.py`,
  `goalflight_fleet_console.py`, this range).
- **last_drain_at** comes from the journal cursor’s `advanced_at`, not a
  newly invented “drain just happened” stamp.
- **Zombie descendants** are filtered from live-work counts
  (`watch-dispatch-tail.sh` captures the `ps` table, then awks it, so a
  failed `ps` is `unknown` not zero; missing state token counts live).
- **`pgrep` is not used for worker liveness** in scripts (Golden Master
  still forbids it). Historical self-match kill path is not live here.
- **Pipe `$?` / `| head` on verdicts.** Production
  `live_descendant_count` no longer pipes `ps` into awk for the exit
  status. Remaining `| head` uses are display/parse (`ps_meta`, tests,
  manuals), not “command succeeded because head succeeded.”
- **Post-spawn `DISPATCH-REGISTRATION-WARN`.** After the worker exists,
  pidfile/identity/caffeinate failures warn and keep the watcher;
  `DISPATCH-START` includes `registration_errors`. Covered by
  `case_post_spawn_registration_failure_still_runs_watcher`. Not a
  warn-and-launch of a *second* writer.
- **Steer to a dead pid.** `_worker_liveness_warning` warns and still
  appends. Damage is an unobserved message, not two writers. Not ranked.

Negative searches with no live hit in-range: `warnings.warn`, occupancy
warning strings, `pgrep -f` kill paths, `subprocess.run` in the touched
drain/launch helpers that ignore `returncode` on a launch-success report
(drain checks both `returncode` and ledger).
