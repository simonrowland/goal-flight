# Sweep C — inert tests (`75b63e5..de00f6c`)

Predicate: a test that still passes after the behaviour it claims to pin is
reverted. Method: for each candidate, revert the claimed production change,
run the test, restore. Reading was used only to pick candidates.

Counts: **P0=0, P1=1, P2=0, P3=0**.

---

## P1 — `test_retry_deadline_checked_before_subsequent_attempt` does not pin the deadline-before-attempt-2 fix

**Anchor:** `tests/python/test_controller_fleet.py:1434-1453`
**Production:** `scripts/goalflight_controllers.py:137-169` (`_retry_journal_busy`)
**Introduced:** `e60749f` ("check the fleet busy-retry deadline before later attempts")

### Claimed behaviour

The commit moved the deadline check to *before* starting attempt 2, including
after the backoff sleep. The old loop checked the deadline *after* attempt 1
and *before* sleeping, then started attempt 2 without re-checking. The bug
the change exists to close:

> attempt 1 finishes under budget → backoff sleep crosses the deadline →
> attempt 2 starts anyway.

A second attempt can be another full journal/lock read (inner reader budget
`JOURNAL_READER_RETRY_BUDGET_S` = 1.0s). That stacks retry windows on the
journals that already contend most, which is the stall the docstring at
`scripts/goalflight_controllers.py:145-149` names.

### Why this test is inert

The test sleeps **0.05s on attempt 1** against a **0.04s budget**
(`test_controller_fleet.py:1438-1443`). Attempt 1 itself overruns the budget.

That pairing already trips the *old* post-attempt-1 check:

```text
attempts += 1
read_once()          # sleeps 0.05s
now >= deadline?     # yes — old code returns here
# never reaches sleep, never starts attempt 2
```

So `len(calls) == 1` and `error.startswith("busy after 1 attempts")` hold on
both the buggy and the fixed loop. `elapsed < 0.2` (`:1453`) is wide enough
for either: one 0.05s sleep, or two 0.05s sleeps plus a 0.05s backoff
(`FLEET_JOURNAL_BUSY_BACKOFF_S` at `scripts/goalflight_controllers.py:111`).

### Failure scenario

Inputs: busy `read_once` that returns immediately (attempt 1 under budget);
`JOURNAL_READER_RETRY_BUDGET_S = 0.04`; backoff 0.050s.

- **Buggy loop (pre-`e60749f`):** attempt 2 starts after the sleep.
- **Fixed loop:** attempt 2 is not started (`:164-165`).
- **This test:** still green on the buggy loop, so a revert of `:164-165` is
  silent. The fleet table can stack another roster/lock read on a journal
  whose first peek already consumed almost the whole 1s window.

This is the expensive direction for this surface: a contended journal keeps
being retried past budget instead of rendering retry-exhausted unknown.

### How verified (run, not read)

1. Replaced `_retry_journal_busy` with the pre-`e60749f` loop (deadline check
   after attempt 1, no post-sleep check). Restored afterwards;
   `git status` clean.
2. **Named test on the buggy loop:**
   `pytest tests/python/test_controller_fleet.py::test_retry_deadline_checked_before_subsequent_attempt`
   → **1 passed in 0.68s**.
3. **Named test on the fixed loop:** same node → **1 passed in 1.56s**.
4. **Correctly-shaped probe** (fast `read_once`, same 0.04s budget, no
   attempt-1 sleep):
   - buggy loop: `calls=2`, `elapsed≈0.042s`, `busy after 2 attempts over 42ms`
     — attempt 2 started.
   - fixed loop: `calls=1`, attempt 2 did not start,
     `busy after 1 attempts`.

The behaviour exists. This test does not pin it.

A test that would pin it: attempt 1 returns immediately, budget shorter than
backoff so the sleep can cross the deadline, assert `len(calls) == 1`.

---

## Out of class

- **`59c5d2d`** (`goalflight_task.py` `FileLock` 30s `STORE_LOCK_BUDGET_S`):
  contended store now raises `TaskError` instead of hanging. No test file in
  the commit. Missing coverage, not an inert test. Pre-existing FileLock
  tests in the range were not extended to this bound.

