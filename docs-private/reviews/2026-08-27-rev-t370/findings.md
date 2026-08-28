# rev-t370 — attribute controller-transport mail at post time

Review of `c87d0e7` on branch `t370-mail-attrib`. Review only; source not edited.
Verified in isolated `mktemp` trees (`GOALFLIGHT_JOURNAL_DIR`,
`GOALFLIGHT_STATE_DIR`, `GOALFLIGHT_WAKE_LEDGER`, `GOALFLIGHT_MESSAGES_DIR`,
`GOALFLIGHT_TASK_STORE`/`GOALFLIGHT_TASK_STORE_DIR`, `GOALFLIGHT_PIDFILE_DIR`,
`GOAL_FLIGHT_PIDFILE_DIR`, `GOALFLIGHT_CAPACITY_CONF=/dev/null`). No live
journals were posted to.

P0: 0 · P1: 2 · P2: 3 · P3: 1 · verdict **FIX**

The documented controller-shell `post` and the MCP tool do stamp
`source.controller_label` (or `UNKNOWN`) and `relay --new` renders that for
`adapter=unknown` controller mail. That is not every posting path, and it is
not the whole reader seam.

---

## Posting-path census (attack 1)

`post_message` (`scripts/goalflight_messages.py:1285`) is the shared admit
path. Its default source is the incident shape and it does **not** stamp:

```
1318:    base_source = {
1319:        "node": "local",
1320:        "adapter": "unknown",
1321:        "transport": "controller",
1322:    }
```

Stamping lives only at two ingresses: CLI `cmd_post` and MCP
`goalflight_post_message_tool`. `_stamp_controller_source_label`
(`scripts/goalflight_messages.py:1958`) is not called from the primitive.

| Path | Location | transport | Stamp? | Verdict |
|---|---|---|---|---|
| CLI `cmd_post`, no `GOALFLIGHT_DISPATCH_ID` | `scripts/goalflight_messages.py:2487` | default `controller` (`:7881`) | yes, via `_stamp_controller_source_label` at `:2508` | **CLOSED** |
| CLI `cmd_post`, `GOALFLIGHT_DISPATCH_ID` set | `:2507-2509` skips the stamp entirely | default `controller` | **no** — field omitted | **OPEN** (incident-shaped) |
| MCP `goalflight_post_message_tool` | `:2046-2074` | defaults to `controller` if omitted | yes; worker id → `UNKNOWN` | **CLOSED** |
| Library `post_message` | `:1285` | default `controller` | no | **OPEN** (legacy-shaped; used by tests on purpose) |
| `write_steering_envelope` | `:4898` / source at `:4918` | `controller` | no (bypasses `post_message`) | **OPEN** (adapter `fleet`) |
| `goalflight_status._post_quota_advisories` | `scripts/goalflight_status.py:2544` / source `:2566` | `controller` | no | **OPEN** (adapter `goalflight_status`) |
| `post_controller_steer` | `scripts/goalflight_messages.py:2014` | `steer` | n/a (not controller transport) | **N/A** |
| `goalflight_watch.post_trace_attention` | `scripts/goalflight_watch.py:932` / `:958` | `trace-liveness` | n/a | **N/A** |
| `goalflight_watch.post_worker_wait_attention` | `:1090` / `:1133` | `steer-wait` | n/a | **N/A** |
| `goalflight_journal.project_terminal_outbox` | `scripts/goalflight_journal.py:4868` / `:4873` | `journal` | n/a | **N/A** |
| `goalflight_task._post_task_store_nudge` | `goalflight_task.py:4732` / `:4744` | `next-frontier` / `done-suggest` / `resume-frontier` | n/a | **N/A** |
| `from-text` / `markers_to_envelopes` | `scripts/goalflight_messages.py:2468`, `:2105` | default `tail_file` | does not post | **N/A** |
| `merge_remote_register` / `cmd_mirror` | `:4939` / `:7823` | copies stored envelopes | preserves whatever was stored | **passthrough** |

