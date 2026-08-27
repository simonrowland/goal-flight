# Review — rev-t376 capture idempotence + drain hold reasons

Read-only adversarial review of two commits off main on branch
`t376-capture-idem` (HEAD `63cf81b`) in
`/Users/simonrowland/Repos/goal-flight/worktrees/t376-capture-idem`.

- `531f47c` — `capture` content-hash idempotence
- `63cf81b` — drain hold-reason aggregation + dead-owner attention

**Verdict: CLEAN.** Severity: P0 0 · P1 0 · P2 0 · P3 0.

No time-only path to expiry of `restore_prepared` entries was found.
The capture hash is not over a timestamp, pid, actor, cwd, or attempt
counter. A colliding retry exits 0, prints the existing id, and
distinguishes minted from already-captured. `--allow-duplicate` still
mints a second item. Hold-reason arithmetic matched a constructed mix.
Reverting production (tests kept) made both new suites fail.

All runtime probes used a fresh `mktemp` tree for
`GOALFLIGHT_JOURNAL_DIR`, `GOALFLIGHT_STATE_DIR`, `GOALFLIGHT_WAKE_LEDGER`,
`GOALFLIGHT_MESSAGES_DIR`, `GOALFLIGHT_TASK_STORE` /
`GOALFLIGHT_TASK_STORE_DIR`, `GOALFLIGHT_PIDFILE_DIR` /
`GOAL_FLIGHT_PIDFILE_DIR`, plus `GOALFLIGHT_CAPACITY_CONF=/dev/null`.
Live queue and live task stores were not opened.

---

## Attack 1 — what the hash is over

**OBSERVED.** The key is:

```
capture:{sha1(kind 0x1f lane 0x1f (severity or "") 0x1f str(project_root) 0x1f _norm_key(title))[:16]}
```

Anchors:

- Derivation and exclusions: `goalflight_task.py:3693-3722`
- Actual payload: `goalflight_task.py:3722`
- `_norm_key` folds case and whitespace: `goalflight_task.py:1606-1607`
- `_short_hash` is SHA-1, first 16 hex chars, fields joined by `0x1f`:
  `goalflight_task.py:1610-1612`

Excluded, as claimed: timestamp, actor, attempt counter, raw cwd
(`source`), tags/links/blocked_by/acceptance/prompt/pattern, `id_family`.

Runtime (isolated CLI, real store):

| Probe | Result |
| --- | --- |
| First `capture "Race in retry path"` | minted `t-001`, exit 0, stderr `captured t-001` |
| Retry after 1.2s, cwd=`/tmp`, different `GOALFLIGHT_TASK_ACTOR` / `GOALFLIGHT_DISPATCH_ID` | **same** `t-001`, exit 0 |
| Retry with extra whitespace/case (`"  Race   in retry PATH  "`) | same `t-001` |
| Retry with `--source /var/empty` | same `t-001` |
| Retry with `--tag extra` | same `t-001` |
| Store count after those retries | **1 item** |
| Stored `capture_key` | `capture:2e3a21219822c599` |
| Independent SHA-1 of `task\x1fdeferred\x1f\x1f{project_root}\x1frace in retry path` | **exact match** |
| Same title, `--severity P2` | new id `b-001` (kind+severity in the key) |
| Same title, `--lane held` | new id `t-006` (lane in the key) |
| Two concurrent `capture` of the same text | same id; one `already_captured: false`, one `true`; both exit 0 |

`created_at` on the minted item was `2026-08-27T21:00:16+00:00` and
`created_by` was `reviewer-first`; the retry used a different actor and
happened later. The key still matched. The item's `source` stayed the
first-attempt cwd and was not rewritten.

`project_root` is in the key (`goalflight_task.py:3817` uses
`store.project_root`). Inside one store that value is constant (CLI
constructs the store via `resolve_project_root`,
`goalflight_task.py:5617`), so it does not fork an original attempt from
its retry. Different projects are different stores.

**Not a defeat of the mechanism.**

---

## Attack 2 — can a deliberate duplicate still be filed?

**OBSERVED. Yes.** `--allow-duplicate` is wired on the capture parser
(`goalflight_task.py:5279-5283`) and skips the live-key check
(`goalflight_task.py:3786-3789`). The new item still carries the key.

Runtime: `capture "Same finding, filed twice on purpose"` → `t-003`;
same text with `--allow-duplicate` → **new** `t-004`, exit 0, stderr
`captured t-004`; a subsequent plain retry returned `t-003` (first live
match), store still two items.

A `done` item does not block recurrence (`goalflight_task.py:3788`
`not item.get("done")`). Runtime: `t-009` marked done, recapture minted
`t-010`, then a retry of that recapture returned `t-010`.

Idempotence is not unconditional.

---

## Attack 3 — colliding retry exit and minted vs already-captured

