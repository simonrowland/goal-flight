# rev-queue-red — four long-red dispatch-queue tests

Range: `de00f6c..4518301` (`4ad55ff` pin, `4518301` ACP re-exec). Review only.
Claims below are **OBSERVED** unless marked **HYPOTHESISED**.

Interpreter used for all runs: `/opt/homebrew/bin/python3` (3.14.5), matching
live drain on this machine. Isolated env: fresh `mktemp -d` for journal,
state, wake-ledger, messages, task-store, both pidfile names;
`GOALFLIGHT_CAPACITY_CONF=/dev/null`; `GOALFLIGHT_DISPATCH_DIR` unset at the
wrapper (pytest autouse still pins per test). Live
`/tmp/goal-flight-501/dispatch` was busy (~12k files, active workers). No
real dispatch launched; live queue/ledger not mutated.

---

## Verdict

**CLEAN.** The two commits fix two independent bugs. Reverts fail as claimed.
Nothing was weakened to get green (93 `test_*` functions held; no skip / xfail
/ timeout hunks vs `de00f6c`). The launch-path change is a real product fix,
not a test-only patch. No P0/P1.

| | P0 | P1 | P2 | P3 |
|---|---|---|---|---|
| count | 0 | 0 | 2 | 1 |

---

## What was claimed, and what ran

Four tests in `tests/python/test_dispatch_queue.py`:

| test | claimed cause | HEAD | revert `de00f6c` | revert pin-only `4ad55ff` |
|---|---|---|---|---|
| `test_submit_default_drain_launches_once_and_duplicate_submit_does_not_double_launch` | unpinned dispatch dir | pass | `last_watcher={}` | pass |
| `test_drain_launches_queued_request_once_and_exits` | unpinned dispatch dir | pass | `last_watcher={}` | pass |
| `test_drain_waits_for_submit_status_recording` | unpinned dispatch dir | pass | `last_watcher={}` | pass |
| `test_acp_submit_then_drain_replays_from_queue` | startup fail-close | pass | submit rc=1, SDK traceback | submit rc=1, SDK traceback |

HEAD (live dir busy): `4 passed, 89 deselected in 16.14s`. Live dir had **zero**
files whose names contained `drain-launch`, `submit-default-launch`,
`submit-drain-race`, or `acp-drain` before or after.

`de00f6c` worktree: `4 failed`. Watcher assertion text (all three):
`watcher did not finish its terminal write; last_watcher={}`. ACP:
`assert submit.returncode == 0` with stderr
`cannot import acp (ModuleNotFoundError: No module named 'acp'); repair the SDK installation in that interpreter`.

`4ad55ff` worktree: `1 failed, 3 passed`. Same ACP assertion; watchers green.

---

## 1. Launch path (`4518301`) — ACP and non-ACP

**Anchor:** `scripts/goalflight_dispatch.py:14646` (`_ensure_acp_sdk_interpreter`),
called from `__main__` at `:14696` before `main()`.

Shape gate (`:14672-14681`): `--shape acp`, or `auto` with `--agent` in
`{claude-acp, claude}` or `--interactive`. **OBSERVED:** `drain` and bash-shape
`--agent codex --submit` never call `os.execv` even when a different
interpreter would otherwise be `reexec`.

Then:

```14682:14692:scripts/goalflight_dispatch.py
    try:
        import goalflight_acp_run  # noqa: PLC0415
        from goalflight_acp_client import ACP_SDK_REEXEC  # noqa: PLC0415
    except BaseException:
        return
    if goalflight_acp_run._acp_reexec_target().state != ACP_SDK_REEXEC:
        return
    goalflight_acp_run._ensure_acp_sdk_python()
```

`_acp_reexec_target()` is `acp_sdk_resolution()`
(`scripts/goalflight_acp_run.py:224-226`,
`scripts/goalflight_acp_client.py:137-227`). Three states only:
`importable` / `reexec` / `unavailable`. There is no `unknown`.

### Cases (OBSERVED on 3.14)

