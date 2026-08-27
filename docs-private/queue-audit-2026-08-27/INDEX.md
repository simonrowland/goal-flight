# Dispatch-queue inventory — 2026-08-27

> **Nothing in the shared queue has been deleted, moved, renamed, or re-fired.**
> This file is an inventory so *you* can **claim or abandon** each entry.
> If the work is still wanted, re-submit it as a **fresh dispatch under a new id
> against current HEAD**. Re-firing a stale queue record is what we are avoiding:
> a queued dispatch carries a premise, and premises go stale (wrong tree, wrong
> branch, vanished prompt, already-finished work).
>
> Ledger `state` values below are the raw on-disk field. They are **not** a
> `reconcile-abandoned` verdict (that gate is under repair in t-371). Claim-pid
> liveness is `kill(pid, 0)` on the filename pid only — a live pid can still be
> a reuse; a dead pid is not a license to drain the same record.


Generated at `2026-08-27T14:54:06.319466+00:00` (UTC). Queue path: `/tmp/goal-flight-501/dispatch-queue`. Ledger path: `/tmp/goal-flight-501/runs.d` (read as JSON files; **no** `StateLock`, **no** `open_reader`, **no** `reconcile-abandoned`).

The queue lives in `/tmp` (macOS may reap it). A byte-for-byte copy of the queue tree, plus every non-terminal ledger record, is under `_raw-snapshot/`.

## Why two controllers disagreed (28 vs 63)

They counted **different populations**. There is no single 'queue depth'. Quote the population with every number:

| Population | Pass-1 snapshot | Pass-2 snapshot | Live during catalog | What this is |
|---|---:|---:|---:|---|
| bare `<id>.json` at queue top | **29** | **28** | **28** | Drainable records. `b285-ring-coherence-adjudication.json` left the live dir during this inventory; it is preserved in pass-1 and in the ledger. |
| `<id>.json.claimed-<pid>-<ts>` at queue top | **26** | **27** | **26** | Claim markers. t-369 saw 25/25 dead; we saw 26 on pass-1 (all dead), then 2 live pids appeared and later left. |
| claimed-only (marker, no surviving bare `.json`) | **11** | **12** | **11** | Unrecoverable by drain. Matches t-369's 11. |
| bare-only (json, no claim marker) | **14** | **13** | **13** | Sitting unclaimed. |
| both bare and claim | **15** | **15** | **15** | Claimed copies of a still-present json. |
| `quarantine/` files | **2 / 2** | **2 / 2** | **2 / 2** | Claim markers moved into quarantine (pids dead). |
| `retired-by-bugs/` files | **12 / 12** | **12 / 12** | **12 / 12** | All bare json; unique ids 12. |
| `retired-by-engine/` files | **3 / 3** | **3 / 3** | **3 / 3** | All three ids also still exist in the live queue. |
| `retired-by-main/` files | **6 / 6** | **6 / 6** | **6 / 6** | 5 bare + 1 claim; 2 of the bare ids also still live. |
| `retired-by-webui/` files | **3 / 3** | **3 / 3** | **3 / 3** | All bare json. |
| retired unique dispatch ids | **23** | **23** | **23** | 12+3+5+3 unique across the four dirs (`fuzzspoke` has both bare and claim). |
| other top-level | 1 (`.submit.lock`) | 1 | 1 | Not a dispatch. |
| dispatch ledger records | **1681 / 1681** | — | — | Every `runs.d/*.json`. |
| ledger non-terminal (raw `state` ∉ `TERMINAL_STATES`) | **48 / 1681** | — | — | 23 `queued` + 18 `running` + 4 `starting` + 2 `waiting_capacity` + 1 `launch_unconfirmed`. |
| ledger non-terminal ∩ live queue id | **28 / 48** | — | — | Same id in both places. |
| ledger non-terminal with **no** live queue file | **20 / 48** | — | — | Includes this worker (`t372-w1`) and six `acp-*` probe leftovers. |
| **union of unique dispatch ids in this audit** | **81** | — | — | Queue files ∪ retired ∪ quarantine ∪ ledger-nonterminal. |

A count of **28** is the bare-json drainable set (29 on pass-1, 28 after `b285` left). A count of **63** is what you get by adding pass-1's 29 bare + 26 claims + 2 quarantine + 6 `retired-by-main` files = **63**, mixing live and retired populations. Neither number is wrong; they are different sets.

### Overlaps (do not double-count these as extra work)