**OBSERVED. Exit 0, existing id, distinguished.**

`_cmd_capture` always `return 0` after a successful mint or collision
(`goalflight_task.py:3804-3835`). Collision writes nothing.

| Mode | First | Retry |
| --- | --- | --- |
| text stdout | `t-001` | `t-001` |
| text stderr | `captured t-001 (deferred). …` | `already captured as t-001 (deferred); not re-minted. use --allow-duplicate …` |
| `--json` stdout | `{"already_captured": false, "id": "t-002"}` | `{"already_captured": true, "id": "t-002"}` |
| exit | 0 | 0 |

Text-mode stdout is the id only (jq-clean); the minted / already-captured
distinction is on stderr and in the JSON flag. That is enough for a
timeout-retry caller: it learns the id and is not pushed back into
"did this mint?" via a non-zero exit.

---

## Attack 4 — dead-owner expiry (the dangerous half)

**OBSERVED. Reporting only. Proof-gated. Unknown holds. No time-only expiry.**

Owner adjudication: `scripts/goalflight_dispatch.py:6136-6167`
(`_restore_prepared_owner_state`). It reuses
`_queue_claim_identity_status` (`scripts/goalflight_dispatch.py:6095-6130`),
which itself calls `goalflight_ledger.identity_matches`.

| Input | Classifier | Attention? | File after drain? |
| --- | --- | --- | --- |
| live controller pid + start_token | `owner_state=live` | no | still present |
| captured identity, process then killed | `owner_state=dead`, `owner_reason=dead` | **yes** `owner_generation_dead` | still present |
| no ledger record | `unknown` / `no_ledger_record` | no | still present |
| live pid, no stored start_token | `unknown` / `controller_identity_unavailable` | no | still present |
| empty record | `unknown` / `no_controller_pid` (direct call) | — | — |
| `controller_pid=999999` (gone), no identity | `dead` (gone pid without token, as documented at `:6155-6156`) | yes | still present |
| `created_at=2019-01-01`, no ledger, `--claim-stale-s 0` | `unknown` | no | **still present** |

Drain partitions `state==restore_prepared` out of launch candidates
(`scripts/goalflight_dispatch.py:12032-12047`) and, on `owner_state==dead`,
appends an attention item with the comment "never an expiry" /
"queue file itself is left untouched"
(`scripts/goalflight_dispatch.py:12141-12157`).

Runtime snapshot of the isolated queue: restore_prepared and
not_before JSON **contents were not rewritten or removed**. Drain
created a `.submit.lock` in the queue dir (lock file, not an entry).
A second drain with `--claim-stale-s 0` reported `mutated=False` for
the entry files; the 2019 envelope and the dead-owner envelope both
remained.

`holds.quarantined` is `recovery.get("quarantined")` — **this-pass**
quarantines from claim recovery, not occupancy of `queue/quarantine/`.
A pre-existing quarantined file in that subdir did not increment the
count (OBSERVED `quarantined: 0`). Those files are already off the
`*.json` glob and are not launched; this is occupancy vs this-pass, not
an expiry path.

**HYPOTHESISED-from-code (not runtime-injected):** an unreadable ledger
sets `records_by_id = None` (`scripts/goalflight_dispatch.py:12112-12119`)
and every restore_prepared then sees `record=None` → `unknown`. Fail
closed, same as the direct `None` call.

