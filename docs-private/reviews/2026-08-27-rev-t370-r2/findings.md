# rev-t370-r2 — refutation pass on the mail-attribution fix round

Review only. Source not edited (temporarily reverted `scripts/goalflight_messages.py`
for test pinning, then restored; sha256
`51ff1efba4b76b97cd1459ddc4fef331b74ceebd660c7425b868dae2c24d3d21` matched
before and after).

Worked from `/Users/simonrowland/Repos/goal-flight/worktrees/t370-mail-attrib`
on `t370-mail-attrib` at `1b1fff6` (round 1 `c87d0e7`; round 2 `ffd22bc`
`e27866d` `1b1fff6`). Isolated `mktemp` trees for
`GOALFLIGHT_JOURNAL_DIR`, `GOALFLIGHT_STATE_DIR`, `GOALFLIGHT_WAKE_LEDGER`,
`GOALFLIGHT_MESSAGES_DIR`, `GOALFLIGHT_TASK_STORE` /
`GOALFLIGHT_TASK_STORE_DIR`, `GOALFLIGHT_PIDFILE_DIR`,
`GOAL_FLIGHT_PIDFILE_DIR`, plus `GOALFLIGHT_CAPACITY_CONF=/dev/null`. No live
controllers were posted to. Key cases ran both with and without
`GOALFLIGHT_DISPATCH_ID`.

P0: 0 · P1: 0 · P2: 1 · P3: 2 · verdict **FIX**

Round 1's two P1s (CLI leftover-dispatch omit; PID/repo invented as sender)
did not regress. The six census admit paths now stamp `source.controller_label`
or `UNKNOWN` when exercised by posting. `relay --new` round-trips the four
journal-delivered paths. Fleet steering and quota advisories stamp on the
carrier but never appear at `relay --new` (they do not journal-deliver).
The remaining P2 is the TRUSTED from-line: a forged label still prints as
`from`, and `author_digest` does not bind that name.

---

## Attack 1 — six CLOSED paths, verified by posting

Census claim: `cmd_post` (no leftover id), `cmd_post` with
`GOALFLIGHT_DISPATCH_ID`, MCP `goalflight_post_message_tool`, library
`post_message`, `write_steering_envelope`, `_post_quota_advisories`.

Stamp lives in `_stamp_controller_source_label`
(`scripts/goalflight_messages.py:1961-1984`), called from `post_message`
(`:1331`) so leftover-dispatch CLI cannot omit the field, and from
`write_steering_envelope` (`:4942`) which bypasses `post_message`.
`GOALFLIGHT_DISPATCH_ID` forces `UNKNOWN` (`_controller_post_source_label`
`:1955-1956`).

| Path | Posted? | Stored `controller_label` | `relay --new` |
|---|---|---|---|
| CLI `cmd_post`, no leftover | yes | `probe-ctl` | `cli-no-dispatch #1 [finding] from probe-ctl:` |
| CLI `cmd_post`, leftover id (incident shape) | yes | `UNKNOWN` (field **present**) | `cli-with-dispatch #1 [finding] from UNKNOWN:` |
| MCP `goalflight_post_message_tool` both ways | yes | `probe-ctl` / `UNKNOWN` | `mcp-*-dispatch #1 [finding] from {label}:` |
| Library `post_message` both ways | yes | `probe-ctl` / `UNKNOWN` | `lib-*-dispatch #1 [finding] from {label}:` |
| `write_steering_envelope` both ways | yes | `probe-ctl` / `UNKNOWN` | **absent** — fleet register only |
| `_post_quota_advisories` both ways | yes | `probe-ctl` / `UNKNOWN` | **absent** — carrier `controller-quota-advisory.jsonl` only |

**OBSERVED** (isolated CLI `--to-controller` / MCP with addressee / library
with addressee, then `relay --new` / `--bodies`):

- Incident shape under leftover `GOALFLIGHT_DISPATCH_ID` is no longer the
  round-1 omit. Stored source is
  `{node: local, adapter: unknown, transport: controller, controller_label: UNKNOWN}`.
  `relay --new` prints `from UNKNOWN`. `--bodies` JSON still contains the key.
