# rev-wt-pool — pooled dispatch, worktree gc, trace archive

Review only. HEAD `7186a1e` on `sweep-inert-test` (`6c54400`, `fb2dd4d`,
`1c99173`, `3748606`, `ba22d14`, `7186a1e`). No source edits. Claims below
are **OBSERVED** unless marked **HYPOTHESISED**.

**Counts: P0=0, P1=0, P2=4, P3=2. Verdict: FIX.**

The dual-writer close on the bash `--worktree` path holds: the seat lock fd
reaches a spawned worker, SIGKILL of that worker (not cleanup code) frees the
seat, exhaustion refuses without `git worktree add`, and `--cwd` without
`--worktree` still launches. The gc identity-live guard retains a live
`idle_timeout` / `worker_dead` / `blocked` worker and still reclaims a
genuinely dead one. The holes are around the gc *exemption mechanism*, a
caffeinate regression on the new env var, archive credentials, and an untested
going-forward hook.

## Method

Isolated env: fresh `mktemp -d` for `GOALFLIGHT_JOURNAL_DIR`,
`GOALFLIGHT_STATE_DIR`, `GOALFLIGHT_WAKE_LEDGER`, `GOALFLIGHT_MESSAGES_DIR`,
`GOALFLIGHT_TASK_STORE`, `GOALFLIGHT_PIDFILE_DIR`, `GOAL_FLIGHT_PIDFILE_DIR`,
`GOALFLIGHT_DISPATCH_DIR`; `GOALFLIGHT_CAPACITY_CONF=/dev/null`;
`GOALFLIGHT_WORKTREE_SEATS=1` or `2`. Throwaway `git init` fixtures only.
Children SIGKILL'd. Existing tests re-run under Homebrew Python 3.14:
`test_worktree_seat_pool.py` (6 PASS as script) plus pytest
`test_dispatch_worktree_pool.py` + `test_worktree_gc.py` +
`test_trace_archive.py` → 21 passed in 21s. Production reverts applied, tests
re-run, `git checkout --` restored; scripts dirty-state empty afterwards.

---

## Attack answers (short)

| Attack | Result |
|---|---|
| 1. Seat lease reaches worker; SIGKILL releases | **Yes** for the dispatch daemon helper → worker hop. Kernel flock, not cleanup. |
| 2. Exhaustion refuses, no silent `git worktree add` | **Yes.** rc=2, occupant named, no `wt-2`. |
| 3. `--cwd` unbroken | **Yes.** Omit `--worktree` and a full pool does not block launch. |
| 4. gc live-identity guard | **Yes** on pid+start_token via `process_identity_matches`. Dead identity is reclaimable. |
| 5. Pool-seat exemption by registration, not name | **No.** Basename `wt-N` only. An ad-hoc tree named `wt-3` with no lock is retained as a "managed pool seat". |
| 6. Trace archive drops, no git-add, no credentials | Drops are named. Never `git add`s. `docs-private/` is gitignored. Credentials **can** ride along (archive does not redact). |
| 7. Reverts | Identity guard, pool exemption, default 24, worker `pass_fds`, and the Unavailable handler are load-bearing. The ledger archive hook is **not** pinned. |

---

## What holds

### 1. The lock fd reaches the worker; SIGKILL frees the seat

**Anchors:** `scripts/goalflight_worktree_pool.py:60-68` (release closes the
descriptor, does not `LOCK_UN`), `:94-136` (`inherited_worktree_lock_fds` /
`pass_worktree_lock_fds`), `:333-335` (`O_CLOEXEC` on open);
`scripts/goalflight_dispatch.py:1041-1048` (daemon helper `Popen` `pass_fds`),
`:1083-1093` (helper itself inherits via `pass_fds=pass_worktree_lock_fds(env)`),
`:14424-14427` (child env `GOALFLIGHT_WORKTREE_LOCK_FD`), `:14498-14502`
(launcher drops its copy after spawn).

**OBSERVED** (ISO `/tmp/rev-wt-pool-ZxMdun`, `GOALFLIGHT_WORKTREE_SEATS=1`):