The pre-change CLI already set `controller_label` from
`GOALFLIGHT_CONTROLLER_LABEL` when that env was present (the `:2454` behaviour
the brief named). This commit replaced that with `_stamp_controller_source_label`
and added MCP + `UNKNOWN`. It did not move the stamp into `post_message`.

---

## P1 — CLI `post` under `GOALFLIGHT_DISPATCH_ID` still emits the incident record

**Anchors:** `scripts/goalflight_messages.py:2507-2509` (skip), contrast
`:1958-1965` (stamp would have written `UNKNOWN`) and MCP `:2071-2074`
(always stamps).

**Failure:** absent field, not `UNKNOWN`. Same stored shape as the incident:
`{node: local, adapter: unknown, transport: controller}` with no
`controller_label`.

**OBSERVED** (isolated probe, documented `post --type finding --to-controller`,
no `--adapter`):

- Controller shell, `GOALFLIGHT_CONTROLLER_LABEL=probe-ctl`, no dispatch id:
  stored `controller_label=probe-ctl`; `relay --new` →
  `probe-topic #1 [finding] from probe-ctl: …`
- Same CLI with `GOALFLIGHT_DISPATCH_ID=rev-t370-leftover`: stored source
  `{'node': 'local', 'adapter': 'unknown', 'transport': 'controller'}` —
  **`controller_label` absent**. Carrier
  `…/messages/worker-cli-topic.jsonl`.
- MCP with the same leftover dispatch id: stored
  `controller_label=UNKNOWN`.

`_stamp_controller_source_label` already treats a worker id as
unestablishable and would stamp `UNKNOWN` (`:1948-1949` then `:1965`).
`cmd_post` never calls it when `GOALFLIGHT_DISPATCH_ID` is set, so CLI and MCP
diverge.

The new tests document leftover dispatch id as a real ambient risk and **pop
it** (`tests/python/test_messages_headlines.py:43-47`, `:385-387`) rather than
pinning CLI `UNKNOWN` under it. That is how the suite stays green with
`GOALFLIGHT_DISPATCH_ID` set in the worker environment: the tests hide the
CLI hole instead of covering it.

Relay headlines of an unlabelled controller record do render `from UNKNOWN`
(see backward compat). Recipients who read `relay --bodies` / the JSON source
still see an absent field and can guess — that is the incident.

---

## P1 — unestablishable identity can still be invented as the git directory name

**Anchors:** `scripts/goalflight_session_status.py:121-141`
(`resolve_controller_label`: env label, else — if a controller PID is present —
`resolve_project_root(…).name`); `_controller_post_source_label`
(`scripts/goalflight_messages.py:1938`) feeds that into the stamp;
`envelope_from` (`:4124-4126`) then renders whatever was stamped.

Attack 3: where a label cannot be established, render `UNKNOWN`, never an
invented sender.

**OBSERVED:** `GOALFLIGHT_CONTROLLER_LABEL` dropped, `GOALFLIGHT_CONTROLLER_PID`
set, lease holder `probe-ctl`. `resolve_controller_label()` returned `"project"`
(the isolated git dir name). CLI stamped `controller_label=project`.
`envelope_from` returned `"project"`. That is not the lease the poster held
and is not `UNKNOWN`.

The new `test_unestablishable_sender_is_stamped_and_rendered_unknown` strips
**all** `GOALFLIGHT_CONTROLLER*` keys, including PID, so it only covers the
all-vars-dropped case. The PID-kept / LABEL-dropped case — the one
`resolve_controller_label` was adopted to cover (`CHANGELOG.md` / docstring at
`:1941-1946`) — invents a sender.

---

## P2 — other controller-transport producers still omit the field

Stamp is not in `post_message`, so every other production caller of the
primitive (or a bypass) still writes controller-transport mail without
`controller_label`.