- MCP and `post_message` match the CLI stamp under the same env.
- Steering (`:4941-4942`) and quota (`scripts/goalflight_status.py:2561-2566`
  via `post_message`) stamp the field. Quota with a declared label rendered
  `from probe-ctl` on `format_envelope_headlines`. With leftover id, the
  stored sentinel is `UNKNOWN` and `envelope_from` yields the informative
  adapter (`fleet` / `goalflight_status`) — that is the `1b1fff6` rule, not
  an omitted field.
- `_journal_delivery_targets` (`scripts/goalflight_messages.py:1519-1576`)
  delivers only with an addressee, a dispatch record, or payload
  `project_root`. Quota payload has none of those; steering never calls
  `post_message`. So those two producers cannot be read back at
  `relay --new`. Their round-2 tests named `*_round_trips` assert
  `format_envelope_headlines`, not relay.

**HYPOTHESISED and rejected:** a remaining admit path that still writes
controller-transport mail without the field. Production `"transport":
"controller"` literals in `scripts/` are only `post_message`'s default
(`:1321`), steering (`:4941`), and quota (`goalflight_status.py:2566`).

Round 1 closed paths (documented CLI without leftover; MCP) still round-trip
at relay. No regression.

---

## Attack 2 — seven N/A-by-transport claims

The census used transport, not "never appears in the controller mailbox".

| Path | Transport | Reaches `relay --new`? | Stamp `controller_label`? | N/A as controller-transport? |
|---|---|---|---|---|
| `post_controller_steer` `:2033` / source `:2041` | `steer` | no (`controller_delivery` null; worker-directed) | no | **yes** |
| `post_trace_attention` `goalflight_watch.py:932` / `:958` | `trace-liveness` | no in this probe | no | **yes** |
| `post_worker_wait_attention` `:1090` / `:1133` | `steer-wait` | no in this probe | no | **yes** |
| `project_terminal_outbox` `goalflight_journal.py:4847` / `:4873` | `journal` | **yes** `from journal-outbox` | no | **yes** (transport) |
| `_post_task_store_nudge` `goalflight_task.py:4779` / `:4829` | `next-frontier` / `done-suggest` / `resume-frontier` | **yes** `from task-store` | no | **yes** (transport) |
| `from-text` / `markers_to_envelopes` `:2124` | default `tail_file`; `--transport controller` is a CLI choice `:7880-7884` | does not post | n/a | **yes** as admit; see caveat |
| `merge_remote_register` / `cmd_mirror` `:4971` / `:7855` | copies stored envelopes | no (fleet register) | no (passthrough) | **passthrough**, not a producer |

**OBSERVED:**

- Steer / trace / wait-attention posted through the real functions. Stored
  transport was not `controller`. No `controller_label`.
- `commit_terminal` then `project_terminal_outbox` wrote
  `{adapter: journal-outbox, transport: journal}` and **did** show up on
  `relay --new` as `na-outbox-worker #1 [result] from journal-outbox:`.
- `_post_task_store_nudge(..., transport="next-frontier")` wrote
  `{adapter: task-store, transport: next-frontier}` and **did** show up as
  `from task-store`. Callers (`:4844`, `:4872`, `:4900`) never pass
  `transport="controller"`.
- `markers_to_envelopes(..., transport=controller)` constructs the incident
  three-key source with **no** `controller_label`. `envelope_from` →
  `UNKNOWN`. It prints; it does not write a carrier. The `from-text` CLI on
  system Python 3.9 dies in `acp_runner.PromptResult` (`str | None`) before
  posting — unrelated to this ticket. Homebrew 3.14 is what the headline
  tests use.
- `merge_remote_register` / `cmd_mirror`: a valid unlabeled
  controller+`adapter=fleet` row and a labeled `remote-ctl` row both
  appended. Unlabeled stayed unlabeled (`from fleet` via adapter fallback).
  Labeled stayed `remote-ctl`. No stamp, no rewrite. Round 1 called this
  passthrough; calling it N/A only holds if N/A means "does not mint new
  controller-transport attribution".