- Pool child holding `LOCK_EX`: second acquire raises
  `all 1 worktree seats are held: wt-1=killed-worker pid=67036`. SIGKILL of
  that child; same path re-acquired as `after-sigkill`.
- Parent acquire → `pass_fds=(lock_fd,)` child → parent `release()` → seat
  still held (`cloexec=False fd=8` in the child) → SIGKILL of child frees
  `wt-1`.
- Real `goalflight_dispatch.py --worktree HEAD --launch-detached -- python3 -c
  …`: worker wrote `{"pid": 67937, "fd": 5, "cloexec": False, …}`; live
  acquire raised `wt-1=inherit-seat`; SIGKILL of 67937; `wt-1` re-acquired.

`close_fds=True` plus `pass_fds` is load-bearing. Revert probe R4 replaced the
helper's `pass_fds=goalflight_worktree_pool.pass_worktree_lock_fds()` with
`pass_fds=()`: `test_seat_survives_for_worker_lifetime_then_frees_on_death`
failed because the worker's `os.fstat(fd)` never ran (marker missing).

ACP `--worktree create` is a different, safer shape: `goalflight_acp_run.py:4094-4101`
passes `worktree_seat.fileno()` **and** the runner keeps the `WorktreeSeatLease`
until `finally` at `:4795-4796`. Even if the agent drops extra fds, the runner
holds the seat for the dispatch lifetime. Bash `--worktree` releases the
launcher copy at spawn, so the worker *is* the last holder.

**HYPOTHESISED, not scored:** a bash-shape CLI that closes inherited fds ≥3
would free the seat while still writing. Tests and the live probe use
`python3 -c`, which does not. Grok/codex were not fd-traced.

### 2. Exhaustion refuses honestly