| situation | `acp_sdk_resolution().state` | startup helper | `_ensure_acp_sdk_python()` |
|---|---|---|---|
| SDK missing, managed venv missing under `Path.home()` | `unavailable` | returns, no raise | raises `AcpError` ("cannot be satisfied before launch") |
| `GOALFLIGHT_ACP_PYTHON == sys.executable` (the queue test) | `unavailable` (already current) | returns | raises |
| override path missing | `unavailable` | returns | raises |
| target is a directory | `unavailable` | (not re-exec) | raises |
| target not executable | `unavailable` | (not re-exec) | raises |
| inspect `OSError` (`exists`/`is_file`) | `unavailable`, reason `could not inspect configured ACP interpreter … PermissionError` | returns (`!= reexec`) | would raise if called |
| different existing executable | `reexec` | `os.execv` invoked (patched) | would execv |
| import of `goalflight_acp_run` raises | n/a | **swallowed**, returns | never called |
| bash / `drain` argv | n/a | returns before probe | never called |

This worker's `HOME` is the grok account sandbox, so `Path.home() / .goal-flight/venvs/acp-0.10/bin/python` does not exist even though
`/Users/simonrowland/.goal-flight/venvs/acp-0.10/bin/python` does and can
`import acp`. That is why the current process resolves `unavailable`. Live
drain uses 3.14 with a normal `HOME` and would be `reexec` into that venv.

### What the check returns when it cannot tell

**OBSERVED:** a probe that cannot inspect the target returns
`ACP_SDK_UNAVAILABLE` with the exception in `reason`
(`goalflight_acp_client.py:156-168`). It does **not** return `importable`,
and it does **not** return a fourth "unknown" state.

Dispatch startup then treats every non-`reexec` state as "do not re-exec,
enter `main()`" (`goalflight_dispatch.py:14690-14691`). That is intentional:
`_ensure_acp_sdk_python()` fail-closes on `unavailable`
(`goalflight_acp_run.py:235-236`), which is exactly what aborted `--submit`
before `4518301`.

Spawn-time `require_acp_sdk()` (`goalflight_acp_client.py:230-238`, called
from `spawn_acp_connection` at `:3359`) still fail-closes on both
`unavailable` and `reexec` (the latter as "requires a different interpreter",
not as a second execv). The test ACP seam
(`goalflight_dispatch.py:12687-13052`) runs *after* `main()` parses args and
*before* `run_acp_dispatch`, so a queued ACP job with
`GOALFLIGHT_TEST_ACP_DISPATCH_COMPLETE_FILE` never hits `require_acp_sdk`.
Permanent os-sandbox refusal (`:2543-2545` → `_validate_agent_os_sandbox`)
also lives in `main()`, so it is reachable again.

A `BaseException` during the *import* of `goalflight_acp_run` is different:
the helper returns void with no state object. That is P2 below.

---

## 2. Product bug vs test-only green

**OBSERVED:** `4518301` fixes the product bug, not just the test.

Pre-`4518301`, every acp-shaped `goalflight_dispatch.py` invocation called
`_ensure_acp_sdk_python()` at process start. After `36bdc71` that function
raises on `unavailable`. `--submit --shape acp` never reached `main()`, so
it could not queue, could not honour the test ACP seam, and could not emit
the permanent os-sandbox refusal (commit message matches the code).

The failing test is a legitimate instance: it sets
`GOALFLIGHT_ACP_PYTHON = sys.executable`
(`test_dispatch_queue.py:1924`). This 3.14 cannot `import acp` →
`unavailable` / already-current → old startup raise → `submit.returncode == 1`.
Reproduced on both `de00f6c` and `4ad55ff`.

**Could an ACP dispatch still re-exec wrongly after this change?**

I could not construct a remaining *fail-close-at-submit* path for
`unavailable`. Ordering that still works:

1. `--submit --shape acp` with `unavailable` now enters `main()` and queues.
2. `drain` is not acp-shaped, so the drain process does not re-exec.
3. The child is acp-shaped. If the child is `reexec`, it `execv`s *before*
   `main()` (no lease yet in the child). If `unavailable`, it skips, then
   either the test seam runs or `require_acp_sdk` fail-closes at spawn.

**HYPOTHESISED residual (P3, not blocking):** `--submit` still re-execs when
state is `reexec` (venv present and different). An `os.execv` failure still
raises from `_ensure_acp_sdk_python` (`:240-247`) and aborts queueing. A
healthy venv just restarts into SDK python and then submits — extra, not
wrong. A present-but-broken *different* interpreter is the remaining
startup abort, and the queue test does not cover it.