**OBSERVED:**

- `_post_quota_advisories` real call wrote
  `controller-quota-advisory.jsonl` with
  `source={node: local, adapter: goalflight_status, transport: controller}`
  and no label. `envelope_from` → `goalflight_status`.
- `write_steering_envelope` source
  `{node: local, adapter: fleet, transport: controller}` (`:4918`).
  `envelope_from` → `fleet`.
- Direct `post_message` with the pre-fix CLI source dict: no label (this is
  also how the backward-compat test plants a legacy row).

These are not the incident adapter=`unknown` shape, because an informative
adapter wins at the reader seam. The field is still absent. Protocol text
(`protocols/controller-mail.md:73-76`) claims every controller-transport post
is attributed; these paths are not.

---

## P2 — reader seam prefers `adapter` over `controller_label`

**Anchor:** `scripts/goalflight_messages.py:4121-4123` then `:4124-4126`.

**OBSERVED:** CLI `post --adapter codex` (otherwise the documented send)
stored `controller_label=probe-ctl` but `envelope_from` / `relay --new`
rendered `from codex`. Host name is not a controller identity; nine
controllers can share `codex`.

The documented send omits `--adapter` (default `unknown` at `:7879`), so the
incident command is not this path. Any producer that sets a non-`unknown`
adapter hides the new field in headlines.

---

## P2 — the label is trusted, not validated against the lease

**Anchors:**

- `_stamp_controller_source_label` `:1962-1963` returns immediately if a
  caller already supplied a non-blank `controller_label`.
- `resolve_controller_label` reads `GOALFLIGHT_CONTROLLER_LABEL` or the repo
  name; it does not consult `Journal.active_lease`.
- `envelope_authored_by_controller` `:4135-4144` explicitly: labels address
  mail; they never prove who posted it. Proof is `author_digest` from a
  presented capability (`:1339-1344`, `:1923-1935`).

**OBSERVED:**

- MCP `source.controller_label=NOT-THE-LEASE-HOLDER` accepted; `envelope_from`
  → `NOT-THE-LEASE-HOLDER`.
- CLI `GOALFLIGHT_CONTROLLER_LABEL=impostor-controller` while the active lease
  is `probe-ctl`: stamped and rendered `impostor-controller`.

This matches the stated design (descriptive metadata). It is also the
incident's failure mode with the tooling's `from` line behind the wrong name.
Non-string junk is refused (`validate_envelope` `:663-668`); a plausible
string is not.

---

## P3 — drain receipts omit the sender except for patch types

**Anchors:** `format_envelope_headlines` `:4296-4309` uses `envelope_from`
(`relay --new`). `format_receipt_headline` `:4225-4238` does not.
`format_patch_drain_head` `:4211-4217` does.

**OBSERVED:** `format_receipt_headline` for a finding has no `from `;
`format_patch_drain_head` includes `from {who}`. A controller that only
`relay --drain`s findings does not see the new attribution on that line.

---

## Attack 2 — normalization / relay copy: closed

**HYPOTHESISED in the brief:** a source-field copy iterating
`("node", "adapter", "transport")` drops `controller_label` in transit.

**OBSERVED:** that tuple at `scripts/goalflight_messages.py:654` is a
required-key check inside `validate_envelope`, not a rebuild. Canonical
serialization (`_canonical_json_text` `:590`, used by `serialize_envelope_line`
`:1222`) dumps the whole envelope. Extra source keys survive.

- Probe `serialize_envelope_line` keys:
  `adapter, controller_label, node, transport`.
- `test_labelled_controller_post_round_trips_label_to_relay` posts via the
  documented CLI, then `relay --bodies` and `relay --new` through production
  journal delivery (`_listener_envelope` `:2986` returns the carrier envelope
  as stored, not a reconstructed 3-key source).
- Isolated probe: documented post → `relay --new` `from probe-ctl`.

