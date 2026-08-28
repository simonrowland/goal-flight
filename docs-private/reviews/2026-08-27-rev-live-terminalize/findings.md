# rev-live-terminalize — sidecar terminal verdict vs identity-live worker

Review of `c2c6aa5` on `sweep-authority` (off main). Analysis only; source was
not edited except a trapped in-place revert of `scripts/goalflight_ledger.py`
and `scripts/goalflight_journal.py` that was restored before this file was
written (`git diff --stat` of those files empty after restore).

Isolated env: fresh `mktemp -d` for `GOALFLIGHT_JOURNAL_DIR`,
`GOALFLIGHT_STATE_DIR`, `GOALFLIGHT_WAKE_LEDGER` / `_DIR`,
`GOALFLIGHT_MESSAGES_DIR`, `GOALFLIGHT_TASK_STORE` / `_DIR`,
`GOALFLIGHT_PIDFILE_DIR`, `GOAL_FLIGHT_PIDFILE_DIR`,
`GOALFLIGHT_DISPATCH_DIR`, `GOALFLIGHT_CAPACITY_CONF=/dev/null`. Live ledger,
queue, and journals were not used. Spawned children were reaped.

Counts: **P0=0, P1=0, P2=2, P3=2**. Verdict: **FIX**.

The genuine dead-with-identity case still terminalizes. That is the question
this patch cannot get wrong; it does not.

---

## P2 — `unknown` / `no_pid` + a terminal sidecar never converges in this reconciler

**Anchors:** `scripts/goalflight_ledger.py:1407-1421`, `:1429-1438`, `:1284-1323`;
`scripts/goalflight_dispatch.py:7442-7447`;
`tests/python/test_goalflight_p3.py:2726-2740`

**OBSERVED** (isolated env, real sidecar file, no recorded pid, no journal
attempt):

| pass | `worker_identity_liveness` | `committed` | ledger `state` | journal attempt |
|---|---|---|---|---|
| 1 | `("unknown", "no_pid")` | 0 | `running` | none |
| 2 | same | 0 | `running` | none |

The gate is `if liveness != "dead": hold; continue`. `no_pid` maps to
`unknown`, never `dead` (`scripts/goalflight_ledger.py:402-408`). The
identity-dead `classify()` branch at `:1429-1438` is skipped whenever a
terminal sidecar produced `status_observation`. Nothing in
`reconcile_terminal_outbox` will ever promote this row.

`reconcile_abandoned_dispatches` is not a backstop for the sidecar the tests
write: `_abandoned_process_evidence` returns `False, "status_worker_alive:true"`
when the sidecar sets `worker_alive: true` (`goalflight_dispatch.py:7445-7447`).
That is the same sidecar shape as the original live-as-failed incident.

The author had to change `test_reconcile_projects_wake_before_slow_failing_history_mutation_pair`
to inject a proven-dead pid (`1_000_000_001`) so the ordering scenario still
commits. That is load-bearing evidence the previous no-pid + terminal-sidecar
path terminalized, and this patch holds it.

**Mitigation already in the patch (observed in source, not a contradiction):**
RUNNING journal attempts with `worker_instance_json.pid` are copied onto the
ledger record before the gate (`:1284-1323`). A worker that was marked running
with an identity still terminalizes when that identity is dead. The leftover
shape is "terminal sidecar, no ledger pid, no RUNNING journal instance."

**HYPOTHESISED (not claimed):** how often production hits the leftover shape
(failed launch, identity never written). The leak is a running ledger row and
any capacity lease still keyed on it, not the identity-dead workers this
review proved still close.

This is the over-correction the brief named, narrowed to the no-identity
case. It is not a regression of the genuine dead-with-identity path.

---

## P2 — `sidecar_terminal_overruled` is written where operator surfaces do not read it

**Anchors:** `scripts/goalflight_ledger.py:1173-1226`, `:1208-1214`;
`scripts/goalflight_journal.py:4161-4187`, `:4206-4207`, `:4257-4258`;
`scripts/goalflight_messages.py:2890`, `:3231`;
`scripts/goalflight_fleet_console.py:389`, `:2894-2906`