**No blind sweeper.** This commit does not unlink, expire, or requeue
`restore_prepared` JSON on age. Claim recovery still uses `stale_s` as a
*gate* after identity classification (`classify_reconciliation_admission`
at `scripts/goalflight_dispatch.py:6366`: "elapsed time never proves
death") — that path is for `*.json.claimed-*` carriers, not for the new
restore_prepared reporting.

---

## Attack 5 — hold-reason counts vs a constructed mix

**OBSERVED. Arithmetic matched. No SC-15 denominator split between a
hold category and its body.**

Constructed isolated queue:

- 2 future `not_before` (`quota-soon` +3h, `quota-later` +8h)
- 5 `restore_prepared`: live, dead, unknown (no ledger), unknown
  (pid-only), ancient unknown
- 2 claimed `*.json.claimed-1` with non-terminal ledger rows
  (reconcile-pending)
- 1 pre-existing file under `queue/quarantine/`

`--json` holds:

```
not_before.count=2  until=<quota-soon timestamp>   (earliest: yes)
restore_prepared.count=5  owner_live=1 owner_dead=1 owner_unknown=3
  live+dead+unknown = count
reconcile_pending.unlinked_quarantine_deferred=2
quarantined=0   (this-pass; see Attack 4)
```

Cross-check against the body:

| Field | Value | Body |
| --- | --- | --- |
| `left_queued` | 7 | 2 not_before + 5 restore_prepared JSON |
| `remaining` | 7 | `glob("*.json")` after the pass (launched=0) |
| `details` | 9 | 2 claimed + 2 not_before + 5 restore_prepared |
| `attention` | 1 (`owner-dead`) | equals `owner_dead` |
| text `waiting_not_before` | 2 | equals `holds.not_before.count` |
| text `awaiting_owner_reconcile` | 5 | equals `holds.restore_prepared.count` |
| text `owner_generation_dead` | 1 | equals `owner_dead` |
| text `attention` | 1 | equals `len(attention)` |
| text `unknown_claimer` | 2 | the two claimed orphans |

`left_queued` (7) vs `details` (9) is not a hold-summary disagreement:
`left_queued` is remaining `*.json` envelopes; claimed carriers live as
`*.json.claimed-*` and are counted under `holds.reconcile_pending` /
`unknown_claimer`, which matched those two detail rows.

Author text-summary test
(`tests/python/test_drain_hold_summary.py:287-316`) pins the same
flattening (`waiting_not_before`, `awaiting_owner_reconcile`,
`owner_generation_dead`, `attention`).

---

## Attack 6 — are the new tests real (b-235)?

**OBSERVED. Yes. Revert re-run by this review, not taken from the author.**

### Capture suite — real store, real CLI

`tests/python/test_capture_idempotence.py` drives `goalflight_task.py` as
a subprocess against a tempfile `--project-root` and asserts on stdout,
stderr, exit, and `list --json`. No store double.

Gate-isolated run: `OK: capture idempotence tests pass` (6 cases).

**Revert:** `git show 531f47c -- goalflight_task.py | git apply -R`
(tests file left in place). Result:

```
AssertionError: retry returns the SAME id
CAPTURE_REVERT_TEST_EXIT=1
```

That is the pre-change double-mint. Production restored via
`git checkout -- goalflight_task.py`.

### Drain suite — real queue, real ledger, real processes

`tests/python/test_drain_hold_summary.py` writes queue JSON, writes
ledger records through `goalflight_ledger.write_record`, spawns live
sleepers for `process_identity`, kills one, and calls `D._cmd_drain`.
The only doubles are dashboard export/refresh no-ops and env isolation.
It even asserts `identity_matches(...) == (False, "dead")` on the killed
generation (`tests/python/test_drain_hold_summary.py:185-188`).

Isolated pytest (`/opt/homebrew/bin/python3`,
`GOALFLIGHT_ISOLATED_TEST_FILE=test_drain_hold_summary.py`): **4 passed**.

**Revert:** `git show 63cf81b -- scripts/goalflight_dispatch.py | git apply -R`.
Result: **4 failed**, all `KeyError: 'holds'` (text test:
`KeyError: 'waiting_not_before'`). Production restored via
`git checkout -- scripts/goalflight_dispatch.py`. Working tree clean
afterwards.

---

## Observations that are not defects

- **`new` does not participate.** `_cmd_new` still calls `_create_item`
  without `capture_key` (`goalflight_task.py:3795-3796`). Runtime:
  `new "Title shared with capture"` minted `t-007`; `capture` of the
  same text minted `t-008`. Collision is by stored `capture_key`, not by
  recomputing title against every live row. The stated problem is a
  `capture` timeout-retry, which this covers. Pre-change live rows also
  lack the field; a retry after upgrade of an *old* capture would still
  double-mint. Out of scope of the two commits unless a follow-up
  backfill is wanted.
- **Gone pid without a start_token is `dead`**, so it is surfaced in
  `attention` but still not expired (`:6155-6156`). That matches the
  commit message ("gone pid or start-token mismatch"). False-dead
  *attention* is possible if the ledger pid is stale while the
  controller lives under a new pid; it is not a delete.
- Capture tests inherit `GOALFLIGHT_TASK_STORE_DIR` from the caller;
  `tests/run.sh` isolates it. They do not self-isolate. The gate path
  is the one that matters; this review isolated them anyway.

---

## Test evidence (this review)

| Run | Result |
| --- | --- |
| Isolated capture CLI probes | hash/retry/allow-duplicate/exit/JSON/parallel as above |
| Isolated drain mix + stale-s=0 + gone-pid-no-token | holds arithmetic, no entry expiry |
| `python3 tests/python/test_capture_idempotence.py` (isolated env) | pass |
| `pytest tests/python/test_drain_hold_summary.py` (isolated env) | 4 passed |
| Reverse `531f47c` production, keep tests | fail: `retry returns the SAME id` |
| Reverse `63cf81b` production, keep tests | 4 failed `KeyError: holds` |
| Restore both files | `git status` clean |

Did not chase the four pre-existing `test_dispatch_queue.py` failures.
Did not run the full `./tests/run.sh` gate (author was killed mid-gate;
this review's contract is the two commits).
