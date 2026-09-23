# Reds-to-zero batch 3 review

REQUEST-CHANGES

## P1 findings

- `tests/python/test_dispatch_capacity_ledger.py:1314-1338` — the idle-timeout fixture still launches with `--cwd tmp/worker-cwd`, an unmanaged child of a synthetic git root. The dispatch worktree contract rejects that path before spawning (`WorktreeCwdRefused`, exit 1), so the assertion expecting exit 2 and `idle_timeout` never exercises the liveness path. Under the required isolated directory driver the module fails twice at this case. Make the fixture use a valid in-place/managed-seat launch while preserving the distinct tree-probe intent, or provide an explicit environment skip only if that intent cannot run in the driver.

## Scope and safety review

- The `19ce119` diff is limited to 12 `tests/python` files; `git diff --check` is clean and no production files or out-of-batch paths changed.
- The changed harness assertions preserve the intended contracts: the isolated-test routing variable is deliberately retained, the flake fixture copies its imported helpers, ACP prompt tests assert the preamble and body, dispatch-dir tests still assert independent path resolution, and the capacity/procedural fixtures retain their behavioral assertions.
- No signalling, process-identity, process-group, or product implementation code changed in this batch; no new product test was falsely greened by a weakened assertion.

## Verification

- Changed self-tests: 2 passed.
- `test_skill_structure.py`: 7 passed.
- `test_acp_dispatch_sigterm.py`: 1 passed through the ACP-aware driver.
- `test_acp_model_passthrough.py`: 3 passed.
- `test_codex_dispatch_seams.py`, `test_capacity_orphan_lease.py`, `test_dispatch_dir_isolation.py`, `test_goalflight_liveness.py`, `test_context_discipline_hooks.py`, `test_grok_seat_permission_mode.py`, `test_goalflight_procedural.py`, `test_python_test_requirements.py`, `test_dispatch_ergonomics.py`, `test_goalflight_status.py`, and `test_capacity_terminal_cleanup.py`: each driver node passed; procedural completed in 147.06s.
- `test_acp_pipe.py`: both driver attempts were blocked by the sandbox denying the repository worktree allocation lock (`WorktreeSeatError`); the failing case was `case_runner_overlimit_response_status_counts_drop`, not an assertion from this diff.
- `test_dispatch_capacity_ledger.py`: both driver attempts failed at the P1 case above.
- Broad `./tests/run.sh` was stopped after its bash phase because `tests/bash/test-watch-dispatch-tail.sh` had 88 passed and 8 process-inspection failures; the sandbox rejected `ps`. The affected cases were `case-3`, `case-3c`, and `case-3d`; its `case-6` live sampler explicitly skipped for the same reason. The Python liveness node to hand to the controller is `test_goalflight_liveness.py::test_cpu_keep_waiting_real_busy_subprocess_keeps_waiting`.