Declaring the seven N/A to shrink stamp work is honest for **controller
transport**. It is not honest as "does not reach the controller channel" for
outbox and task-store nudges: those are mail the controller's `relay --new`
shows. They are attributed by adapter, which is the right producer name for
those transports.

---

## Attack 3 — PID / repo heuristic cannot become a sender

**P0 question:** can a pid or git directory name still surface as a sender?

**OBSERVED:** `GOALFLIGHT_CONTROLLER_LABEL` dropped, `GOALFLIGHT_CONTROLLER_PID=424242`,
cwd a git dir named `probable-sender-repo`.

- `resolve_controller_label` (`scripts/goalflight_session_status.py:121-141`)
  still returned `"probable-sender-repo"`. The inventing fallback remains
  session identity (listen / follow / supervise / lease lookup).
- `_controller_post_source_label` (`scripts/goalflight_messages.py:1943-1958`)
  returned `None`. It reads only `GOALFLIGHT_CONTROLLER_LABEL` and refuses a
  leftover `GOALFLIGHT_DISPATCH_ID`. It does **not** call
  `resolve_controller_label`.
- CLI post stored `controller_label=UNKNOWN`. `relay --new` →
  `pid-fallback #1 [finding] from UNKNOWN:`. Neither `424242` nor
  `probable-sender-repo` appeared in the source dict.
- Same under leftover dispatch id.
- `post_controller_steer` under that env: transport `steer`, no
  `controller_label`, no `controller_session_id` (live-session lookup failed
  without a matching session). `from goalflight-dispatch`.

Callers of `resolve_controller_label` that remain
(`_controller_sender_session_id` `:2001`, `_verified_controller_identity`
`:2805`, `controller_mail_summary` `:3296`, claim/follow/listen/supervise,
dispatch session stamp) use the name as a **mailbox / session key**, not as
`source.controller_label`. `envelope_from` (`:4130-4162`) never reads the
resolver.

**Not P0.** Heuristic still invents a session label; it is no longer a
from-line.

---

## Attack 4 — UNKNOWN at relay, vs named UNKNOWN, vs absent field

**OBSERVED:**

- Unestablishable CLI (all `GOALFLIGHT_CONTROLLER*` dropped): stored
  `controller_label=UNKNOWN`; `relay --new` `from UNKNOWN`. Field present.
  Distinct from the pre-fix omit at the JSON seam.
- Incident leftover dispatch + default `adapter=unknown`: same stored
  sentinel; `relay --new` `from UNKNOWN`.
- Pre-existing record with the field stripped after admit: `relay --new`
  exit 0, `legacy-absent #1 [finding] from UNKNOWN:`, did not print
  `probe-ctl`, did not crash (`validate_envelope` `:663-668` only constrains
  the value when present).
- Controller literally named `UNKNOWN`: stored value is also the string
  `UNKNOWN`; `envelope_from` treats it as not a real label (`:4147-4150`).
  **From-line and stored value are indistinguishable from the sentinel.**
  `relay --new` under `GOALFLIGHT_CONTROLLER_LABEL=UNKNOWN` exited 2
  (`controller label 'UNKNOWN' is not among ACTIVE leases: probe-ctl`)
  because the lease was still `probe-ctl`. Relayed later under `probe-ctl`
  as `from UNKNOWN`.

Absent field vs explicit `UNKNOWN`: distinguishable in JSON, not on the
from-line (intentional backward compat). A controller whose durable name is
the sentinel collides. P3.

---

## Attack 5 — TRUSTED vs VALIDATED, and whether `author_digest` is live

They chose TRUSTED. The docstring at `_stamp_controller_source_label`
(`:1961-1977`) is accurate about lease ambiguity and about MCP / status /
fleet posters often carrying no unique nonce.

**Judge the argument.**

- Several controllers can hold leases on one journal: not re-litigated here;
  `lease_records()` is a list. Validating against "the" active lease would
  be ambiguous.
- MCP / status / fleet may carry no unique lease nonce: **OBSERVED.** MCP
  and quota posts in this probe had `author_digest: null`. Steering bypasses
  `post_message`'s capability path entirely. CLI only mints a digest when
  `_presented_ambient_controller_capability` (`:1928-1940`) sees exactly one
  of `GOALFLIGHT_CONTROLLER_LEASE_NONCE` / `GOALFLIGHT_CONTROLLER_SESSION_ID`
  and no leftover dispatch id. A bash-tool post with only
  `GOALFLIGHT_CONTROLLER_LABEL` mints no digest.
