# Reds-to-zero batch 5 review

Base: `b9c142b`  
Tip: `f130a74`

## Verdict

REQUEST-CHANGES.

## Findings

### P1 — supervisor migration can terminate the caller

`scripts/goalflight_messages.py:9572-9592` records any live waiter whose
start token matches, and `scripts/goalflight_messages.py:9636-9638` then sends
that PID `SIGTERM`. The only self-protection is `pid == os.getpid()` at line
9590, but the supervisor is a child process. A caller can therefore hold a
watchdog/listener waiter lock, launch `goalflight_messages.py supervise`, and
become a verified incumbent target from the supervisor's perspective. The
supervisor then signals the caller process. This is the failure scenario in
`test_doctor_wake_coverage_reports_supervisor_state`, which registers the
caller-owned waiter at `tests/python/test_rearm_hint_supervisor.py:1966`.

There is no `killpg` in this path; the direct PID signal is sufficient to kill
the caller, and it also means the new test is not a product fix. The commit
only adds an explicit `ps`-unavailable skip at
`tests/python/test_rearm_hint_supervisor.py:1954`. The sandboxed run therefore
skipped this node, while a ps-capable run can still hit the unsafe signal.
The safe probe reproduced the matching caller PID/start-token target without
emitting a signal.

### P2 — the batch's controller-startup error typing is still red

`tests/python/test_write_failure_visibility.py:375-400` still fails for a
present journal with `journal_meta` removed: the controller reports
`JournalIOError`, but the test requires `JournalIntegrityError`. The directory
driver reproduced this on both initial and confirmation runs. A caller that
starts against a structurally damaged journal therefore receives an
availability classification instead of an integrity diagnosis. The batch tip
does not change the controller-startup path in
`scripts/goalflight_session_status.py:1214-1337` (nor the read-side
classification that produces the wrong type), so this required batch-5 case
has not been fixed with a designed-red regression closure.

### P2 — journal-open handling was narrowed too far

`f130a74` changes `scripts/goalflight_wake_supervise.py:2908-2916` from a
catch of `JournalError` to a catch of only `JournalIntegrityError`. If the
present journal becomes busy, disappears, is unreadable, or requires an
upgrade after lease resolution, `Journal.open_reader` raises one of the other
concrete journal failures and the supervisor falls through to the generic
`goalflight_messages` process-boundary error. The previous operator-facing
`supervise: journal holder unavailable` result and its controlled startup exit
are lost. The fix should name and handle the concrete failure classes required
at this boundary, preserving fail-closed behavior without restoring the broad
catch.

## Verification

- The focused `tests/python` directory driver ran with the isolated variables
  from `tests/run.sh`, selecting `test_follow_listener`,
  `test_supervised_wake`, `test_listener_terse_startup`,
  `test_rearm_hint_supervisor`, `test_goalflight_journal_reader`, and
  `test_write_failure_visibility`: 3 module passes, 2 module failures, and 1
  explicit skip. The follow module itself was independently rerun: 90 passed.
- All five requested watchdog cases passed through the directory driver:
  `test_follow_backup_and_watchdog_coexist_and_sigkill_wakes`,
  `test_backup_wakes_when_watchdog_is_sigkilled_with_stream_alive`,
  `test_watchdog_reads_durable_age_and_wakes_with_exact_rearm`,
  `test_watchdog_grace_does_not_hide_a_new_follow_fault`, and
  `test_watchdog_wakes_when_durable_follow_state_never_appears`.
- The skipped node was
  `tests/python/test_rearm_hint_supervisor.py::test_doctor_wake_coverage_reports_supervisor_state`,
  with reason `real process-table probe unavailable: [Errno 1] Operation not permitted: 'ps'`.
- `test_goalflight_journal_reader` had 33 passed and 1 failed, with only the
  three Batch-4 dispatch sites and the Batch-4 journal site remaining; the
  messages and wake-supervise sites changed by this tip were no longer among
  the residuals. `test_write_failure_visibility` had 65 passed and 3 failed;
  its other two failures are Batch-4 cases.
- `git diff --check b9c142b f130a74` passed. The tip changes only the two
  Batch-5 production files and the two Batch-5 test files.
- The complete `./tests/run.sh` was started with its own isolated environment;
  it passed eight bash entries and then stalled in the unrelated setup phase,
  so it was stopped. No repository test or product file was changed by that
  run.
