# Sweep D — duplicated authority, and absence read as non-existence

Range: `75b63e5..de00f6c` (branch `sweep-authority` @ `de00f6c`).
Class: D1 two records of one fact (stale reader); D2 missing artifact treated as never-existed; absent identifier inviting a guess.
Analysis only. No source edits.

The range closed the cheaper D1 direction (sidecar frozen at `running` resurrecting a finished ledger). t-373 tests document that carve-out. This sweep checked the expensive reverse — a live thing reported absent/failed — including pre-existing code those commits walked past.

Counts: **P0=0, P1=1, P2=4, P3=2**.

---

## P1 — `reconcile_terminal_outbox` promotes a terminal sidecar into journal+ledger authority without checking that the worker is alive

**Anchors:** `scripts/goalflight_ledger.py:1264-1290`, `:1321-1327`

**Class:** D1 (sidecar is a second lifecycle writer) that becomes D2 after the write (live work then reads as finished/failed).

**Failure scenario**

1. Ledger row is `state=running`, `terminal_state=unknown`. Journal attempt is `RUNNING`. Worker pid identity-matches live.
2. `status.json` exists and carries a *terminal* sidecar state (`failed`, `idle_timeout`, or `complete`), including the case where that sidecar is a heartbeat/write-error copy and `worker_alive` is still `true`.
3. Anyone runs `goalflight_ledger.py reconcile-outbox` (the function's own docstring: "Repair terminal authority and classify provably dead workers once").
4. Because `terminal_state_for(sidecar.state)` is not `""` / `"unknown"` / `"watcher_stopped"`, the sidecar becomes `status_observation`.
5. `commit_terminal_authority(..., worker_still_alive=False)` fires. First-terminal-wins. Journal goes `TERMINAL`. Ledger is rewritten to the sidecar state.
6. Every post-t-373 reader now trusts that poisoned ledger/journal row. The controller treats the dispatch as finished/failed and can re-dispatch into a tree whose worker is still running.

The identity-dead branch at `:1298-1307` *does* call `classify()` and will `continue` on a live pid. The sidecar branch at `:1284-1290` skips that probe.

t-373 (`tests/python/test_ledger_lifecycle_authority.py:33`) explicitly labelled this path "not this defect". The range made ledger/journal the readers' authority, then left this writer feeding them from the sidecar.

**OBSERVED** (isolated `GOALFLIGHT_*` dirs under a `mktemp` root; this process as the live worker):

| sidecar `state` | `identity_matches` before | reconcile `committed` | ledger after | journal after |
|---|---|---|---|---|
| `failed` (`reason=status_write_error:simulated`, `worker_alive=true`) | `(True, 'live')` | 1 | `failed` / `error` | `TERMINAL` |
| `idle_timeout` (`worker_alive=true`) | `(True, 'live')` | 1 | `idle_timeout` | `TERMINAL` |
| `complete` (`worker_alive=true`) | live | 1 | `complete`, `worker_still_alive=False` | `TERMINAL` |
| `running` (`worker_alive=true`) | live | 0 | still `running` | still `RUNNING` |

Control: a `running` sidecar is not promoted. The t-373 corpse-as-live reader fix holds. The live-as-failed *writer* does not.

**HYPOTHESISED (not claimed):** how often production invokes `reconcile-outbox` outside tests. Production callers found: the `reconcile-outbox` CLI only (`:1392-1793`). Drain/status do not call it. Damage still durable when it *does* run: first-terminal-wins.

---

## P2 — watcher-dead repair overwrites `watcher_stopped` with `failed` when `status.json` cannot be written

**Anchors:** `scripts/goalflight_dispatch.py:1275-1312`, `:1154-1155`, `:14569-14597`

**Class:** D1 (computed watcher-liveness vs reported state) plus D2 (failed write treated as launch failure).

**Failure scenario**

Foreground wait (`_wait_for_detached_watcher`) sees the watcher die before a terminal sidecar. Repair computes:

- worker identity-alive → `state = "watcher_stopped"` (`:1275-1276`)
- that is the one state `_is_live_watcher_stopped` will keep open (`:1154-1155`)

If `write_status` then raises, `:1309` overwrites `payload["state"]` to `"failed"`. Comment at `:1306-1308` says terminal authority stays in the journal and this is only a mirror miss. The caller does not honor that: it returns the mutated payload, and the launch `finally` uses `final_state` to decide whether to `_finish_ledger`.

**OBSERVED**

- Writable status path, live pid: repair returns `state=watcher_stopped`, `worker_alive=True`, no `status_write_error`.
- Status path is a directory (write fails with `IsADirectoryError`): repair returns `state=failed`, `worker_alive=True`, `status_write_error` set. Disk file is absent (the write never landed).
- `_is_live_watcher_stopped("watcher_stopped", True)` is True.
- `_is_live_watcher_stopped("failed", True)` is False.
- `terminal_state_for("failed", ...)` is a terminal failure.

**HYPOTHESISED from reading `:14569-14597` (not an end-to-end `--foreground` launch):** `keep_live_watcher_open` is then False, so `_finish_ledger(dispatch_id, "failed", ...)` runs while the worker is still alive. Default launch is background (`not args.foreground`), so this is the `--foreground` join path, not every dispatch.

This is the same *shape* as the live receipt in the brief (reported `failed`, no `status.json`, worker finished minutes later), on a remaining writer.

---

## P2 — missing `status.json` *file* is empty evidence, not indeterminate; no recorded pid then means never-existed

**Anchors:** `scripts/goalflight_dispatch.py:7230-7246`, `:7442-7447`, `:7573-7580`

**Class:** D2.

`_abandoned_status_payload` distinguishes three misses, then the evaluator only fail-closes on `status is None`:

| miss | return | evaluator |
|---|---|---|
| no `status_path` on the ledger | `None, "status_path_absent"` | `status_indeterminate` (keep) |
| path recorded, file unreadable/wrong id | `None, "status_unreadable:*"` / `"status_dispatch_mismatch"` | keep |
| path recorded, file not found | `{}, "status_file_absent"` | continues with empty dict |

Empty status + no ledger pid → `_abandoned_process_evidence` returns `True, "no_recorded_pid"` (`:7442-7447`). After the 300s progress floor, drain closes the row as `inconclusive_no_final` / `abandoned_without_verdict`.

The docstring at `:7233-7235` says "a status file that was never created is absent evidence". That is the D2 predicate: FileNotFoundError is equally "created and the write failed" (the brief's live catch) or "watcher has not written yet".

Journal `dispatch_attempts.worker_instance_json` is a corroborating pid the range's *other* reconciler already copies onto a ledger row that lacks one (`goalflight_ledger.py:1238-1246`). Abandoned reconcile does not consult it.

**OBSERVED** (isolated env, `reconcile_abandoned_dispatches` with `now` +900s):

- Running ledger, `status_path` set, file absent, `worker_pid=None` → `_abandoned_status_payload` = `({}, "status_file_absent")`; closed `inconclusive_no_final`, `process_evidence=no_recorded_pid`.
- Same but `worker_pid=os.getpid()` with matching identity → kept `worker_live_or_indeterminate` / `worker:live:live`.
- Existing test `test_missing_status_pointer_is_ambiguous_and_left_open` only covers the pointer-absent arm, not the file-absent arm.

---

## P2 — watcher treats prompt+status both absent as "retired" without reading the ledger

**Anchors:** `scripts/goalflight_watch.py:3495-3501`, `:3352-3365`

**Class:** D2.

If `--ignore-prompt-file` is set (ACP and bash watchers both pass it) and *both* the prompt file and `status.json` are missing at startup, `main()` prints `dispatch retired` and returns 0. It does not call `_dispatch_record_is_nonterminal`.

The following check (`:3505-3514`) *does* consult the ledger, but only when the prompt file exists. The both-absent gate runs first.

`_dispatch_record_is_nonterminal` itself maps any OSError/JSON error to `False` (`:3356-3357`), so a missing ledger is "not nonterminal" = retired.

**OBSERVED** by the range-adjacent tests in `tests/python/test_goalflight_dispatch_acp_agents.py`:

- `test_watcher_both_absent_returns_before_scanner_initialization` patches `_dispatch_record_is_nonterminal` to **True** (live nonterminal ledger) and still gets `dispatch retired` / rc 0 / no status write. That is the predicate: ledger is not consulted.
- `test_nonterminal_dispatch_record_authorizes_missing_status_creation` shows the *prompt-exists* arm does use the ledger.

**HYPOTHESISED:** a live ACP worker whose prompt sidecar and status were both cleaned (or never written) while a watcher is (re)started. The watcher exits, writes no `status.json`, and leaves the worker unwatched. Not claimed as the common first-launch path: first launch normally has a prompt file.

---

## P2 — completion authority keys a terminal ledger row by `dispatch_id` only, ignoring `queue_launch_token`

**Anchors:** `scripts/goalflight_dispatch.py:8617-8632`, documented residual at `:5937-5953`

**Class:** D1. Two attempts, one id. Readers use the stale terminal record.

**Failure scenario**

Dispatch ids are reusable once terminal. A new queue carrier for the same id carries a new `queue_launch_token`. `_entry_completion_authority` Leg 0 returns `existing_terminal_record` from the *previous* attempt without comparing tokens. Drain then prints `not launched: existing_terminal_record` and can unlink the *current* carrier (`:5944-5950`).

The comment at `:5951-5953` states the real fix (bind completion authority to the token) as a separate change. The range's drain work (`d306c44`, `8d704e6`, `0ed4291`) walked past it.

**OBSERVED:** ledger row `state=complete` for `reused-id`; queue entry with `queue_launch_token=NEW-TOKEN-ATTEMPT-2`. `_entry_completion_authority(entry)` returned `{'state': 'complete', 'reason': 'existing_terminal_record', 'source': 'ledger'}`.

---

## P3 — `--wait` copies sidecar `state` onto a non-terminal ledger when journal authority is missing

**Anchors:** `scripts/goalflight_status.py:1549-1558`

**Class:** D1, residual of t-373's pre-journal/tmp seam.

When journal lifecycle is not live and not final (authority `None` / no attempt row), and the ledger is not structurally terminal, sidecar `state` / `terminal_state` are copied onto the wait record.

**OBSERVED:** `_wait_record_from_snapshots("overlay-id", {state: running, terminal_state: unknown}, {state: failed}, None)` returned `state=failed`, `terminal_state=unknown`.

Native post-t-373 launches have a journal attempt, so the live/final journal branches above this (`:1500-1548`) win. This remains the embedder/tmp/missing-journal seam.

---

## P3 — absent pid is `unknown_no_pid` to `classify()` and `confirmed_dead` to `--wait` liveness

**Anchors:** `scripts/goalflight_ledger.py:426-427` vs `scripts/goalflight_status.py:1760`

**Class:** D1 (two verdicts of one fact) and the absent-identifier predicate (absence vs explicit UNKNOWN).

**OBSERVED:**

- `classify({"state": "running"})` → `unknown_no_pid`
- `_wait_worker_liveness_detail({"state": "running", "classification": "unknown_no_pid"})` → `("confirmed_dead", "no_pid")`
- `done_code` for that row stays `2` (ambiguous), so `--wait` does not immediately terminalize. `confirmed_dead` currently only flips the snapshot's `worker_alive` bit.

Not a hot-path closer. Still two records of the same pid-absence fact, and the wait name (`confirmed_dead`) asserts non-existence.

---

## Searched and clean

These were hunted as the class and found sound, or fixed *in this range*.

**Fixed in range (the brief's live catch, D1/D2)**

- `de00f6c` / `scripts/goalflight_ledger.py:941-` (verified by `tests/python/test_goalflight_p2.py:336-`): recording `running` before the worker has claimed RUNNING is `attempt_not_yet_running` / `retryable: True`, not `cas_lost`. The old disposition made a startup race indistinguishable from a lost CAS and reported a live worker as a failed launch.
- t-373 / `8d5f021` / `406a55c`: readers of a *structurally terminal* ledger no longer overlay sidecar `running`/`stalled`. Re-verified here: a `running` sidecar does not reopen a live journal attempt via `reconcile_terminal_outbox`. `cmd_finish` still does not rewrite `status.json` (`:1111-1114`).

**D2 handled as unknown/indeterminate, not never-existed**

- Journal GC (`scripts/goalflight_journal_gc.py`, new in range): refuses `Path.exists()`; FileNotFoundError is `absent`, other OSError is `unknown`; unknown is never reclaimed. Empty journals whose project root still exists are kept (`6143efd`).
- `--wait` missing ledger row: 30s grace then "no dispatch record yet (continuing to wait)", never "never launched" (`goalflight_status.py:82-85`, `:2370-2384`).
- Abandoned reconcile missing *status_path pointer*: `status_indeterminate`, left open (`test_missing_status_pointer_is_ambiguous_and_left_open`).
- Abandoned reconcile live pid even with missing status file: kept (`worker:live:live`, this run).
- Fleet watch `_resolve_unconfirmed_without_status`: missing remote status consults pid identity; live pid waits; dead pid waits out grace (`goalflight_fleet_watch.py:480-527`).
- `reap_dispatch_homes`: missing ledger → not eligible to delete (`goalflight_reap_dispatch_homes.py:77-102`).
- Producer-set incomplete contract: `INDETERMINATE` / structurally absent, and a missing contract does not rescue an *unproven* death (`goalflight_dispatch.py:7323-7441`). Bare "no evidence" fail-closes except the no-pid empty-status arm in Finding 3.
- `classify()` maps `no_pid` to `unknown_no_pid`, not dead (`goalflight_ledger.py:426-427`).
- Fused controller identify: three-state snapshot, UNKNOWN vs mismatch (`b97e868`, `1ef148d`).
- Mail sender: `_controller_sender_session_id` keeps `None` on missing/ambiguous identity and must not guess (`goalflight_messages.py:1924-1951`). `cmd_post` omits `controller_label` rather than inventing one when env is empty.
- Watcher zombie descendants: missing `ps` state token is not a zombie (`goalflight_watch.py` range diff; `_is_zombie_or_defunct_state`).
- Capacity `read_records` placeholder `state=unreadable` is not a settled terminal (`goalflight_ledger.py:531-537`).

**D1 readers checked after t-373**

- `status_payload` / `done_code` / dashboard `live` / `--done`: overlay skipped when ledger is structurally terminal.
- Fleet `_authority_snapshot`: sidecar lifecycle ignored once ledger is structurally terminal (`goalflight_fleet_console.py:859-876`).
- Rate pressure: ledger terminal wins; sidecar used only as terminal-*failure* fallback (over-count, not live-as-absent) (`goalflight_rate_pressure.py:580-601`).
- `cmd_finish` missing ledger returns `missing_dispatch` (error), not success (`:1042-1044`).

**Grep predicates exhausted without extra live hits**

- `if not path.exists()` in dispatch/ledger/status/watch/messages/journal_gc/fleet_watch: remaining hits are config/profile/setup, or the instances above.
- `status.json` lifecycle readers after t-373: heartbeat/pid/trace/marker only, except Finding 1's writer and Finding 6's no-journal wait seam.
- Drain `existing_terminal_record` without token: Finding 5; not a silent success (it still prints `not launched:`).

---

## Out of class

- Drain journal-busy isolation (`d17faf5` / `d306c44`) is availability, not authority duplication. No extra note.

---

## Method notes

- Isolated env for every run: `GOALFLIGHT_JOURNAL_DIR`, `GOALFLIGHT_STATE_DIR`, `GOALFLIGHT_WAKE_LEDGER`, `GOALFLIGHT_MESSAGES_DIR`, `GOALFLIGHT_TASK_STORE`, `GOALFLIGHT_PIDFILE_DIR`, `GOAL_FLIGHT_PIDFILE_DIR`, `GOALFLIGHT_CAPACITY_CONF=/dev/null`, plus `GOALFLIGHT_DISPATCH_DIR` under the temp state dir.
- Did not touch live queue, ledger, or journals.
- "OBSERVED" means the function was called with constructed inputs and the return/disk state was read. "HYPOTHESISED" is called out where only the caller was read.