**Anchors:** `scripts/goalflight_worktree_pool.py:416-419`
(`WorktreeSeatUnavailable`); `scripts/goalflight_dispatch.py:1505-1506`
("Never falls back to add"), `:14764-14773` (rc=2 + "refusing to git worktree
add"). No `git worktree add` in `goalflight_dispatch.py` except those two
strings.

**OBSERVED:** with `wt-1` held as `held-occupant`,
`--worktree HEAD` returned rc=2, stderr contained `all 1 worktree seats are
held: wt-1=held-occupant` and `refusing to git worktree add`, and
`worktrees/wt-2` was not created.

Revert R6 deleted the `WorktreeSeatUnavailable` handler. Acquire still raises;
the parent `WorktreeSeatError` handler returns rc=1 and still does not add.
The test failed on `assert 1 == 2` and the missing refuse sentence. No-fallback
is structural; the dedicated handler is the honest exit code and wording.

### 3. `--cwd` without `--worktree` is unchanged

**Anchors:** `scripts/goalflight_dispatch.py:1513-1515` (bind returns `None`
when the flag is omitted), `:13468-13469` / `:13578-13579` / `:13484-13485`
(preset cwd flags).

**OBSERVED:** `--cwd <repo>` with no `--worktree` launched rc=0 while every
seat was held (`cwd-intact-when-seats-exhausted`).
`test_cwd_without_worktree_does_not_acquire_a_seat` passed on the 21-test run.

`--worktree` then overwrites `args.cwd` to the seat (`:1526`) and rebuilds
worker argv (`:14386-14388`). Grok gets `--cwd <seat>`; Codex gets `-C <seat>`.
See P3 for Claude / raw `--`.

### 4. gc identity-live guard

**Anchors:** `scripts/goalflight_worktree_gc.py:89-98` (`LIVENESS_VERDICTS`),
`:341-367` (`_identity_live`: pid + `start_token`, never pid alone),
`:369-391` (`_record_owns_path`);
`scripts/goalflight_compat.py:1254-1296` (`process_identity_matches`);
`scripts/goalflight_dispatch_states.py:61-63,74,192-196` (`idle_timeout`,
`worker_dead`, `blocked` are terminal — that is why the identity override
exists). `pgrep` appears only in comments telling the reader not to use it
(`goalflight_worktree_gc.py:35,342,401-402`).

**OBSERVED** (throwaway repo, `--apply` against that fixture only):

| ledger | identity | decision |
|---|---|---|
| `idle_timeout` | this process pid+token | retain, unowned=no |
| `idle_timeout` | pid=`2**30`, fake token | remove, then `--apply` deleted the tree |
| `worker_dead` | this process | retain |
| `blocked` | this process | retain |
| `idle_timeout` | pid only, no `start_token` | retain (fail closed: `_identity_live` → `None`) |

`--apply` removed the dead-identity tree and an unowned ad-hoc litter tree;
live-identity trees stayed on disk.

Revert R1 deleted the `live is True` / `live is None and liveness_verdict`
block. `test_idle_timeout_identity_live_worker_is_not_reclaimed` failed:
decision `remove` (unowned yes) because `idle_timeout` is terminal.
`test_idle_timeout_dead_identity_does_not_own_the_tree` still passed.

### Default seats = 24

**OBSERVED:** `DEFAULT_WORKTREE_SEATS == 24` at
`scripts/goalflight_worktree_pool.py:24`. Revert R3 → 4 made
`test_default_seat_count_is_not_a_per_controller_cap` fail; the other seat-pool
tests still passed because they set `GOALFLIGHT_WORKTREE_SEATS`.

---

## P2 — gc exempts `wt-N` by basename, not by seat registration

**Anchor:** `scripts/goalflight_worktree_pool.py:138-144`
(`is_pool_seat_path`); `scripts/goalflight_worktree_gc.py:484-491`
(`classify` returns retain before the four-condition conjunction).

The brief asked to confirm exemption by **registration**, not a name an ad-hoc
tree could match. The predicate is:

```python
name = Path(path).name
return name.startswith("wt-") and rest.isdigit() and int(rest) >= 1
```

No lock file, no `worktrees/` parent, no `configured_worktree_seats()` range.

**OBSERVED:**

- `is_pool_seat_path("/tmp/wt-1")` True; `"/repo/adhoc/wt-99999"` True;
  `"wt-01"` True (`"01".isdigit()`); `"wt-0"` False; `"t-353-live"` False.
- Ad-hoc worktree whose **directory** was `…/wt-3` (branch `adhoc-wt3`),
  merged + clean + unowned, **no** `wt-3.lock` (lock dir had only
  `allocation.lock` and `wt-1.lock` from a real acquire): gc retained it as
  `managed pool seat wt-3 is maintained by the worktree pool, not litter`.
  `--apply` left it on disk.
- `tests/python/test_worktree_gc.py:332-349`
  (`test_pool_seat_is_never_reclaimed_as_litter`) itself creates an ad-hoc
  `git worktree add -b wt-1` tree. It pins the name pattern, not a leased
  seat. Revert R2 (delete the `is_pool_seat_path` block) turned that test
  red with decision `remove`.

**Failure direction:** leak, not deletion. Real pool seats are named `wt-N`,
so they are protected. An ad-hoc tree accidentally named `wt-3` / `wt-01` /
`wt-99999` is immortal litter — the sprawl this change exists to stop, for
that naming. This repo's current worktree basenames (`sweep-*`, `t3*`,
`b2*`, …) do not match.

**Fix shape:** treat a path as a pool seat only when it is under the managed
root **and** the matching `goalflight-worktree-seat-locks/wt-N.lock` exists
(and, if cheap, `N <= configured_worktree_seats()`). Keep the name check as a
necessary but not sufficient condition.

---

## P2 — `--worktree` breaks the post-spawn caffeinate helper

**Anchor:** `scripts/goalflight_dispatch.py:14424-14427` (LOCK_FD stuffed into
the worker `env` dict, not `os.environ`), `:14498-14502` (launcher closes its
copy), `:14519-14528` (`_start_caffeinate(..., env=env)`), `:1041-1048`
(helper `pass_fds=pass_worktree_lock_fds()` with no env argument →
`inherited_worktree_lock_fds()` which **raises** if the env var names a
closed fd). Watcher spawn at `:14664-14666` uses `os.environ.copy()` and so
does not see LOCK_FD.