- **retired ∩ live queue:** `codex-56849-1787443719-retry-63523187`, `codex-80486-1787414845-retry-d69b4bc9`, `magemin-status-review`, `status-r2-notfixed`, `t697-dunite`.
- **quarantine ∩ live queue:** none. **quarantine ∩ retired:** none.
- **claimed-only (no bare json, drain cannot recover):** pass-1 list: `codex-20873-1787795343`, `codex-35329-1787794960`, `codex-38318-1787793995`, `codex-45427-1787794032`, `codex-62503-1787807410`, `codex-74265-1787791956`, `grok-code-28489-1787781619`, `grok-code-58307-1787790827`, `layermap-harvest-d`, `sr2-closure`, `t800-pulse`.
- **live queue with no ledger record at all:** `codex-356-1787583341-retry-03e74ab8`, `codex-38168-1787588171-retry-ae50e9f1`, `codex-56849-1787443719-retry-63523187`, `codex-73102-1787700838-retry-c2f707cf`, `codex-80486-1787414845-retry-d69b4bc9`, `fr-d1-r3-retry-b8fa0aba`, `t746-r2-retry-15cc828e`.

## Method

1. Copied `/tmp/goal-flight-501/dispatch-queue` to `_raw-snapshot/queue-dir-copy` **before** analysis (pass-1), then recopied (pass-2) to catch drift. The live dir mutated during this inventory: `b285-ring-coherence-adjudication.json` disappeared; two claim markers with live pids appeared and later left.
2. Enumerated bare json, claim markers, `quarantine/`, and every `retired-by-*` directory separately.
3. Read every `runs.d/*.json` as text. Non-terminal means the record's `state` field is not in `goalflight_dispatch_states.TERMINAL_STATES`. That is a vocabulary filter, not a liveness verdict.
4. Claim-pid liveness: `os.kill(pid, 0)` on the pid parsed from the filename.
5. Prompt pin: `request.prompt_file` / `--prompt-file`, else inline `request.prompt` / `--prompt`, else ledger `prompt_path`. Full text copied to `prompts/<dispatch_id>.md`. A missing file is recorded as missing — that is decision-relevant.
6. Hints (already-done, missing cwd, terminal ledger state) are **hints**, not adjudications.
7. Queue and ledger were not written, renamed, drained, or reconciled.

## Prompt pinning

- **70 / 81** prompts pinnable (file still on disk, or inline text present).
- **11 / 81** premises missing (prompt-file gone or never stored). Honest default for those: abandon.
- Source mix: `{'prompt-file-missing': 10, 'prompt-file': 65, 'inline': 5, 'missing': 1}`.

## Per-project summary

| Project | Entries (union) | Live queue-top | Retired/quarantine only | Ledger-nonterm only | Pinnable | Missing premise | File |
|---|---:|---:|---:|---:|---:|---:|---|
| battery-tool-v2 | 46 / 81 | 21 / 46 | 17 / 46 | 8 / 46 | 43 / 46 | 3 / 46 | [`battery-tool-v2.md`](battery-tool-v2.md) |
| pm2 | 16 / 81 | 11 / 16 | 1 / 16 | 4 / 16 | 14 / 16 | 2 / 16 | [`pm2.md`](pm2.md) |
| regolith-pyrolysis-simulator | 11 / 81 | 9 / 11 | 2 / 11 | 0 / 11 | 11 / 11 | 0 / 11 | [`regolith.md`](regolith.md) |
| goal-flight | 8 / 81 | 0 / 8 | 0 / 8 | 8 / 8 | 2 / 8 | 6 / 8 | [`goal-flight.md`](goal-flight.md) |

## Ledger non-terminal state histogram (raw)

| state | count | in TERMINAL_STATES? |
|---|---:|---|
| `complete` | 1162 / 1681 | yes (excluded from non-terminal population) |
| `worker_dead` | 161 / 1681 | yes (excluded from non-terminal population) |
| `quota_exhausted` | 99 / 1681 | yes (excluded from non-terminal population) |
| `blocked` | 78 / 1681 | yes (excluded from non-terminal population) |
| `inconclusive_no_final` | 46 / 1681 | yes (excluded from non-terminal population) |
| `failed` | 37 / 1681 | yes (excluded from non-terminal population) |
| `queued` | 23 / 1681 | no (counted as non-terminal) |
| `running` | 20 / 1681 | no (counted as non-terminal) |
| `superseded` | 18 / 1681 | yes (excluded from non-terminal population) |
| `blocked_os_sandbox` | 14 / 1681 | yes (excluded from non-terminal population) |
| `orphaned` | 9 / 1681 | yes (excluded from non-terminal population) |
| `idle_timeout` | 6 / 1681 | yes (excluded from non-terminal population) |
| `starting` | 3 / 1681 | no (counted as non-terminal) |
| `blocked_capacity` | 2 / 1681 | yes (excluded from non-terminal population) |
| `launch_unconfirmed` | 1 / 1681 | no (counted as non-terminal) |
| `transient_throttle` | 1 / 1681 | yes (excluded from non-terminal population) |
| `waiting_capacity` | 1 / 1681 | no (counted as non-terminal) |

## Per-project files

- [`battery-tool-v2.md`](battery-tool-v2.md) — 46 entries
- [`pm2.md`](pm2.md) — 16 entries
- [`regolith.md`](regolith.md) — 11 entries
- [`goal-flight.md`](goal-flight.md) — 8 entries

Pinned prompts live in [`prompts/`](prompts/).