---

## Searched and clean

**Observed** rows: the claimed production change was reverted and a probe
or the named test was run. **Read** rows: the test was read against the
source path it calls; reverting that path would fail the assertion as
written. The P1 above is the only case where a revert left the named test
green.

| Kind | Discrimination | Test / probe | Revert | Result |
|---|---|---|---|---|
| Observed | not-yet-RUNNING vs `cas_lost` (`de00f6c`) | `test_running_before_worker_claims_is_not_reported_as_a_lost_cas` (`test_goalflight_p2.py:336`) | disposition back to `cas_lost` | named test **FAILED** |
| Observed | quota horizon named vs UNKNOWN (`99d6576`) | `test_advisory_names_the_reset_horizon_or_says_it_is_unknown` (`test_quota_stuck.py:911`) | drop `reset_horizon_clause` from advisory text | named test **FAILED** (`UNKNOWN` missing) |
| Observed | foreign attention id vs ceremony-free (`2f396e0`) | `_terminal_marker_matches_dispatch` (`test_ci_mutation_guards.py:127`, `test_terminal_vocab.py` added pairs) | `_attention_payload_names_foreign_dispatch` always `False` | foreign payload **binds** |
| Observed | `last_drain_available` from measured timestamps (`adae0a5`) | `test_empty_index_is_honest` (`test_controller_fleet.py:666`) | hardcode `last_drain_available: True` | empty payload is `True` |
| Observed | terminal ledger vs running sidecar (`8d5f021`) | `test_finished_ledger_is_not_resurrected_by_stale_running_sidecar` (`test_wait_terminal_primitive.py:444`) | overlay sidecar `state` even when ledger is terminal | `state=running`, `done_code=2` |
| Observed | zombie descendants not live work (`9a8c1fa`) | `test_live_descendant_count_filters_zombie_rows_at_ps_seam` (`test_watch_idle_activity.py:292`) | `_is_zombie_or_defunct_state` always `False` | zombie-only count **1**, expected **0** |
| Observed | write-lock without sqlite is unknown (`a5c725b`) | `test_write_lock_without_sqlite_is_unknown` (`test_journal_gc.py:116`) | skip lock-present → unknown | classify `empty` / `reclaimable=True` |
| Read | fused unknown vs mismatch (`1ef148d` / `b97e868`) | `test_matching_capability_stays_unknown_not_mismatch_when_registry_unreadable` (`test_dispatch_registration_gate.py:1345`) | real exclusive lock; tripwire on collapsing `_kernel_live`; distinct reasons asserted | mapping unreadable → mismatch would fail the reason asserts |
| Read | producer-set structurally absent (`0ed4291`) | `test_dead_pids_without_group_contract_are_eligible` (`test_abandoned_dispatch_reconciliation.py:437`) | old path enumerated on any contract field (including bare `worker_pgid`) | test asserts `closed==1` and `_PRODUCER_SET_STRUCTURALLY_ABSENT` |
| Read | `worker_alive` must not short-circuit pid probes (`55bcb7f`) | `test_stale_worker_alive_true_with_dead_pid_is_eligible` (`:282`) | old early-return `worker_alive is True` | would keep the record running; test asserts `closed==1` |
| Read | live-overdue as own state (`2a63ec4`) | `test_holder_state_never_collapses_unknown_into_dead` (`:278`) + `test_live_overdue_holder_renders_live_overdue_with_renew_hint` (`:354`) | collapse `live-overdue` → `live`/`unknown`/`dead` | `== "live-overdue"` fails |
| Read | supervisor executable-position argv (`b7fe269`) | `test_foreign_python_c_carrying_supervise_argv_is_not_running` (`test_rearm_hint_supervisor.py:920`) | drives `_supervisor_generation_state_from_listing` | substring match would report RUNNING |
| Read | drain busy isolation (`d17faf5` / `cbfd260`) | `test_busy_project_is_skipped_and_other_project_still_drains` (`test_drain_busy_isolation.py:185`) | real exclusive lock on one project | other project still drains; structural vs busy is a second test |
| Read | pre-launch claim release (`cbfd260`) | `test_pre_launch_abort_releases_claim` (`test_drain_claim_release.py:100`) | — | asserts envelope restored to `queued` and no `.claimed-*` |
| Read | journal GC keep create-only of live root (`6143efd`) | `test_apply_does_not_delete_create_only_journal_of_live_project` (`test_journal_gc.py:330`) | empty-before-root-check | would reclaim a live project's create-only journal |
| Read | JOURNAL_DIR pin (`c1df747`) | `test_isolated_create_does_not_touch_live_journals` (`test_journal_dir_isolation.py:81`) | not reverted against live XDG | pops `TASK_STORE_DIR`, keeps `JOURNAL_DIR`; asserts no live slug |
| Read | ABC exemption earned by unknown shape (`b634bfa`) | `test_journal_unavailable_abc_exemption_is_earned_by_unknown_shape` (`test_goalflight_journal_reader.py:815`) | snippet table includes `pass` / empty-list as violations | analyzer, not a tautological constant |
| Read | dry-run text + record (`76942df`) | `test_reconcile_abandoned_dry_run_text_states_no_record_changed` (`test_abandoned_dispatch_reconciliation.py:978`) | — | asserts the phrase **and** `_read(...)["state"] == "running"` |