**OBSERVED** on the same `--worktree HEAD` launch that successfully handed
fd 5 to the worker:

```text
DISPATCH-REGISTRATION-WARN {"errors": [{"step": "caffeinate",
  "reason": "RuntimeError: caffeinate daemon spawn failed:
   {\"error\": \"WorktreeSeatError: GOALFLIGHT_WORKTREE_LOCK_FD does not
    name an open descriptor: '5'\"}"}]}
```

In-process: after `release()`, dispatcher-style `pass_worktree_lock_fds(env)`
returns `()` (fstat of the closed fd is swallowed). The helper is then
started with `env` still containing `GOALFLIGHT_WORKTREE_LOCK_FD=5` and
`pass_fds=()`. The helper's `inherited_worktree_lock_fds()` raises. Dispatch
still returns 0; the worker still holds the seat. Darwin `caffeinate -w`
never starts.

**Fix shape:** pop `GOALFLIGHT_WORKTREE_LOCK_FD` from the env passed to
sidecars, or teach `_cmd_spawn_daemon` to ignore a stale LOCK_FD instead of
raising, or pass caffeinate `os.environ.copy()` like the watcher.

---

## P2 — archive copies credential-shaped text; it only documents the risk

**Anchors:** `scripts/goalflight_trace_archive.py:8-44` (policy: tails
untrusted / possibly sensitive; never `git add`; historical 7.1 GB not
auto-copied), `:122-174` (`decide_archive`), `:219-227` (dropped list on a
keep), `:257-260` (manifest `git` note); `scripts/goalflight_output_redact.py:38`
(live tail filter is `xai-[a-z0-9]{20,}` only); `.gitignore:47` (`docs-private/`).

**OBSERVED:**

- Keep: marker run `keep-complete` written under
  `docs-private/traces/<day>/<id>/` with `MANIFEST.json`.
- Drop reported: `skip-capacity` (`blocked_capacity`, no marker) →
  `keep: false` with reason; steer mailbox not copied (`steer_copied=[]`);
  oversized tail recorded `dropped_bytes=8226` and
  `"tail middle bytes"` in `dropped`; manifest lists
  `steer mailbox`, `watcher log`, `caffeinate log`, `pidfile`, `prompt copy`.
- `git diff --cached` empty; `git status --porcelain` showed `?? docs-private/`
  (untracked, not staged). `.gitignore:47` matches
  `docs-private/traces/...`. Source never calls `git add` (the phrase appears
  only as "never git add`s").
- A tail containing `xai-abcdefghijklmnopqrstuvwxyz123456`,
  `sk-proj-OPENAISECRETVALUE0000000000`, `ghp_GITHUB…`, and a Bearer JWT was
  archived **verbatim**. `goalflight_output_redact.redact_text` would have
  replaced the xai- key only; archive does not call it.

Going-forward tails written by `_cmd_spawn_daemon` already pass through
`goalflight_output_redact.py`, so xai- keys in *those* tails should already
be `[redacted]` before `cmd_finish` archives them. Other token shapes are
not stripped at write time either. Sweeping an old `/tmp` backlog with
`--source-dir --apply` copies whatever is on disk.

**Fix shape:** run `redact_data`/`redact_text` (and broaden the secret
shapes) inside `archive_finished_dispatch` before write; keep the "never
git-add" rule.

---

## P2 — going-forward archive hook is untested (revert is silent)

**Anchor:** `scripts/goalflight_ledger.py:1141-1147`
(`cmd_finish` → `archive_finished_dispatch(record, apply=True)`, exceptions
swallowed). Tests: `tests/python/test_trace_archive.py` calls the archive
module directly; no test imports the ledger hook.

**OBSERVED:** revert R5 deleted the `cmd_finish` try/except. `pytest
tests/python/test_trace_archive.py` still **3 passed**. The tool's selection
policy is pinned; the integration that actually drains `/tmp` as dispatches
finish is not.