Journal assignment stores `origin_node` only (`:1603-1641`) as the delivery
key; the envelope bytes stay on the JSONL carrier.

---

## Attack 3 — UNKNOWN at the reader seam: yes for `relay --new`

**OBSERVED:**

- Legacy / unlabelled controller mail: `envelope_from` → `UNKNOWN`
  (`:4127-4128`). `test_preexisting_unlabelled_record_renders_unknown_without_crashing`
  and `test_from_renders_unknown_for_unattributed_controller_mail` pin this.
  Isolated probe: same function returned `"UNKNOWN"` for the incident source
  dict.
- Unestablishable CLI (all `GOALFLIGHT_CONTROLLER*` dropped): stored
  `controller_label=UNKNOWN`; `relay --new` `from UNKNOWN`.
- Non-controller transport still falls through to node (`worker-box` in the
  unit test).

Caveats above: drain line, adapter preference, invented repo-name stamp.

---

## Attack 5 — backward compatibility: closed at `relay --new`

**OBSERVED:** `post_message` with the pre-fix CLI source dict writes a record
with no `controller_label` (`test_preexisting_…` asserts that). `relay --new`
exits 0 and prints `legacy-topic #1 [finding] from UNKNOWN:`. No crash on the
optional field (`validate_envelope` `:663` only constrains the value when
present).

---

## Attack 6 — reverts (each new test fails on the behaviour it pins)

Source restored after; `scripts/goalflight_messages.py` hash unchanged.

| Revert | test_from_renders_unknown | test_labelled | test_unestablishable | test_preexisting | test_mcp |
|---|---|---|---|---|---|
| `envelope_from` → pre-change (adapter/node, no label/`UNKNOWN`) | FAIL (`UNKNOWN` vs `local`) | FAIL (relay `from headlines`) | FAIL (relay `from UNKNOWN`) | FAIL (relay `from UNKNOWN`) | FAIL (relay `from headlines`) |
| CLI stamp removed entirely | pass | FAIL `KeyError controller_label` | FAIL `KeyError` | pass | pass |
| CLI stamp → old env-var only | pass | pass (env still set) | FAIL `KeyError` (no `UNKNOWN`) | pass | pass |
| MCP stamp removed | pass | pass | pass | pass | FAIL `KeyError controller_label` |

So:

- Reader `UNKNOWN` is pinned by `test_from_renders_unknown`,
  `test_preexisting`, and the relay assertions on labelled / unestablishable /
  MCP.
- CLI `UNKNOWN` when identity is missing is pinned only by
  `test_unestablishable` (and is **not** pinned under leftover
  `GOALFLIGHT_DISPATCH_ID`).
- MCP stamp is pinned by `test_mcp_ingress_stamps_the_same_attribution`.
- `test_labelled` does **not** uniquely pin `resolve_controller_label` vs the
  old env-var stamp; with `GOALFLIGHT_CONTROLLER_LABEL` set, the pre-change CLI
  already stored the field. Its new pin is relay rendering the label instead
  of `from local`.

Current HEAD: `python3 tests/python/test_messages_headlines.py` — 18 PASS
under isolated env.

---

## What is actually closed

- Documented controller-shell `post` (no leftover dispatch id, default
  adapter): field stamped, survives canonical JSON + journal delivery +
  `relay --new` / `--bodies`.
- MCP default and MCP-under-dispatch-id: stamped (`label` or `UNKNOWN`).
- Unlabelled historical rows: `relay --new` says `from UNKNOWN`, does not
  crash.
- Normalization does not drop the extra source key.

## What is not

- CLI post with `GOALFLIGHT_DISPATCH_ID` still writes the incident record.
- `post_message` / quota / steering still omit the field on controller
  transport.
- A supplied or resolved label is not checked against the lease the poster
  holds; `from` will print a lie.
- `resolve_controller_label`'s PID/repo-name fallback invents a sender.
- `relay --drain` findings do not show `from`.