- Validating would blank legitimate producers without proving authorship:
  coherent. The leftover-dispatch CLI **cannot** present a unique nonce
  (`:2528-2529` skips capability when `GOALFLIGHT_DISPATCH_ID` is set).
- `from` is a self-asserted name: **OBSERVED.**

**Is `author_digest` a dead tripwire (SC-37)?** Not dead, but it proves a
different fact than the from-line.

**OBSERVED readers** of the digest:

- `envelope_authored_by_controller` (`:4165-4181`) compares the envelope
  digest to `controller_session_digest(lease_nonce)` and **explicitly
  discards** `controller_label` (`:4174`).
- `_foreign_controller_items` (`:4309-4323`) keeps items **not** authored
  by the viewing controller.
- Used by `relay --new` (`:4568-4573`), follow (`:6276`), listen
  (`:7675`, `:7689`).

So the digest is a live **self-vs-foreign filter** on peek/wake, not a
check that the printed name is true.

**OBSERVED lie:** `post_message` with
`source.controller_label=NOT-THE-LEASE-HOLDER`, no capability. Stored that
name, no digest. `relay --new` →
`trust-forged #1 [finding] from NOT-THE-LEASE-HOLDER:`. A reader who never
looks at the digest (relay does not print it) is misled.

**OBSERVED hide:** same impostor name **with** the real lease nonce as
capability. Digest minted and matched the lease holder. `relay --new` as
that holder **omitted** the row (`hidden_as_self`). `relay --drain` still
printed it, because drain iterates `items_with_rows` (`:4616-4617`), not
`visible_items`, and `format_receipt_headline` (`:4255-4268`) has no
`from` for findings.

The TRUSTED decision is the right stamp-time policy. It does not close the
incident's sibling: a plausible string in `from` is believed. That is the
residual P2.

---

## Attack 6 — `1b1fff6` label ahead of a shared adapter

**OBSERVED:**

- CLI `post --adapter codex` with declared `probe-ctl`: stored both;
  `envelope_from` / `relay --new` → `from probe-ctl`, not `from codex`.
  Round 1 P2 "adapter wins over a real label" is closed.
- UNKNOWN + `adapter=fleet` → `from fleet`. UNKNOWN +
  `adapter=goalflight_status` → `from goalflight_status`. Documented and
  pinned by `test_from_renders_unknown_for_unattributed_controller_mail`.
- Genuine adapter-sourced mail: MCP
  `{adapter: acp, transport: tail_file}` did not stamp `controller_label`;
  `envelope_from` → `acp`.

**OBSERVED leftover + informative adapter (P3):** CLI with
`GOALFLIGHT_DISPATCH_ID` **and** `--adapter codex` stored
`controller_label=UNKNOWN` but `relay --new` printed `from codex`. The
sentinel yields to any non-`unknown` adapter (`:4154-4156`). The documented
send omits `--adapter` (default `unknown` at `:7911`) and still says
`from UNKNOWN`. This is the `1b1fff6` rule applied to CLI, not a regression
of the real-label fix.

---

## Attack 7 — backward compatibility

**OBSERVED.** Planted a controller-transport finding, stripped
`controller_label` on the carrier, ran `relay --new`: exit 0,
`legacy-absent #1 [finding] from UNKNOWN:`, no invented sender, no crash.
Matches round 1's closed reader path. No regression.

---

## Attack 8 — reverts (tests kept, `scripts/goalflight_messages.py` swapped)

Focused `tests/python/test_messages_headlines.py` slice (15 tests at HEAD).
Isolated env; Homebrew Python 3.14 + pytest. File hash restored after.

