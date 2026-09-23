# Reds-to-zero batch 5 review

Base: `b9c142b`
Tip: `b915d02`

## Verdict

REQUEST-CHANGES.

## Findings

### P1 — supervisor migration can signal a caller ancestor

`scripts/goalflight_messages.py:9506-9521` only compares the candidate PID's
process group with the immediate caller's process group. It never proves that
the candidate is not in the caller's ancestry. Both migration paths then send
`SIGTERM` at `scripts/goalflight_messages.py:9670` after that incomplete check
(`:9610-9618` records the preflight target).

Failure scenario: a controller starts `goalflight_messages.py supervise` from a
process group where an ancestor supervisor/controller has a different process
group, and the stale waiter record contains that ancestor's matching PID and
start token. The new supervisor treats the ancestor as a distinct target and
signals it during migration. The same failure can occur after the revalidation
window if the ancestor still has the recorded identity. A direct probe of the
tip with caller PID 12345 in PGID 77 and target PID 54321 in PGID 88 returns
`False` from `_pid_in_caller_process_group`, so this is not hypothetical logic.

The added regression test only makes every `getpgid` call return 77, covering a
caller process-group match but not an ancestor in another group. The migration
guard must walk/prove the caller ancestry and refuse any ancestor; only a
matching-identity target proven both non-ancestor and outside the caller group
may be signalled.

## Module dispositions

- `test_follow_listener.py`: PRODUCT path reviewed. The concrete journal
  exception handling preserves the watchdog/follow wake reasons and rearm
  diagnostics. The five inventory watchdog cases passed.
- `test_supervised_wake.py`: PRODUCT fix for the disabled-dashboard
  `unavailable` hint preserves quiet idle generations while forwarding real
  projection failures. The five concrete journal-open failure cases passed.
- `test_listener_terse_startup.py`: TEST fixture fix is appropriate; it supplies
  known zero-waiter evidence before asserting `live == 0` and does not weaken
  the assertion.
- `test_rearm_hint_supervisor.py`: PRODUCT fix is incomplete for the P1 above.
  The explicit `ps`-unavailable skip is honest, but the real process-table
  safety test must also run on a ps-capable controller.
- `test_goalflight_journal_reader.py`: the messages and wake-supervise sites
  changed by this tip name concrete journal availability subclasses. The
  remaining four lint failures are Batch 4 sites, not changed by this batch.
- `test_write_failure_visibility.py`: the controller-startup integrity typing
  case is green. Its dashboard pidfile and stale ACP source-guard failures are
  Batch 4 residuals.

## Scope note

`scripts/goalflight_journal.py:1413-1419` is outside Batch 5 ownership, but the
four-line change is minimal and necessary for Batch 5's controller-startup
case: dropping `journal_meta` now becomes `JournalIntegrityError` instead of
`JournalIOError`. This cross-batch change should remain explicitly attributed
to that acceptance case and coordinated with Batch 4.

## Verification

The isolated tests were run with the environment contract from `tests/run.sh`
and the tests/python directory driver:

- Batch 5 plus neighbours: 582 collected; 577 passed, 3 failed, 2 skipped in
  216.81s. The failures are the known Batch 4/cross-batch residuals:
  `test_journal_unavailable_handlers_name_concrete_subclasses`, the dashboard
  pidfile contract case, and the stale ACP detach source guard.
- Focused safety, journal-open, and controller-startup cases: 8 passed, 1
  skipped in 0.66s.
- `test_rearm_hint_supervisor.py`: 47 passed, 2 skipped in 20.39s.
- Sandbox-denied node: `tests/python/test_rearm_hint_supervisor.py::test_real_process_table_with_spaced_root_never_proves_absence`
  skipped with `real process-table probe unavailable: [Errno 1] Operation not permitted: 'ps'`.
- Explicit safety-test skip: `tests/python/test_rearm_hint_supervisor.py::test_doctor_wake_coverage_reports_supervisor_state`
  skipped with `real process-table probe unavailable`.
- `test_controller_startup_journal_error_returns_structured_result_without_traceback`
  passed for both `upgrade` and `integrity` parameters.
- `git diff --check b9c142b b915d02` passed before this review artifact was
  rewritten.