The patch's visibility story is a durable journal attention item plus the
reconcile JSON `overruled` list. Both exist.

**OBSERVED** after a live hold and an unknown hold in the same isolated journal:

- `Journal.attention_items()` returns `item_type=sidecar_terminal_overruled`
  with `reason=worker_identity_live` vs `reason=worker_identity_unknown`
  (distinguishable without parsing `payload_json`).
- Delivery events for those items: `recipient_label='*'`, `wake_class='quiet'`,
  `event_type='controller_attention'` (`journal.py:4257-4258`).
- `pending_delivery_events(..., waking_only=True)` — the default for
  `controller_pending_events` (`messages.py:2890`) and for the mail-summary
  path (`:3231`) — did not return those quiet events. `waking_only=False` did.
- `goalflight_fleet_console._attention_kind("controller_attention")` is
  `None`; `_attention_kind("sidecar_terminal_overruled")` is `None`.
  `_ATTENTION_KINDS` is `{user_need, user_confirm, blocked, advisory}` (`:389`).
  Unrecognised kinds are dropped, not promoted (`:2916-2923`).
- `ledger.status_payload()` JSON mentioned none of `sidecar_terminal_overruled`,
  `worker_identity_live`, `worker_identity_unknown`.
- `scripts/goalflight_status.py` and `scripts/goalflight_doctor.py` contain
  no `attention_items` / `sidecar_terminal_overruled` references (grep).
- Production `attention_items()` callers are envelope hydration in
  `goalflight_messages.py` when a `journal:` delivery event is already being
  peeked. The doorbell that would deliver that event is quiet and addressed
  at `*`. Contrast `orphaned_controller_work`, which got a separate fleet
  HUNG projector because the quiet doorbell was known not to be enough
  (`tests/python/test_listener_floor.py:258-261`).

The hold itself is visible as "still running." The disagreement the item
exists to record is not on status, doctor, fleet attention, or default waking
mail. A durable row nobody's operator surface reads is the dead-tripwire
class. `cmd_reconcile_outbox` prints `overruled` once, ephemerally.

---

## P3 — fail-on-revert of the five new tests is mostly a missing helper, not the live-terminalize assertion

**Anchors:** `tests/python/test_ledger_sidecar_terminal_gate.py:178,264,300,360,398`

**OBSERVED** after restoring `c2c6aa5^` of ledger+journal only (tests kept),
isolated env:

```
5 failed in 0.81s
```

Four failures: `AttributeError: module 'goalflight_ledger' has no attribute
'worker_identity_liveness'` at the pre-reconcile assertions. One failure:
`KeyError: 'overruled'` on the running-sidecar control, which is not the
defect. Author duration `5 failed in 4.33s` was not reproduced as a number;
the failure set is the same five nodeids.

The behavioral pin was reproduced separately against the reverted source
without calling the new helper (see Q6). That transcript is the actual
proof the tests target the right defect. The five pytest failures on revert
do not, by themselves, prove the live-worker terminalize.

---

## P3 — OPEN `sidecar_terminal_overruled` items are never resolved after the hold converges

**Anchors:** `scripts/goalflight_ledger.py:1208-1214` (`INSERT OR IGNORE` only);
no `RESOLVED` write on the converge path at `:1407-1421`

**OBSERVED:** after a live hold, kill, and a second reconcile that committed
the sidecar (`committed=1`, ledger `idle_timeout`, attempt `TERMINAL`), the
reconcile JSON `overruled` list was empty (liveness is now `dead`, so the
gate does not re-record). Nothing updates the existing OPEN item to
RESOLVED. Repeat reconciles of a still-held unknown row re-list it in JSON
but do not stack duplicates (uuid5 + `INSERT OR IGNORE`). Sticky OPEN after
convergence is the same pattern as `terminal_outbox_quarantined`.

---

## Attack questions

### 1. Does the genuine dead case still terminalize?

**Yes. OBSERVED, not inferred.**