I could not construct a TOCTOU where startup classifies `unavailable` and
then later *silently* talks ACP without `require_acp_sdk`. If the venv
appears between startup and spawn, `require_acp_sdk` sees `reexec` and
raises rather than execv'ing — fail-closed, slightly blunt.

---

## 3. Load / order sensitivity

**Verdict: the author's "not load- or order-sensitive" claim is true.** The
brief's picture of "unpinned live shared dispatch dir" is the wrong mechanism.

**OBSERVED mechanism (pytest):** autouse isolation
(`tests/python/conftest.py:48-55` → `machine_isolation.py:110-117` →
`support.py:59`) sets `GOALFLIGHT_DISPATCH_DIR` to *pytest's* `tmp_path/state/dispatch`.
These tests own a separate `tempfile.TemporaryDirectory` and, before
`4ad55ff`, `_env` copied `os.environ` and overwrote `GOALFLIGHT_STATE_DIR`
but not `GOALFLIGHT_DISPATCH_DIR` (`test_dispatch_queue.py:38-54` after the
pin; parent still only set `STATE_DIR`).

Watcher writes follow `dispatch_base_dir()`
(`scripts/goalflight_dispatch_paths.py:18-29`) → pytest tree.
`_wait_for_dispatch_shutdown` on `de00f6c` waited on
`Path(env["GOALFLIGHT_STATE_DIR"]) / "dispatch" / f"{id}.watcher.log"` —
the TemporaryDirectory tree, empty → `last_watcher={}`.

That is two temp trees, deterministic under pytest, independent of whether
`/tmp/goal-flight-501/dispatch` is quiet or full. Reproduced on revert
inside an isolated env with the live dir busy: same `last_watcher={}`, not
a live payload.

**HYPOTHESISED:** as a script (`python test_dispatch_queue.py` via `main()`,
no autouse) with `GOALFLIGHT_DISPATCH_DIR` unset, default
`<state>/dispatch` would coincide with the test tree and the watcher tests
would pass. Failures "all day on clean main" are the pytest gate, not live
pollution. If that script inherited a controller's live
`GOALFLIGHT_DISPATCH_DIR`, it *would* write live — but that is not how the
gate invokes this file.

---

## 4. Did the pin isolate, or relocate?

**OBSERVED:** isolate, for these four tests.

`4ad55ff` sets `GOALFLIGHT_DISPATCH_DIR` to the test-owned
`tmp/state/dispatch` (`test_dispatch_queue.py:45`) and reads the watcher log
from that override (`:134-137`). Also pins `GOALFLIGHT_PIDFILE_DIR` (the
alias `machine_isolation.py` already documents as historically forgotten).

With ~12k live dispatch files present, the four tests passed and no live
artifact named after those dispatch ids appeared. They did not need, and
did not write, the shared tree.

---

## 5. Reverts

See table above. Both fixes fail independently. Watcher pin does not save
the ACP test; ACP launch-path fix does not save unpinned watchers (not
directly re-run as "ACP-only on unpinned tests", but `de00f6c` has neither
and all four fail; `4ad55ff` has only the pin and only ACP fails).

---

## 6. Weakening

**OBSERVED:** `git diff de00f6c..HEAD -- tests/` is only
`tests/python/test_dispatch_queue.py` (+11 / −2). 93 `test_*` functions at
parent, pin, and HEAD. No `skip` / `xfail` / timeout / `_ASYNC_WAIT` hunks.
Test count held.

---

## P2 — launch-path import still swallows `BaseException` and proceeds

**Anchor:** `scripts/goalflight_dispatch.py:14682-14686`. This is the block
`4518301` edited.

**Failure:** if `import goalflight_acp_run` (or `ACP_SDK_REEXEC`) raises
anything — `OSError`, `TypeError`, `KeyboardInterrupt` — the helper
`return`s. **OBSERVED:** patched `__import__` raising `OSError("simulated
import probe failure")` → helper returned, no re-exec, no raise.

That is the SC-153 shape: a lookup that cannot tell, licensing "proceed
into `main()`" with no resolution object. Distinct from
`acp_sdk_resolution`, which names inspect failure as `unavailable`.