| Module version | Result that pins the round-2 claims |
|---|---|
| HEAD `1b1fff6` | 15 passed |
| round 1 `c87d0e7` | 10 failed / 5 passed. Leftover CLI, primitive, steering, quota: `KeyError controller_label`. PID-without-label (no leftover): stamps `probable-sender-repo` not `UNKNOWN`. Adapter still beats a real label (`codex` vs `probe-ctl`). |
| `ffd22bc` (`e27866d^`) stamp-every-path, heuristic still live | 2 failed: PID-without-label invents `probable-sender-repo`; adapter still beats label. Leftover CLI / primitive / steering / quota now pass (stamp is in `post_message`). |
| `e27866d` (`1b1fff6^`) heuristic gone, adapter still wins | 1 failed: `test_from_prefers_controller_label_over_shared_adapter` (`codex` vs `probe-ctl`). PID-without-label passes. |

So: leftover omit is pinned by `test_cli_post_under_dispatch_id[leftover-worker-id]`;
primitive / steering / quota stamps by their new tests; inventing fallback
by `test_pid_without_label[None]` (the leftover parametrize of that test
already forced `UNKNOWN` on `ffd22bc`); adapter precedence only by
`test_from_prefers_controller_label_over_shared_adapter`.

---

## P2 — `from` is still a self-asserted name; digest does not authenticate it

**Anchors:** `_stamp_controller_source_label` `:1961-1977` (TRUSTED policy);
`envelope_authored_by_controller` `:4174-4181` (label discarded; digest vs
viewer's nonce); `relay --new` `:4568-4573` + `format_envelope_headlines`
`:4339`; CLI mint `:2527-2529`.

**Failure:** a caller who sets `source.controller_label` (MCP arguments, or
`GOALFLIGHT_CONTROLLER_LABEL` while holding some other lease) is printed as
that sender. `author_digest` is checked to hide the viewer's own posts on
`relay --new` / follow / listen when a capability was actually presented.
It is not shown, not required, and not bound to the name. Most production
posts in this probe carried no digest.

This matches the authors' stated design. It is also the incident's failure
mode with a string instead of an absent field. Lease-validating at stamp
time would be the wrong fix (their argument holds). A residual worth
keeping: the scannable `from` line is still believed.

---

## P3 — `relay --drain` findings still omit `from`

**Anchors:** `format_receipt_headline` `:4255-4268` vs
`format_envelope_headlines` `:4339` vs `format_patch_drain_head` `:4241-4248`.

**OBSERVED.** Drain of four findings:

```
[finding] trust-cap seq=1 — forged with cap
[finding] trust-forged seq=1 — trusted stamp
...
```

No `from `. Patch drain still includes `from {who}`. Round 1 P3, not
claimed fixed in round 2, still true. A controller who only `--drain`s
findings does not see attribution on that line.

---

## P3 — leftover `GOALFLIGHT_DISPATCH_ID` + informative adapter hides the sentinel

**Anchors:** `envelope_from` `:4147-4156`; CLI `--adapter` default `:7911`.

**OBSERVED.** Leftover dispatch id + `--adapter codex`: field is `UNKNOWN`,
relay prints `from codex`. Default adapter `unknown` still prints
`from UNKNOWN` (the incident command). Quota/fleet UNKNOWN→adapter is
intentional. The same rule lets a leftover worker CLI that passes
`--adapter` look like a named host rather than unestablishable.

---

## What is actually closed

- Documented controller-shell `post` (default adapter), with and without
  leftover `GOALFLIGHT_DISPATCH_ID`: field present, `UNKNOWN` when
  unestablishable, survives carrier + journal delivery + `relay --new` /
  `--bodies`.
- MCP default and MCP-under-dispatch-id: same.
- Library `post_message`: same, including the round-1 open hole.
- Steering and quota: field present on the carrier they actually write.
- PID/repo heuristic is not a sender.
- Real controller_label outranks shared adapter names; genuine
  `transport != controller` adapter mail still names the adapter.
- Legacy unlabeled controller rows: `from UNKNOWN`, no crash.

## What is not

- `from` is not authenticated identity. Digest is a self-filter, often
  absent.
- Drain receipts for findings still omit `from`.
- UNKNOWN as a literal controller name collides with the sentinel at the
  from-line.
- Leftover dispatch + `--adapter <host>` prints the host, not UNKNOWN.
- Steering and quota never hit `relay --new`; their "round-trip" tests do
  not exercise that seam.