Hermeticity already closed in-range (`e144d5c` / `881adf0` / `1573238` /
`abb4b3e`): listing doubles accept `timeout_s`, autouse isolation no longer
collides with test-owned tmp paths, spawn-listen tests hold a real lease so
they do not measure ambient "unowned". Those were prior instances of this
class; they are not live now.

Also inspected, not inert:

- `test_permanently_busy_journal_reports_retry_exhausted` (`:1386`) pins
  attempt-cap exhaustion (`== FLEET_JOURNAL_BUSY_ATTEMPTS`), not
  deadline-before-attempt-2. Reverting only `:164-165` still yields 6
  instant attempts inside the 1s default budget.
- `test_contentless_row_predicate` (`:938`) and
  `test_journal_display_name_strips_store_hash` (`:930`) are helper unit
  tests; reverting the helpers fails them. Integration rows
  (`test_unreadable_journal_yields_exactly_one_named_row`,
  `test_never_emits_contentless_unknown_label_row`) exercise emission.
- `test_process_identity_precondition_dead_pid_is_absent`
  (`test_ledger_lifecycle_authority.py:161`) is a synthetic PID
  precondition (`DEAD_PID = 1_000_000_001`), not a host-ambient "unowned"
  measurement.
- `test_unscoped_resolver_still_points_at_live_xdg`
  (`test_journal_dir_isolation.py:67`) documents the production default
  with env cleared; it does not create a live journal. Isolation is pinned
  by `test_isolated_create_does_not_touch_live_journals`.
- No `try/except: pass` swallows in the new test files that would hide a
  failed discrimination.
- Timing tests added in the range that use a **fake clock**
  (`test_connect_stops_after_one_attempt_when_shared_deadline_is_spent`,
  `test_construction_shares_one_busy_deadline_across_lock_and_open_stages`
  in `test_goalflight_journal_reader.py`) are pre-existing in this file's
  ABC-exemption edit and are not the 0.05-vs-0.04 pairing.

Shapes hunted across added tests: doubles returning the asserted value;
assertions on a constant (`last_drain_available: True` was the pre-fix
production constant — the new tests fail if it returns); preconditions
built by calling the function under test; `assert x is not None` where None
was never reachable; timing margins both branches satisfy; `try/except:
pass`.

---

## Method note

All production edits were temporary and restored. Worktree `git status` was
clean after the last restore. Tests ran under isolated
`GOALFLIGHT_{JOURNAL,STATE,MESSAGES,TASK_STORE,PIDFILE,DISPATCH}_DIR` and
`GOALFLIGHT_CAPACITY_CONF=/dev/null`. No live queue, ledger, or journal was
written (journal-isolation tests that would mint a live slug were not
reverted against the host XDG).