This swallow is pre-existing (`850ab49`); `4518301` kept it. It is not a
silent "SDK is ready": `_run_acp_shape` (`:13007`) imports
`goalflight_acp_run` *without* this swallow, so a real ACP session still
dies on a broken import. `--submit` proceeding is the desired outcome.
Still: this is the one place on the new launch path where "cannot tell"
is void, not `unavailable`.

**Suggestion:** catch `Exception` (not `BaseException`), and if the import
fails after the shape gate, either leave a named skip reason on stderr or
share the same `unavailable` object `require_acp_sdk` already understands.

---

## P2 — same two-tree class remains in other dispatch test `_env` helpers

**Anchor (instances, not live writes):**

- `tests/python/test_dispatch_capacity_ledger.py:32-45`
- `tests/python/test_dispatch_capacity_requeue.py:30-47`
- `tests/python/test_acp_dispatch_sigterm.py:78-88`
- `tests/python/test_dispatch_steer.py:54-63`
- `tests/python/test_dispatch_task_links.py:29-38`

Each copies `os.environ`, overrides `GOALFLIGHT_STATE_DIR` to its own
`TemporaryDirectory`, and does **not** re-pin `GOALFLIGHT_DISPATCH_DIR`.
Under pytest they inherit autouse's pytest `tmp_path` tree, not
`/tmp/goal-flight-501/dispatch`. None of them wait on `*.watcher.log`, so
they are not the same red tests.

This is the class `4ad55ff` fixed one instance of, not leftover live-dir
leakage. Autouse still keeps them off the live shared dir.

Tests that *do* resolve the live default are opt-in:
`tests/python/test_dispatch_dir_isolation.py` (`pytestmark = live_machine_state`,
`:20`, `:159-170` asserts the unscoped resolver still points at
`/tmp/goal-flight-<uid>/dispatch`). That is the subject's point, not a miss.

`tests/python/test_goalflight_messages.py:61` pops `GOALFLIGHT_DISPATCH_DIR`
so mailbox writes land under the fixture `state_dir/dispatch`, which is
isolated.

**OBSERVED:** no other suite file was seen writing the four test dispatch
ids into the live shared dir during this review. **HYPOTHESISED:** a
script-style `main()` of one of the unpinned helpers, run with a
controller's live `GOALFLIGHT_DISPATCH_DIR` in the environment and without
pytest autouse, could still write live status. The gate does not do that
(`tests/run.sh` does not set `GOALFLIGHT_DISPATCH_DIR` globally; autouse
does per test).

**Suggestion:** the `_env` helpers that launch `goalflight_dispatch.py`
should pin `GOALFLIGHT_DISPATCH_DIR` to the test-owned tree the way
`test_dispatch_queue.py:45` now does — class fix, not another one-off.

---

## P3 — `--submit` still re-execs on `reexec`; no direct tests of the wrapper

**Anchor:** `goalflight_dispatch.py:14690-14692`. Grep: `_ensure_acp_sdk_interpreter`
is referenced only in `goalflight_dispatch.py` (definition + `__main__`).
Coverage is the one integration test plus `tests/python/test_acp_reexec.py`
against `_ensure_acp_sdk_python` / `require_acp_sdk`, not the dispatch
wrapper.

If state is `reexec`, `--submit` still `execv`s before parse. Execv
`OSError` still aborts queueing (`goalflight_acp_run.py:240-247`).
**HYPOTHESISED** only; not hit by the queue test (same-exe → `unavailable`).

---

## Attack checklist

1. Launch path vs missing / same / broken / failed probe — **OBSERVED** table
   above. Cannot-tell → `ACP_SDK_UNAVAILABLE` (named). Import failure → void
   proceed (P2).
2. Product vs test — **product fix**. No remaining wrong re-exec of the
   fail-close-at-submit kind constructed.
3. Load-sensitivity — **not load-sensitive**. Two pytest trees, not live dir.
4. Pin isolated — **yes**. Live dir untouched for those ids while busy.
5. Reverts — **both fail** as claimed (`last_watcher={}`; ACP submit rc=1).
6. Weakened? — **no**. 93/93 held.
