# Reds-to-zero batch 2 review

Compared `c044198` against `b9c142b`.

## Verdict

REQUEST-CHANGES

## Findings

### P1 — unknown recovery-lock ownership is still reclaimable

`scripts/goalflight_fleet_launch_detached.py:395-404` allows an unknown owner
(`live is None`) to reclaim a recovery lock solely because its timestamp is
old. The batch-owned test `tests/python/test_fleet_launch_detached.py:774-811`
explicitly preserves this behavior with a missing PID and expects a relaunch.

Failure scenario: a live launcher is unprobeable because `ps` or the identity
probe is unavailable; after the TTL, a second recovery attempt treats the
unknown owner as stale, unlinks its lock, and starts a duplicate worker. This
violates the acceptance invariant that unknown liveness never authorizes a
reclaim. The test must be changed and the product path must retain the lock
until owner death or PID reuse is positively established.

No P2 or P3 findings.

## Patch assessment

- `test_fleet_dispatch.py`: TEST fix; the persisted and mocked identities now
  provide the required matching `start_token`, preserving the live-status
  recovery intent.
- `test_fleet_launch_detached.py`: TEST fix; `lstart` now matches while
  `start_token` differs, so the test isolates the intended PID-reuse reason.
- `test_watch_prompt_echo.py`: TEST fix; the mismatch fixture now isolates
  `start_token` precedence, and the watcher liveness scenarios provide explicit
  worker identity/cwd evidence without weakening assertions.

The diff changes only the three assigned test files. No product fix is present
in the patch. The direct-dispatch process-group guard and watcher group-scope
checks remain conservative, and the patch does not add any signal path.

## Verification

- Isolated directory-driver batch selection: 3/3 module children passed,
  277 unrelated driver cases deselected, 107.82s.
- Isolated directory-driver identity neighbours (`test_pid_probe`,
  `test_worker_identity_exec`, `test_fleet_watch`, `test_acp_shim_reaper`):
  4/4 module children passed, 276 unrelated driver cases deselected, 5.75s.
- No test nodeids were blocked by sandbox-denied `ps` or process-group
  operations.