Isolated spawn, capture `process_identity`, write `failed` sidecar, SIGTERM
+ wait, `process_identity(pid) is None`, then `reconcile_terminal_outbox`:

- `worker_identity_liveness` → `("dead", "dead")`
- `committed == 1`, `overruled == []`
- ledger `state=failed`, `terminal_state=error`, `worker_still_alive=False`
- journal attempt `TERMINAL`

The pytest `test_terminal_sidecar_terminalizes_genuinely_dead_worker` also
passed (5/5 on the fixed tree, 2.80s). The hold-then-converge path after
killing a previously live worker also committed (`committed=1`, ledger
`idle_timeout`).

### 2. Is the identity check honest?

**Mostly yes. OBSERVED.**

- Gate calls `worker_identity_liveness` → `identity_matches` →
  `compare_process_identities` (`ledger.py:391-408`, `:352-388`, `:282-330`).
  Start-token mismatch returns `pid_reused_start_token`; lstart mismatch
  returns `pid_reused_lstart`. `pgrep` does not appear in those helpers
  (`process_identity` uses `ps -p` + `pid_liveness`).
- Pid-reuse by re-key: two real children; recorded identity is the first
  child's start token with the second child's pid. Occupant exists (a pid-only
  check would say live). `worker_identity_liveness` →
  `("dead", "pid_reused_start_token")`; reconcile `committed=1`, ledger
  `complete`. Tokens differed (`darwin:…:427436` vs `darwin:…:429432`).
- OS-level recycle of the same pid number: **not obtained**. 400 spawn/kill
  attempts in 8s after killing the donor; `recycled=false`. Do not read the
  re-key as an OS recycle.
- Pid-only recorded identity `{pid: N}` against a live occupant:
  `identity_matches` → `(True, "live")` because
  `compare_process_identities` returns
  `identity_inconclusive_missing_expected_lstart` and `identity_matches`
  collapses that to `"live"` (`:386-387`). For this gate that is hold, not
  terminalize. The changelog's "pid AND start token, never pid alone" is
  stronger than the code: pid-alone occupancy is treated as live/hold, not
  compared via start token.

### 3. Is `unknown` distinguishable from `live` at a reader seam?

**Yes at the API the tests name; no at status / fleet attention.**

OBSERVED without parsing `payload_json`:

- `attention_items()[].reason` is `worker_identity_live` vs
  `worker_identity_unknown`
- reconcile JSON `overruled[].liveness` is `"live"` vs `"unknown"`

The gate's control flow collapses both to hold (`liveness != "dead"`). That
is the intended action. `status_payload()` and the fleet attention plane do
not carry either reason (see P2). A caller that only inspects `committed==0`
loses the distinction; the only production caller of
`reconcile_terminal_outbox` is `cmd_reconcile_outbox` (`ledger.py:1524-1527`,
grep).

### 4. Does the held state become permanent?

**Live: no. Unknown/no_pid: yes in this function (P2).**

OBSERVED for live: hold while the child is alive (`committed=0`, ledger
`running`, attempt `RUNNING`, child `poll() is None`); after SIGTERM + wait,
`worker_identity_liveness` → `("dead", "dead")` and the next
`reconcile_terminal_outbox` commits the sidecar.

**HYPOTHESISED additional live resolvers (not exercised end-to-end here):**
the worker's own `cmd_finish` path is independent of this reconciler; drain's
abandoned pass may close an identity-dead row after its stale floor.
`reconcile_terminal_outbox` itself is CLI-only in production (no drain/status
caller).

Unknown/no_pid: see P2. Two reconciles did not move it. Abandoned drain
refuses the `worker_alive: true` sidecar.

### 5. Is `sidecar_terminal_overruled` reachable by an operator?

**Reachable via `Journal.attention_items()` and the reconcile-outbox JSON.
Not reachable via status, doctor, fleet attention, or default waking mail.**
See P2.

### 6. Revert

**OBSERVED.**

- Five new tests fail on reverted ledger+journal (see P3).
- Behavioral transcript on reverted source, live child still running,
  sidecar `idle_timeout`:

```
child_still_alive: true
identity_matches: [true, "live"]
committed: 1
ledger_state: idle_timeout
terminal_state: idle_timeout
worker_still_alive: false   # ledger lie
attempt_state: TERMINAL
overruled_key_present: false
```

That is `committed=1` over an identity-live worker. Author's
`committed=1-over-live-worker` claim holds.

### 7. Author's gate-failure dismissals

Author: all full-gate failures were pristine-reproduced or live-fleet
artifacts. I did not re-run the 46-minute `./tests/run.sh`. Per-item:

| Dismissal | Independent check | Verdict |
|---|---|---|
| `test-ci-gate-honesty.sh` omits `tests/python/ext/test_claude_usage.py` | File is gitignored (`.gitignore:58`); absent on disk; `--list` uses `find` so a clean worktree omits it. Honesty test failed here with exactly that message. | **Confirmed** worktree/gitignore artifact, not this diff. |
| `test_write_failure_visibility` nested-shutdown + driver `test_deliberate_flake_is_reported_end_to_end` | Identical 2 failures on this tree and on `git archive c2c6aa5^`. `_detach_live_worker_state` missing from `_run_acp_dispatch_impl`; copied-conftest `machine_isolation` import. | **Confirmed** pristine-identical. |
| `test_dashboard_pidfile_write_distinguishes_contract_from_transient[error0-True]` | Author listed it as failing identically. It **passed** for me on both this tree and parent (2 failed, 57 passed, not 3). | **Not reproduced.** Not a regression from this patch. Treat the author's "3" as environmental/flaky. |
| `test_dispatch_queue` four drain tests | Identical 4 failures on current and parent in ~100s: three `watcher did not finish its terminal write; last_watcher={}`, one `No module named 'acp'` on homebrew 3.14. | **Confirmed** pristine-identical. |
| p3 four listener/doorbell tests | Identical 4 failures on current and parent: "never armed" at `:2381`, `:2551`, `:2858`/`:2850`, `:3306`/`:3298`. The adapted reconcile-ordering test **passed** on this tree (0.89s). | **Confirmed** pristine-identical for the four; the one test this patch touched is green. |
| `test-watch-dispatch-tail.sh` case-1j | Not re-run here. Commit does not touch `goalflight_watch.py` or that bash file. | **Not independently reproduced.** Plausible; do not treat as proven. |
| live-journal-isolation `project-<10hex>` | Before/after snapshot of `~/.local/state/goal-flight/journals` and `$XDG_STATE_HOME/.../journals`: same three named dirs, **zero** `project-*` children. Isolated runs left no `probe-*` / `revert-live*` in the live dispatch dir. Full-gate leak during the author's 47-minute run was not reproduced. | **Our runs did not leak.** Author's full-gate attribution to fleet churn is consistent with this snapshot but not re-proven for that 47-minute window. |
| Collection 219 → 220 | `pytest tests/python --collect-only`: 220 on this tree, 219 on `c2c6aa5^`. | **Confirmed.** |

No hidden regression was found behind the dismissals that I actually
re-ran. The one I did not re-run (case-1j) is in files this commit does not
touch.

---

## What is not a finding

- Live worker + `failed` / `idle_timeout` / `complete` sidecar holds,
  records `overruled`, leaves journal RUNNING. OBSERVED.
- Running sidecar is neither promoted nor flagged. Covered by tests; not
  re-probed beyond the 5/5 pass.
- Already-terminal records still re-commit (gate skipped when
  `record_terminal_key` is already terminal or the attempt is final). Read
  in source; the p2/p3 idempotency tests were not re-run as a set. The
  adapted p3 wake-before-history test passed.

---

## Method notes

- Interpreter for pytest: `/opt/homebrew/bin/python3` (3.14.5, pytest 9.0.3).
  `/usr/bin/python3` is 3.9 and has no pytest.
- Parent comparison tree: `git archive c2c6aa5^` at `/tmp/gf-parent-c2c6aa5hat`.
- Probe artifact: `docs-private/reviews/2026-08-27-rev-live-terminalize/probe-results.json`.