If the hook is dropped, new tails stay in volatile dispatch state and the
7.1 GB problem continues unless an operator runs `--source-dir --apply`.

---

## P3 — Claude preset and raw `--` do not receive the seat as cwd

**Anchors:** `scripts/goalflight_dispatch.py:13468-13469` (raw remainder
returned unchanged), `:13629-13643` (claude argv has no `--cwd`),
`:1041-1048` (daemon `Popen` has no `cwd=`).

**OBSERVED** after `_bind_dispatch_worktree`:

```text
grok  = ['grok', '--prompt-file', …, '--cwd', '<seat>/wt-1']
codex = […, '-C', '<seat>/wt-1', '-']
claude= ['claude', '-p', '--output-format', 'text']   # no seat
raw   = [python3, '-c', 'print(1)']                   # no seat
```

Grok/codex — the intended bash presets — do get the seat. Claude-as-bash and
`-- python3 -c` keep the dispatcher's process cwd. ACP shape is separate
(`cfg.cwd` = project root, `worktree=create` leases inside `acp_run`).

---

## P3 — retain reason says "non-terminal" for terminal liveness verdicts

**Anchor:** `scripts/goalflight_worktree_gc.py:426-432`.

**OBSERVED:** a live `idle_timeout` row (which `is_terminal_state` reports
True) is retained with
`non-terminal dispatch idle-live-w1 (state=idle_timeout)`. The guard is
correct; the sentence is not. An operator grepping "non-terminal" will
misread the incident this patch exists to name.

---

## Revert table

| Probe | Production cut | Named test | Result |
|---|---|---|---|
| R1 | `_record_owns_path` identity override | `test_idle_timeout_identity_live_worker_is_not_reclaimed` | FAIL (`remove`); dead-identity test still PASS |
| R2 | `is_pool_seat_path` retain in `classify` | `test_pool_seat_is_never_reclaimed_as_litter` | FAIL (`remove`) — pins **name**, not a lock |
| R3 | `DEFAULT_WORKTREE_SEATS = 4` | `test_default_seat_count_is_not_a_per_controller_cap` | FAIL; other seat tests still PASS |
| R4 | helper `pass_fds=()` | `test_seat_survives_for_worker_lifetime_then_frees_on_death` | FAIL (worker never inherited fd) |
| R5 | delete `cmd_finish` archive hook | `test_trace_archive.py` | still 3 PASS — **inert** |
| R6 | delete `WorktreeSeatUnavailable` handler | `test_worktree_exhaustion_refuses_honestly_and_does_not_add` | FAIL rc 1≠2; still no `git worktree add` |

Scripts restored; `git status -- scripts/goalflight_{worktree_gc,worktree_pool,dispatch,ledger}.py` empty.

---

## Archive drop policy (what it actually drops)

Stated in `goalflight_trace_archive.py:19-44` and emitted per keep in
`MANIFEST.json` / CLI `SKIP` lines:

- runs with no worker marker and no findings path (capacity-blocked, never
  spawned, empty tails)
- steer mailboxes
- watcher / caffeinate logs, pidfiles, prompt copies
- middle bytes of an oversized tail (64 KiB head + 192 KiB tail; count in
  the manifest and a marker in `tail.log`)
- the historical ~7.1 GB `/tmp` backlog, unless an operator passes
  `--source-dir --apply` (CLI without `--source-dir` exits 64 and says so)

Nothing is force-added to git.

---

## Residual / not scored

- **HYPOTHESISED:** a bash CLI that closes extra fds drops the bash
  `--worktree` lease early. ACP runner holds its own copy.
- Existing `--cwd` still allows two bash workers into the same tree (prior
  review, outside this range). `--worktree` is the occupancy-safe path;
  `--cwd` was required to keep working and does.
- `pass_worktree_lock_fds` after launcher `release()` returning `()` is the
  caffeinate bug, not a seat leak: the worker already holds the fd.
