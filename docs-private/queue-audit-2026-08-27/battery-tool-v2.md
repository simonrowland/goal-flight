# Queue inventory for **battery-tool-v2** — 2026-08-27

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


This list is for the battery-tool-v2 controllers (main, engine, bugs, webui). Several entries target `.cache/worktrees/*` or `/tmp/goal-flight-501/audit-*` sandboxes — those are not the main tree. The twelve `retired-by-bugs` rows are the b-2130 audit slices; the three `retired-by-webui` rows were already set aside by that controller.

See [INDEX.md](INDEX.md) for population counts and method. This file lists **46 / 81** union dispatch ids whose `project_root` (or cwd) belongs to this project.

Prompts: **43 / 46** pinnable, **3 / 46** missing.

## Quick list

| dispatch_id | age | owner | agent | claim pid | prompt | TLDR |
|---|---|---|---|---|---|---|
| [`codex-20873-1787795343`](#codex-20873-1787795343) | 13h 5m | `battery-engine` | `codex` | pid `20873` **dead** | pinned | b-2710 — write the end-to-end seam test that three 'verified' fixes slipped through; do not fix the bug. Target: worktree `bt-b2080`. Claim-only. |
| [`codex-21996-1787791668`](#codex-21996-1787791668) | 14h 6m | `None` | `codex` | pid `44163` **dead** | pinned | b-2690 — split the CLEAN half of a contaminated commit onto its own branch. Target: worktree `bt-warm`. |
| [`codex-320-1787529559`](#codex-320-1787529559) | 3d 14h | `None` | `codex` | none | pinned | b-2043 — why the current tree tops out at 2–3 MP on BBL 4014760001 (do not bisect history). Target: battery-tool-v2 main. Retired-by-main. |
| [`codex-35329-1787794960`](#codex-35329-1787794960) | 13h 11m | `battery-engine` | `codex` | pid `4701` **dead** | pinned | b-2710 seam test (same brief as codex-20873) but cwd is battery-tool-v2 **main**, not `bt-b2080`. Claim-only. Same premise, different tree — do not re-fire blindly. |
| [`codex-356-1787583341-retry-03e74ab8`](#codex-356-1787583341-retry-03e74ab8) | 2d 21h | `battery-bugs` | `codex` | none | pinned | Retry of b-2181 / t-556: bearing-topology mechanism on the FLAGSHIP. Target: battery-tool-v2 main. Prompt file `B-b2181.md`. No ledger record. |
| [`codex-36951-1787795932`](#codex-36951-1787795932) | 12h 55m | `None` | `codex` | pid `25513` **dead** | pinned | b-2710 seam test against battery-tool-v2 **main** (third copy of the same brief). Target: main tree, not a worktree. |
| [`codex-38168-1787588171-retry-ae50e9f1`](#codex-38168-1787588171-retry-ae50e9f1) | 2d 21h | `battery-bugs` | `codex` | none | pinned | Retry of t-558: layout_count ~ n_cpu / per-(layout x family) seed prep on parallel CPUs. Target: battery-tool-v2 main. No ledger record. |
| [`codex-38318-1787793995`](#codex-38318-1787793995) | 13h 27m | `battery-bugs` | `codex` | pid `31779` **dead** | pinned | b-2702 gap 2 — IFC export silently discards the BESS equipment. Target: worktree `bt-b2702` which was MISSING on disk at inventory. Claim-only. |
| [`codex-43969-1787541709`](#codex-43969-1787541709) | 3d 11h | `None` | `codex` | none | pinned | b-2130 audit slice 0001 (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-0001` — a tmp sandbox, not battery-tool-v2 main. |
| [`codex-44655-1787541714`](#codex-44655-1787541714) | 3d 11h | `None` | `codex` | none | pinned | b-2130 audit slice 0401 (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-0401`. |
| [`codex-45162-1787541720`](#codex-45162-1787541720) | 3d 11h | `None` | `codex` | none | pinned | b-2130 audit slice 0801 (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-0801`. |
| [`codex-45427-1787794032`](#codex-45427-1787794032) | 13h 26m | `battery-engine` | `codex` | pid `45427` **dead** | pinned | Put test coverage on MEMBER_TRUTH_CONNECTION_ID_REQUIRED (export-mission guard). Target: worktree `bt-warm`. Claim-only. |
| [`codex-45667-1787541726`](#codex-45667-1787541726) | 3d 11h | `None` | `codex` | none | pinned | b-2130 audit slice 1201 (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-1201`. |
| [`codex-46279-1787541732`](#codex-46279-1787541732) | 3d 11h | `None` | `codex` | none | pinned | b-2130 audit slice 1501 (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-1501`. |
| [`codex-46804-1787541738`](#codex-46804-1787541738) | 3d 11h | `None` | `codex` | none | pinned | b-2130 audit slice 1801 (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-1801`. |
| [`codex-48669-1787557501`](#codex-48669-1787557501) | 3d 7h | `None` | `codex` | none | pinned | b-2130 audit slice 0001, second wave (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-0001`. |
| [`codex-49282-1787557506`](#codex-49282-1787557506) | 3d 7h | `None` | `codex` | none | pinned | b-2130 audit slice 0401, second wave (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-0401`. |
| [`codex-50159-1787557511`](#codex-50159-1787557511) | 3d 7h | `None` | `codex` | none | pinned | b-2130 audit slice 0801, second wave (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-0801`. |
| [`codex-52033-1787557524`](#codex-52033-1787557524) | 3d 7h | `None` | `codex` | none | pinned | b-2130 audit slice 1201, second wave (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-1201`. |
| [`codex-53248-1787557530`](#codex-53248-1787557530) | 3d 7h | `None` | `codex` | none | pinned | b-2130 audit slice 1501, second wave (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-1501`. |
| [`codex-53956-1787557536`](#codex-53956-1787557536) | 3d 7h | `None` | `codex` | none | pinned | b-2130 audit slice 1801, second wave (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-1801`. |
| [`codex-55395-1787799489`](#codex-55395-1787799489) | 11h 55m | `battery-bugs` | `codex` | none | pinned | b-2714 — the lint that enforces infeasible-is-an-owner-verdict is itself red. Ledger-only; worker pid dead. Target: battery-tool-v2. |
| [`codex-56849-1787443719-retry-63523187`](#codex-56849-1787443719-retry-63523187) | 3d 9h | `battery-main` | `codex` | none | **MISSING** | Retry of b-1977. Prompt file `b-1977.md` is MISSING. Present both in the live queue and in retired-by-main. Honest default: abandon. |
| [`codex-60942-1787841540`](#codex-60942-1787841540) | 15m | `battery-bugs` | `codex` | none | pinned | SC-150 class sweep — read-only diagnosis, do not fix. Ledger running with a live worker pid at inventory. Target: battery-tool-v2. |
| [`codex-62503-1787807410`](#codex-62503-1787807410) | 9h 43m | `battery-bugs` | `codex` | pid `53562` **dead** | pinned | b-2696 — IFC provenance guards are self-referential. Target: worktree `bt-b2696` which was MISSING on disk. Claim-only. |
| [`codex-62720-1787530305`](#codex-62720-1787530305) | 3d 14h | `None` | `codex` | none | pinned | b-2101 — publish SEED cards while until-dry is still running (plan-first). Retired-by-webui. Target: battery-tool-v2 main. |
| [`codex-63597-1787530311`](#codex-63597-1787530311) | 3d 14h | `None` | `codex` | none | pinned | t-554 / b-2139 follow-through: make e2e capacity runs site on the DECLARED roof. Retired-by-webui. Target: battery-tool-v2 main. |
| [`codex-64518-1787530317`](#codex-64518-1787530317) | 3d 14h | `None` | `codex` | none | pinned | t-555 backwards sweep (read-only): capacity/coverage comparisons lacking site-model identity. Retired-by-webui. Target: battery-tool-v2 main. |
| [`codex-69648-1787841595`](#codex-69648-1787841595) | 14m | `battery-bugs` | `codex` | none | pinned | SC-150 class sweep — read-only diagnosis, do not fix. Ledger running with a live worker pid at inventory. Target: battery-tool-v2. |
| [`codex-72685-1787777565`](#codex-72685-1787777565) | 18h 1m | `battery-main` | `codex` | none | pinned | Review-and-verify the UNVERIFIED q-042 salvage branch (read-only; do not modify the branch). Ledger-only; worker pid dead. |
| [`codex-73102-1787700838-retry-c2f707cf`](#codex-73102-1787700838-retry-c2f707cf) | 1d 14h 40m | `battery-webui` | `codex` | none | pinned | Retry of b-2484 (brief `b2482-brief.md`). Target: battery-tool-v2 main. No ledger record. |
| [`codex-73527-1787842274`](#codex-73527-1787842274) | 2m | `battery-engine` | `codex` | none | pinned | b-2759 — diagnose-then-fix: rail crossing called ORPHAN at 0.0057 inch from its top chord. Appeared mid-inventory with a live claim pid; ledger running. |
| [`codex-74265-1787791956`](#codex-74265-1787791956) | 14h 1m | `None` | `codex` | pid `27400` **dead** | pinned | b-2702 gaps 2 and 3 — put BESS equipment and keep-clear zones into the IFC. Target: worktree `bt-cat`. Claim-only. |
| [`codex-75378-1787688360`](#codex-75378-1787688360) | 1d 18h 48m | `None` | `codex` | none | pinned | b-2426 (brief `b2426-brief.md`). Target: worktree `bt-b2154` — both cwd and project_root were MISSING on disk at inventory. |
| [`codex-80486-1787414845-retry-d69b4bc9`](#codex-80486-1787414845-retry-d69b4bc9) | 3d 9h | `battery-bugs` | `codex` | none | **MISSING** | Retry of b-1940 (brief-sfring3). Prompt file MISSING. Present both in the live queue and in retired-by-main. Honest default: abandon. |
| [`codex-86585-1787546556`](#codex-86585-1787546556) | 3d 10h | `None` | `codex` | none | pinned | b-2148 — two defects in the Playwright acceptance bar itself (fix the bar, not the product). Retired-by-main. Target: battery-tool-v2 main. |
| [`grok-code-21722-1787778673`](#grok-code-21722-1787778673) | 17h 42m | `battery-main` | `grok-code` | none | pinned | b-2630 / b-2619 — reimport must invalidate/bypass the stale acquisition cache. Target: worktree `bt-b2630`. |
| [`grok-code-28489-1787781619`](#grok-code-28489-1787781619) | 16h 53m | `battery-main` | `grok-code` | pid `34323` **dead** | pinned | t-602 scout A (read-only): ground-site mode — acquisition/lot-polygon + import seam. Target: battery-tool-v2 main. Claim-only. |
| [`grok-code-34851-1787725369`](#grok-code-34851-1787725369) | 1d 8h 31m | `None` | `grok-code` | pid `27995` **dead** | pinned | Offer-review (`offer-review-brief.md`). Target: worktree `bt-d013` which was MISSING on disk. |
| [`grok-code-58307-1787790827`](#grok-code-58307-1787790827) | 14h 20m | `battery-main` | `grok-code` | pid `57031` **dead** | pinned | t-605 (G1 of ground-site mode t-602): get the lot polygon into the pipeline. Target: worktree `bt-t605`. Claim-only. |
| [`grok-code-70915-1787842258`](#grok-code-70915-1787842258) | 3m | `battery-bugs` | `grok-code` | pid `69178` **dead** | pinned | SC-150 class sweep (STORED-VERDICT.md) — read-only diagnosis. Target: worktree `bt-sc150-3`. Appeared mid-inventory with a live claim pid. |
| [`grok-code-77178-1787729728`](#grok-code-77178-1787729728) | 1d 7h 18m | `None` | `grok-code` | pid `84166` **dead** | pinned | Offer-review (`offer-review-brief.md`), second copy. Target: worktree `bt-d013` which was MISSING on disk. |
| [`grok-code-84519-1787841683`](#grok-code-84519-1787841683) | 12m | `battery-bugs` | `grok-code` | none | pinned | SC-150 class sweep — read-only diagnosis, do not fix. Ledger-only; worker pid dead. Target: battery-tool-v2. |
| [`raw-b2753-studio-e02ec9816-r2`](#raw-b2753-studio-e02ec9816-r2) | 32m | `None` | `codex` | pid `26932` **dead** | **MISSING** | Premise missing (no inline prompt, no prompt-file). Target cwd: worktree `bt-b2753`. Honest default: abandon. |
| [`zw-aw31`](#zw-aw31) | 1d 21h 35m | `battery-bugs` | `codex` | none | pinned | Awaiting-review batch 31: verify 14 already-closed rows against current release tip. Target: battery-tool-v2. Ledger `running` but worker pid was dead at inventory. |
| [`zw-fix_other_cross-layer__b1`](#zw-fix_other_cross-layer__b1) | 2d 18h | `battery-bugs` | `codex` | none | pinned | ZERO campaign fix wave 2, batch `fix_other_cross-layer__b1` (adapters/exporters). Target: worktree `bt-zw-fix_other_cross-layer__b1` DETACHED at origin/main 0bcbebe2a. Ledger `waiting_capacity`, age ~2d 18h. |

## Entries

### `codex-20873-1787795343`

- **TLDR:** b-2710 — write the end-to-end seam test that three 'verified' fixes slipped through; do not fix the bug. Target: worktree `bt-b2080`. Claim-only.
- **Pinned prompt:** [`prompts/codex-20873-1787795343.md`](prompts/codex-20873-1787795343.md)
- **Project name:** battery-tool-v2 (worktree bt-b2080)
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-b2080`
- **Queue payload state:** `claimed`
- **Ledger state (raw):** `starting`
- **Agent:** `codex`
- **Created at / age:** `2026-08-27T01:49:03+00:00` / **13h 5m**
- **Owner label:** `battery-engine`
- **Claim marker:** pid `20873` **dead**
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/c6fb7200-a974-44ea-87a1-0a95d1cfa471/scratchpad/seam-test-brief.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (20186 chars)
- **Task ids:** `[]`
- **Base SHA:** `3de431f458cc09ab6b4d859a29d78206b4948d73`
- **Populations:** `claim-marker`, `ledger-nonterminal`

### `codex-21996-1787791668`

- **TLDR:** b-2690 — split the CLEAN half of a contaminated commit onto its own branch. Target: worktree `bt-warm`.
- **Pinned prompt:** [`prompts/codex-21996-1787791668.md`](prompts/codex-21996-1787791668.md)
- **Project name:** battery-tool-v2 (worktree bt-warm)
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-warm`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `queued`
- **Agent:** `codex`
- **Created at / age:** `2026-08-27T00:47:48+00:00` / **14h 6m**
- **Owner label:** `None`
- **Claim marker:** pid `44163` **dead**
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/c6fb7200-a974-44ea-87a1-0a95d1cfa471/scratchpad/b2690-split-brief.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (19723 chars)
- **Task ids:** `[]`
- **Base SHA:** `3de431f458cc09ab6b4d859a29d78206b4948d73`
- **Populations:** `bare-json`, `claim-marker`, `ledger-nonterminal`, `queue-top`

### `codex-320-1787529559`

- **TLDR:** b-2043 — why the current tree tops out at 2–3 MP on BBL 4014760001 (do not bisect history). Target: battery-tool-v2 main. Retired-by-main.
- **Pinned prompt:** [`prompts/codex-320-1787529559.md`](prompts/codex-320-1787529559.md)
- **Project name:** battery-tool-v2
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `inconclusive_no_final`
- **Agent:** `codex`
- **Created at / age:** `2026-08-23T23:59:19+00:00` / **3d 14h**
- **Owner label:** `None`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/2ee708ed-cae0-44ff-be85-9cf833e8930f/scratchpad/briefs/mpcap.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (26933 chars)
- **Task ids:** `['b-2043']`
- **Base SHA:** `05d3ad9dbfc67f3660c502b084526b375a8d60dd`
- **Populations:** `ledger-terminal`, `retired-by-main`
- **Hints (not verdicts):** HINT: ledger already records terminal state `inconclusive_no_final` (not a queue-purge verdict)

### `codex-35329-1787794960`

- **TLDR:** b-2710 seam test (same brief as codex-20873) but cwd is battery-tool-v2 **main**, not `bt-b2080`. Claim-only. Same premise, different tree — do not re-fire blindly.
- **Pinned prompt:** [`prompts/codex-35329-1787794960.md`](prompts/codex-35329-1787794960.md)
- **Project name:** battery-tool-v2
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Queue payload state:** `claimed`
- **Ledger state (raw):** `queued`
- **Agent:** `codex`
- **Created at / age:** `2026-08-27T01:42:40+00:00` / **13h 11m**
- **Owner label:** `battery-engine`
- **Claim marker:** pid `4701` **dead**
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/c6fb7200-a974-44ea-87a1-0a95d1cfa471/scratchpad/seam-test-brief.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (20186 chars)
- **Task ids:** `[]`
- **Base SHA:** `None`
- **Populations:** `claim-marker`, `ledger-nonterminal`

### `codex-356-1787583341-retry-03e74ab8`

- **TLDR:** Retry of b-2181 / t-556: bearing-topology mechanism on the FLAGSHIP. Target: battery-tool-v2 main. Prompt file `B-b2181.md`. No ledger record.
- **Pinned prompt:** [`prompts/codex-356-1787583341-retry-03e74ab8.md`](prompts/codex-356-1787583341-retry-03e74ab8.md)
- **Project name:** battery-tool-v2
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Queue payload state:** `queued`
- **Ledger state (raw):** `None`
- **Agent:** `codex`
- **Created at / age:** `2026-08-24T17:03:23+00:00` / **2d 21h**
- **Owner label:** `battery-bugs`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/c6fb7200-a974-44ea-87a1-0a95d1cfa471/scratchpad/B-b2181.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (27412 chars)
- **Task ids:** `['t-556']`
- **Base SHA:** `94a983232f8982290e1d82988e9af4fde203e624`
- **Populations:** `bare-json`, `queue-top`

### `codex-36951-1787795932`

- **TLDR:** b-2710 seam test against battery-tool-v2 **main** (third copy of the same brief). Target: main tree, not a worktree.
- **Pinned prompt:** [`prompts/codex-36951-1787795932.md`](prompts/codex-36951-1787795932.md)
- **Project name:** battery-tool-v2
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `queued`
- **Agent:** `codex`
- **Created at / age:** `2026-08-27T01:58:52+00:00` / **12h 55m**
- **Owner label:** `None`
- **Claim marker:** pid `25513` **dead**
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/c6fb7200-a974-44ea-87a1-0a95d1cfa471/scratchpad/seam-test-brief.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (20186 chars)
- **Task ids:** `[]`
- **Base SHA:** `None`
- **Populations:** `bare-json`, `claim-marker`, `ledger-nonterminal`, `queue-top`

### `codex-38168-1787588171-retry-ae50e9f1`

- **TLDR:** Retry of t-558: layout_count ~ n_cpu / per-(layout x family) seed prep on parallel CPUs. Target: battery-tool-v2 main. No ledger record.
- **Pinned prompt:** [`prompts/codex-38168-1787588171-retry-ae50e9f1.md`](prompts/codex-38168-1787588171-retry-ae50e9f1.md)
- **Project name:** battery-tool-v2
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Queue payload state:** `queued`
- **Ledger state (raw):** `None`
- **Agent:** `codex`
- **Created at / age:** `2026-08-24T17:03:25+00:00` / **2d 21h**
- **Owner label:** `battery-bugs`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/c6fb7200-a974-44ea-87a1-0a95d1cfa471/scratchpad/B-t558.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (27309 chars)
- **Task ids:** `['t-556']`
- **Base SHA:** `94a983232f8982290e1d82988e9af4fde203e624`
- **Populations:** `bare-json`, `queue-top`

### `codex-38318-1787793995`

- **TLDR:** b-2702 gap 2 — IFC export silently discards the BESS equipment. Target: worktree `bt-b2702` which was MISSING on disk at inventory. Claim-only.
- **Pinned prompt:** [`prompts/codex-38318-1787793995.md`](prompts/codex-38318-1787793995.md)
- **Project name:** battery-tool-v2 (worktree bt-b2702)
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-b2702`
- **Queue payload state:** `claimed`
- **Ledger state (raw):** `starting`
- **Agent:** `codex`
- **Created at / age:** `2026-08-27T01:26:35+00:00` / **13h 27m**
- **Owner label:** `battery-bugs`
- **Claim marker:** pid `31779` **dead**
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/14146fbb-321f-44b3-8014-c2faa22b0e32/scratchpad/b2702-brief.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (16791 chars)
- **Task ids:** `[]`
- **Base SHA:** `3de431f458cc09ab6b4d859a29d78206b4948d73`
- **Populations:** `claim-marker`, `ledger-nonterminal`
- **Hints (not verdicts):** HINT: worker_cwd does not exist on disk: `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-b2702`<br>HINT: worker_cwd missing: `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-b2702`

### `codex-43969-1787541709`

- **TLDR:** b-2130 audit slice 0001 (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-0001` — a tmp sandbox, not battery-tool-v2 main.
- **Pinned prompt:** [`prompts/codex-43969-1787541709.md`](prompts/codex-43969-1787541709.md)
- **Project name:** audit-0001
- **Project root:** `/private/tmp/goal-flight-501/audit-0001`
- **Target cwd:** `/private/tmp/goal-flight-501/audit-0001`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `inconclusive_no_final`
- **Agent:** `codex`
- **Created at / age:** `2026-08-24T03:21:49+00:00` / **3d 11h**
- **Owner label:** `None`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/c6fb7200-a974-44ea-87a1-0a95d1cfa471/scratchpad/B-audit-0001.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (12821 chars)
- **Task ids:** `['b-2130']`
- **Base SHA:** `None`
- **Populations:** `ledger-terminal`, `retired-by-bugs`
- **Hints (not verdicts):** HINT: ledger already records terminal state `inconclusive_no_final` (not a queue-purge verdict)

### `codex-44655-1787541714`

- **TLDR:** b-2130 audit slice 0401 (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-0401`.
- **Pinned prompt:** [`prompts/codex-44655-1787541714.md`](prompts/codex-44655-1787541714.md)
- **Project name:** audit-0401
- **Project root:** `/private/tmp/goal-flight-501/audit-0401`
- **Target cwd:** `/private/tmp/goal-flight-501/audit-0401`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `inconclusive_no_final`
- **Agent:** `codex`
- **Created at / age:** `2026-08-24T03:21:54+00:00` / **3d 11h**
- **Owner label:** `None`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/c6fb7200-a974-44ea-87a1-0a95d1cfa471/scratchpad/B-audit-0401.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (12821 chars)
- **Task ids:** `['b-2130']`
- **Base SHA:** `None`
- **Populations:** `ledger-terminal`, `retired-by-bugs`
- **Hints (not verdicts):** HINT: ledger already records terminal state `inconclusive_no_final` (not a queue-purge verdict)

### `codex-45162-1787541720`

- **TLDR:** b-2130 audit slice 0801 (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-0801`.
- **Pinned prompt:** [`prompts/codex-45162-1787541720.md`](prompts/codex-45162-1787541720.md)
- **Project name:** audit-0801
- **Project root:** `/private/tmp/goal-flight-501/audit-0801`
- **Target cwd:** `/private/tmp/goal-flight-501/audit-0801`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `inconclusive_no_final`
- **Agent:** `codex`
- **Created at / age:** `2026-08-24T03:22:00+00:00` / **3d 11h**
- **Owner label:** `None`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/c6fb7200-a974-44ea-87a1-0a95d1cfa471/scratchpad/B-audit-0801.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (12821 chars)
- **Task ids:** `['b-2130']`
- **Base SHA:** `None`
- **Populations:** `ledger-terminal`, `retired-by-bugs`
- **Hints (not verdicts):** HINT: ledger already records terminal state `inconclusive_no_final` (not a queue-purge verdict)

### `codex-45427-1787794032`

- **TLDR:** Put test coverage on MEMBER_TRUTH_CONNECTION_ID_REQUIRED (export-mission guard). Target: worktree `bt-warm`. Claim-only.
- **Pinned prompt:** [`prompts/codex-45427-1787794032.md`](prompts/codex-45427-1787794032.md)
- **Project name:** battery-tool-v2 (worktree bt-warm)
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-warm`
- **Queue payload state:** `claimed`
- **Ledger state (raw):** `queued`
- **Agent:** `codex`
- **Created at / age:** `2026-08-27T01:27:12+00:00` / **13h 26m**
- **Owner label:** `battery-engine`
- **Claim marker:** pid `45427` **dead**
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/c6fb7200-a974-44ea-87a1-0a95d1cfa471/scratchpad/guard-test-brief.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (9175 chars)
- **Task ids:** `[]`
- **Base SHA:** `3de431f458cc09ab6b4d859a29d78206b4948d73`
- **Populations:** `claim-marker`, `ledger-nonterminal`

### `codex-45667-1787541726`

- **TLDR:** b-2130 audit slice 1201 (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-1201`.
- **Pinned prompt:** [`prompts/codex-45667-1787541726.md`](prompts/codex-45667-1787541726.md)
- **Project name:** audit-1201
- **Project root:** `/private/tmp/goal-flight-501/audit-1201`
- **Target cwd:** `/private/tmp/goal-flight-501/audit-1201`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `inconclusive_no_final`
- **Agent:** `codex`
- **Created at / age:** `2026-08-24T03:22:06+00:00` / **3d 11h**
- **Owner label:** `None`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/c6fb7200-a974-44ea-87a1-0a95d1cfa471/scratchpad/B-audit-1201.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (12821 chars)
- **Task ids:** `['b-2130']`
- **Base SHA:** `None`
- **Populations:** `ledger-terminal`, `retired-by-bugs`
- **Hints (not verdicts):** HINT: ledger already records terminal state `inconclusive_no_final` (not a queue-purge verdict)

### `codex-46279-1787541732`

- **TLDR:** b-2130 audit slice 1501 (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-1501`.
- **Pinned prompt:** [`prompts/codex-46279-1787541732.md`](prompts/codex-46279-1787541732.md)
- **Project name:** audit-1501
- **Project root:** `/private/tmp/goal-flight-501/audit-1501`
- **Target cwd:** `/private/tmp/goal-flight-501/audit-1501`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `inconclusive_no_final`
- **Agent:** `codex`
- **Created at / age:** `2026-08-24T03:22:12+00:00` / **3d 11h**
- **Owner label:** `None`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/c6fb7200-a974-44ea-87a1-0a95d1cfa471/scratchpad/B-audit-1501.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (12821 chars)
- **Task ids:** `['b-2130']`
- **Base SHA:** `None`
- **Populations:** `ledger-terminal`, `retired-by-bugs`
- **Hints (not verdicts):** HINT: ledger already records terminal state `inconclusive_no_final` (not a queue-purge verdict)

### `codex-46804-1787541738`

- **TLDR:** b-2130 audit slice 1801 (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-1801`.
- **Pinned prompt:** [`prompts/codex-46804-1787541738.md`](prompts/codex-46804-1787541738.md)
- **Project name:** audit-1801
- **Project root:** `/private/tmp/goal-flight-501/audit-1801`
- **Target cwd:** `/private/tmp/goal-flight-501/audit-1801`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `inconclusive_no_final`
- **Agent:** `codex`
- **Created at / age:** `2026-08-24T03:22:18+00:00` / **3d 11h**
- **Owner label:** `None`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/c6fb7200-a974-44ea-87a1-0a95d1cfa471/scratchpad/B-audit-1801.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (12821 chars)
- **Task ids:** `['b-2130']`
- **Base SHA:** `None`
- **Populations:** `ledger-terminal`, `retired-by-bugs`
- **Hints (not verdicts):** HINT: ledger already records terminal state `inconclusive_no_final` (not a queue-purge verdict)

### `codex-48669-1787557501`

- **TLDR:** b-2130 audit slice 0001, second wave (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-0001`.
- **Pinned prompt:** [`prompts/codex-48669-1787557501.md`](prompts/codex-48669-1787557501.md)
- **Project name:** audit-0001
- **Project root:** `/private/tmp/goal-flight-501/audit-0001`
- **Target cwd:** `/private/tmp/goal-flight-501/audit-0001`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `inconclusive_no_final`
- **Agent:** `codex`
- **Created at / age:** `2026-08-24T07:45:01+00:00` / **3d 7h**
- **Owner label:** `None`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/c6fb7200-a974-44ea-87a1-0a95d1cfa471/scratchpad/B-audit-0001.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (12821 chars)
- **Task ids:** `['b-2130']`
- **Base SHA:** `None`
- **Populations:** `ledger-terminal`, `retired-by-bugs`
- **Hints (not verdicts):** HINT: ledger already records terminal state `inconclusive_no_final` (not a queue-purge verdict)

### `codex-49282-1787557506`

- **TLDR:** b-2130 audit slice 0401, second wave (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-0401`.
- **Pinned prompt:** [`prompts/codex-49282-1787557506.md`](prompts/codex-49282-1787557506.md)
- **Project name:** audit-0401
- **Project root:** `/private/tmp/goal-flight-501/audit-0401`
- **Target cwd:** `/private/tmp/goal-flight-501/audit-0401`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `inconclusive_no_final`
- **Agent:** `codex`
- **Created at / age:** `2026-08-24T07:45:06+00:00` / **3d 7h**
- **Owner label:** `None`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/c6fb7200-a974-44ea-87a1-0a95d1cfa471/scratchpad/B-audit-0401.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (12821 chars)
- **Task ids:** `['b-2130']`
- **Base SHA:** `None`
- **Populations:** `ledger-terminal`, `retired-by-bugs`
- **Hints (not verdicts):** HINT: ledger already records terminal state `inconclusive_no_final` (not a queue-purge verdict)

### `codex-50159-1787557511`

- **TLDR:** b-2130 audit slice 0801, second wave (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-0801`.
- **Pinned prompt:** [`prompts/codex-50159-1787557511.md`](prompts/codex-50159-1787557511.md)
- **Project name:** audit-0801
- **Project root:** `/private/tmp/goal-flight-501/audit-0801`
- **Target cwd:** `/private/tmp/goal-flight-501/audit-0801`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `inconclusive_no_final`
- **Agent:** `codex`
- **Created at / age:** `2026-08-24T07:45:12+00:00` / **3d 7h**
- **Owner label:** `None`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/c6fb7200-a974-44ea-87a1-0a95d1cfa471/scratchpad/B-audit-0801.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (12821 chars)
- **Task ids:** `['b-2130']`
- **Base SHA:** `None`
- **Populations:** `ledger-terminal`, `retired-by-bugs`
- **Hints (not verdicts):** HINT: ledger already records terminal state `inconclusive_no_final` (not a queue-purge verdict)

### `codex-52033-1787557524`

- **TLDR:** b-2130 audit slice 1201, second wave (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-1201`.
- **Pinned prompt:** [`prompts/codex-52033-1787557524.md`](prompts/codex-52033-1787557524.md)
- **Project name:** audit-1201
- **Project root:** `/private/tmp/goal-flight-501/audit-1201`
- **Target cwd:** `/private/tmp/goal-flight-501/audit-1201`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `inconclusive_no_final`
- **Agent:** `codex`
- **Created at / age:** `2026-08-24T07:45:24+00:00` / **3d 7h**
- **Owner label:** `None`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/c6fb7200-a974-44ea-87a1-0a95d1cfa471/scratchpad/B-audit-1201.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (12821 chars)
- **Task ids:** `['b-2130']`
- **Base SHA:** `None`
- **Populations:** `ledger-terminal`, `retired-by-bugs`
- **Hints (not verdicts):** HINT: ledger already records terminal state `inconclusive_no_final` (not a queue-purge verdict)

### `codex-53248-1787557530`

- **TLDR:** b-2130 audit slice 1501, second wave (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-1501`.
- **Pinned prompt:** [`prompts/codex-53248-1787557530.md`](prompts/codex-53248-1787557530.md)
- **Project name:** audit-1501
- **Project root:** `/private/tmp/goal-flight-501/audit-1501`
- **Target cwd:** `/private/tmp/goal-flight-501/audit-1501`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `inconclusive_no_final`
- **Agent:** `codex`
- **Created at / age:** `2026-08-24T07:45:30+00:00` / **3d 7h**
- **Owner label:** `None`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/c6fb7200-a974-44ea-87a1-0a95d1cfa471/scratchpad/B-audit-1501.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (12821 chars)
- **Task ids:** `['b-2130']`
- **Base SHA:** `None`
- **Populations:** `ledger-terminal`, `retired-by-bugs`
- **Hints (not verdicts):** HINT: ledger already records terminal state `inconclusive_no_final` (not a queue-purge verdict)

### `codex-53956-1787557536`

- **TLDR:** b-2130 audit slice 1801, second wave (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-1801`.
- **Pinned prompt:** [`prompts/codex-53956-1787557536.md`](prompts/codex-53956-1787557536.md)
- **Project name:** audit-1801
- **Project root:** `/private/tmp/goal-flight-501/audit-1801`
- **Target cwd:** `/private/tmp/goal-flight-501/audit-1801`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `inconclusive_no_final`
- **Agent:** `codex`
- **Created at / age:** `2026-08-24T07:45:36+00:00` / **3d 7h**
- **Owner label:** `None`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/c6fb7200-a974-44ea-87a1-0a95d1cfa471/scratchpad/B-audit-1801.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (12821 chars)
- **Task ids:** `['b-2130']`
- **Base SHA:** `None`
- **Populations:** `ledger-terminal`, `retired-by-bugs`
- **Hints (not verdicts):** HINT: ledger already records terminal state `inconclusive_no_final` (not a queue-purge verdict)

### `codex-55395-1787799489`

- **TLDR:** b-2714 — the lint that enforces infeasible-is-an-owner-verdict is itself red. Ledger-only; worker pid dead. Target: battery-tool-v2.
- **Pinned prompt:** [`prompts/codex-55395-1787799489.md`](prompts/codex-55395-1787799489.md)
- **Project name:** battery-tool-v2
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `None`
- **Queue payload state:** `running`
- **Ledger state (raw):** `running`
- **Agent:** `codex`
- **Created at / age:** `2026-08-27T02:58:09+00:00` / **11h 55m**
- **Owner label:** `battery-bugs`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/tmp/goal-flight-501/dispatch/codex-55395-1787799489.assembled.prompt` — EXISTS on disk
- **Prompt pin:** **pinnable** (15784 chars)
- **Task ids:** `None`
- **Base SHA:** `None`
- **Populations:** `ledger-nonterminal`

### `codex-56849-1787443719-retry-63523187`

- **TLDR:** Retry of b-1977. Prompt file `b-1977.md` is MISSING. Present both in the live queue and in retired-by-main. Honest default: abandon.
- **Pinned prompt:** [`prompts/codex-56849-1787443719-retry-63523187.md`](prompts/codex-56849-1787443719-retry-63523187.md)
- **Project name:** battery-tool-v2
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Queue payload state:** `queued`
- **Ledger state (raw):** `None`
- **Agent:** `codex`
- **Created at / age:** `2026-08-24T05:41:41+00:00` / **3d 9h**
- **Owner label:** `battery-main`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/2ee708ed-cae0-44ff-be85-9cf833e8930f/scratchpad/briefs/b-1977.md` — MISSING on disk
- **Prompt pin:** **missing** (0 chars)
- **Task ids:** `['b-1977']`
- **Base SHA:** `b0e52f25d790e9ba3759f01e6e912c65cb3fada9`
- **Populations:** `bare-json`, `queue-top`, `retired-by-main`
- **Hints (not verdicts):** HINT: prompt-file is gone — premise cannot be re-checked; honest default is abandon

### `codex-60942-1787841540`

- **TLDR:** SC-150 class sweep — read-only diagnosis, do not fix. Ledger running with a live worker pid at inventory. Target: battery-tool-v2.
- **Pinned prompt:** [`prompts/codex-60942-1787841540.md`](prompts/codex-60942-1787841540.md)
- **Project name:** battery-tool-v2
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `None`
- **Queue payload state:** `running`
- **Ledger state (raw):** `running`
- **Agent:** `codex`
- **Created at / age:** `2026-08-27T14:39:00+00:00` / **15m**
- **Owner label:** `battery-bugs`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/tmp/goal-flight-501/dispatch/codex-60942-1787841540.assembled.prompt` — EXISTS on disk
- **Prompt pin:** **pinnable** (12331 chars)
- **Task ids:** `None`
- **Base SHA:** `None`
- **Populations:** `ledger-nonterminal`

### `codex-62503-1787807410`

- **TLDR:** b-2696 — IFC provenance guards are self-referential. Target: worktree `bt-b2696` which was MISSING on disk. Claim-only.
- **Pinned prompt:** [`prompts/codex-62503-1787807410.md`](prompts/codex-62503-1787807410.md)
- **Project name:** battery-tool-v2 (worktree bt-b2696)
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-b2696`
- **Queue payload state:** `claimed`
- **Ledger state (raw):** `queued`
- **Agent:** `codex`
- **Created at / age:** `2026-08-27T05:10:10+00:00` / **9h 43m**
- **Owner label:** `battery-bugs`
- **Claim marker:** pid `53562` **dead**
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/14146fbb-321f-44b3-8014-c2faa22b0e32/scratchpad/b2696-brief.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (15595 chars)
- **Task ids:** `[]`
- **Base SHA:** `3de431f458cc09ab6b4d859a29d78206b4948d73`
- **Populations:** `claim-marker`, `ledger-nonterminal`
- **Hints (not verdicts):** HINT: worker_cwd does not exist on disk: `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-b2696`<br>HINT: worker_cwd missing: `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-b2696`

### `codex-62720-1787530305`

- **TLDR:** b-2101 — publish SEED cards while until-dry is still running (plan-first). Retired-by-webui. Target: battery-tool-v2 main.
- **Pinned prompt:** [`prompts/codex-62720-1787530305.md`](prompts/codex-62720-1787530305.md)
- **Project name:** battery-tool-v2
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `inconclusive_no_final`
- **Agent:** `codex`
- **Created at / age:** `2026-08-24T00:11:45+00:00` / **3d 14h**
- **Owner label:** `None`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/tmp/claude-501/brief-b2101.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (7948 chars)
- **Task ids:** `['b-2101']`
- **Base SHA:** `05d3ad9dbfc67f3660c502b084526b375a8d60dd`
- **Populations:** `ledger-terminal`, `retired-by-webui`
- **Hints (not verdicts):** HINT: ledger already records terminal state `inconclusive_no_final` (not a queue-purge verdict)

### `codex-63597-1787530311`

- **TLDR:** t-554 / b-2139 follow-through: make e2e capacity runs site on the DECLARED roof. Retired-by-webui. Target: battery-tool-v2 main.
- **Pinned prompt:** [`prompts/codex-63597-1787530311.md`](prompts/codex-63597-1787530311.md)
- **Project name:** battery-tool-v2
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `inconclusive_no_final`
- **Agent:** `codex`
- **Created at / age:** `2026-08-24T00:11:51+00:00` / **3d 14h**
- **Owner label:** `None`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/tmp/claude-501/brief-declseed.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (7071 chars)
- **Task ids:** `['t-554']`
- **Base SHA:** `05d3ad9dbfc67f3660c502b084526b375a8d60dd`
- **Populations:** `ledger-terminal`, `retired-by-webui`
- **Hints (not verdicts):** HINT: ledger already records terminal state `inconclusive_no_final` (not a queue-purge verdict)

### `codex-64518-1787530317`

- **TLDR:** t-555 backwards sweep (read-only): capacity/coverage comparisons lacking site-model identity. Retired-by-webui. Target: battery-tool-v2 main.
- **Pinned prompt:** [`prompts/codex-64518-1787530317.md`](prompts/codex-64518-1787530317.md)
- **Project name:** battery-tool-v2
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `inconclusive_no_final`
- **Agent:** `codex`
- **Created at / age:** `2026-08-24T00:11:57+00:00` / **3d 14h**
- **Owner label:** `None`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/tmp/claude-501/brief-sweep-sitemodel.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (5617 chars)
- **Task ids:** `['t-555']`
- **Base SHA:** `a376fc7b69184b21a1bdfc8ca0e8f0610d59b75a`
- **Populations:** `ledger-terminal`, `retired-by-webui`
- **Hints (not verdicts):** HINT: ledger already records terminal state `inconclusive_no_final` (not a queue-purge verdict)

### `codex-69648-1787841595`

- **TLDR:** SC-150 class sweep — read-only diagnosis, do not fix. Ledger running with a live worker pid at inventory. Target: battery-tool-v2.
- **Pinned prompt:** [`prompts/codex-69648-1787841595.md`](prompts/codex-69648-1787841595.md)
- **Project name:** battery-tool-v2
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `None`
- **Queue payload state:** `running`
- **Ledger state (raw):** `running`
- **Agent:** `codex`
- **Created at / age:** `2026-08-27T14:39:56+00:00` / **14m**
- **Owner label:** `battery-bugs`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/tmp/goal-flight-501/dispatch/codex-69648-1787841595.assembled.prompt` — EXISTS on disk
- **Prompt pin:** **pinnable** (12388 chars)
- **Task ids:** `None`
- **Base SHA:** `None`
- **Populations:** `ledger-nonterminal`

### `codex-72685-1787777565`

- **TLDR:** Review-and-verify the UNVERIFIED q-042 salvage branch (read-only; do not modify the branch). Ledger-only; worker pid dead.
- **Pinned prompt:** [`prompts/codex-72685-1787777565.md`](prompts/codex-72685-1787777565.md)
- **Project name:** battery-tool-v2
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `None`
- **Queue payload state:** `running`
- **Ledger state (raw):** `running`
- **Agent:** `codex`
- **Created at / age:** `2026-08-26T20:52:47+00:00` / **18h 1m**
- **Owner label:** `battery-main`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/tmp/goal-flight-501/dispatch/codex-72685-1787777565.assembled.prompt` — EXISTS on disk
- **Prompt pin:** **pinnable** (7187 chars)
- **Task ids:** `None`
- **Base SHA:** `None`
- **Populations:** `ledger-nonterminal`

### `codex-73102-1787700838-retry-c2f707cf`

- **TLDR:** Retry of b-2484 (brief `b2482-brief.md`). Target: battery-tool-v2 main. No ledger record.
- **Pinned prompt:** [`prompts/codex-73102-1787700838-retry-c2f707cf.md`](prompts/codex-73102-1787700838-retry-c2f707cf.md)
- **Project name:** battery-tool-v2
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Queue payload state:** `queued`
- **Ledger state (raw):** `None`
- **Agent:** `codex`
- **Created at / age:** `2026-08-26T00:13:20+00:00` / **1d 14h 40m**
- **Owner label:** `battery-webui`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/9a67b5a3-991f-4854-b2a4-60cae5c8328a/scratchpad/b2482-brief.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (9844 chars)
- **Task ids:** `['b-2484']`
- **Base SHA:** `f33ea700ba04eb60821aaa7d69f4e166bdc67fb2`
- **Populations:** `bare-json`, `queue-top`

### `codex-73527-1787842274`

- **TLDR:** b-2759 — diagnose-then-fix: rail crossing called ORPHAN at 0.0057 inch from its top chord. Appeared mid-inventory with a live claim pid; ledger running.
- **Pinned prompt:** [`prompts/codex-73527-1787842274.md`](prompts/codex-73527-1787842274.md)
- **Project name:** battery-tool-v2
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `None`
- **Queue payload state:** `running`
- **Ledger state (raw):** `running`
- **Agent:** `codex`
- **Created at / age:** `2026-08-27T14:51:14+00:00` / **2m**
- **Owner label:** `battery-engine`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/tmp/goal-flight-501/dispatch/codex-73527-1787842274.assembled.prompt` — EXISTS on disk
- **Prompt pin:** **pinnable** (22146 chars)
- **Task ids:** `None`
- **Base SHA:** `None`
- **Populations:** `ledger-nonterminal`

### `codex-74265-1787791956`

- **TLDR:** b-2702 gaps 2 and 3 — put BESS equipment and keep-clear zones into the IFC. Target: worktree `bt-cat`. Claim-only.
- **Pinned prompt:** [`prompts/codex-74265-1787791956.md`](prompts/codex-74265-1787791956.md)
- **Project name:** battery-tool-v2 (worktree bt-cat)
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-cat`
- **Queue payload state:** `claimed`
- **Ledger state (raw):** `queued`
- **Agent:** `codex`
- **Created at / age:** `2026-08-27T00:52:36+00:00` / **14h 1m**
- **Owner label:** `None`
- **Claim marker:** pid `27400` **dead**
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/c6fb7200-a974-44ea-87a1-0a95d1cfa471/scratchpad/ifc-equip-brief.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (9694 chars)
- **Task ids:** `[]`
- **Base SHA:** `3de431f458cc09ab6b4d859a29d78206b4948d73`
- **Populations:** `claim-marker`, `ledger-nonterminal`

### `codex-75378-1787688360`

- **TLDR:** b-2426 (brief `b2426-brief.md`). Target: worktree `bt-b2154` — both cwd and project_root were MISSING on disk at inventory.
- **Pinned prompt:** [`prompts/codex-75378-1787688360.md`](prompts/codex-75378-1787688360.md)
- **Project name:** battery-tool-v2 (worktree bt-b2154)
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-b2154`
- **Target cwd:** `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-b2154`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `queued`
- **Agent:** `codex`
- **Created at / age:** `2026-08-25T20:06:00+00:00` / **1d 18h 48m**
- **Owner label:** `None`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/9a67b5a3-991f-4854-b2a4-60cae5c8328a/scratchpad/b2426-brief.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (9109 chars)
- **Task ids:** `['b-2426']`
- **Base SHA:** `None`
- **Populations:** `bare-json`, `ledger-nonterminal`, `queue-top`
- **Hints (not verdicts):** HINT: worker_cwd does not exist on disk: `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-b2154`<br>HINT: project_root does not exist on disk: `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-b2154`<br>HINT: worker_cwd missing: `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-b2154`<br>HINT: project_root missing: `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-b2154`

### `codex-80486-1787414845-retry-d69b4bc9`

- **TLDR:** Retry of b-1940 (brief-sfring3). Prompt file MISSING. Present both in the live queue and in retired-by-main. Honest default: abandon.
- **Pinned prompt:** [`prompts/codex-80486-1787414845-retry-d69b4bc9.md`](prompts/codex-80486-1787414845-retry-d69b4bc9.md)
- **Project name:** battery-tool-v2
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Queue payload state:** `queued`
- **Ledger state (raw):** `None`
- **Agent:** `codex`
- **Created at / age:** `2026-08-24T05:41:43+00:00` / **3d 9h**
- **Owner label:** `battery-bugs`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/c6fb7200-a974-44ea-87a1-0a95d1cfa471/scratchpad/brief-sfring3.md` — MISSING on disk
- **Prompt pin:** **missing** (0 chars)
- **Task ids:** `['b-1940']`
- **Base SHA:** `c81f2f6218c85e7ab9b8b478f17603d7e92680a7`
- **Populations:** `bare-json`, `queue-top`, `retired-by-main`
- **Hints (not verdicts):** HINT: prompt-file is gone — premise cannot be re-checked; honest default is abandon

### `codex-86585-1787546556`

- **TLDR:** b-2148 — two defects in the Playwright acceptance bar itself (fix the bar, not the product). Retired-by-main. Target: battery-tool-v2 main.
- **Pinned prompt:** [`prompts/codex-86585-1787546556.md`](prompts/codex-86585-1787546556.md)
- **Project name:** battery-tool-v2
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `inconclusive_no_final`
- **Agent:** `codex`
- **Created at / age:** `2026-08-24T04:42:36+00:00` / **3d 10h**
- **Owner label:** `None`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/2ee708ed-cae0-44ff-be85-9cf833e8930f/scratchpad/briefs/b2148.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (11821 chars)
- **Task ids:** `['b-2148']`
- **Base SHA:** `3677516bd575bad5c1e582be40ba5b90cf21cc34`
- **Populations:** `ledger-terminal`, `retired-by-main`
- **Hints (not verdicts):** HINT: ledger already records terminal state `inconclusive_no_final` (not a queue-purge verdict)

### `grok-code-21722-1787778673`

- **TLDR:** b-2630 / b-2619 — reimport must invalidate/bypass the stale acquisition cache. Target: worktree `bt-b2630`.
- **Pinned prompt:** [`prompts/grok-code-21722-1787778673.md`](prompts/grok-code-21722-1787778673.md)
- **Project name:** battery-tool-v2 (worktree bt-b2630)
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-b2630`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `queued`
- **Agent:** `grok-code`
- **Created at / age:** `2026-08-26T21:11:13+00:00` / **17h 42m**
- **Owner label:** `battery-main`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/2ee708ed-cae0-44ff-be85-9cf833e8930f/scratchpad/brief-b2630-cache.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (7174 chars)
- **Task ids:** `['b-2630', 'b-2619']`
- **Base SHA:** `05bff28cf2c7b35c93e7c49d0cfd84b58c2419f4`
- **Populations:** `bare-json`, `ledger-nonterminal`, `queue-top`

### `grok-code-28489-1787781619`

- **TLDR:** t-602 scout A (read-only): ground-site mode — acquisition/lot-polygon + import seam. Target: battery-tool-v2 main. Claim-only.
- **Pinned prompt:** [`prompts/grok-code-28489-1787781619.md`](prompts/grok-code-28489-1787781619.md)
- **Project name:** battery-tool-v2
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Queue payload state:** `claimed`
- **Ledger state (raw):** `failed`
- **Agent:** `grok-code`
- **Created at / age:** `2026-08-26T22:00:19+00:00` / **16h 53m**
- **Owner label:** `battery-main`
- **Claim marker:** pid `34323` **dead**
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/2ee708ed-cae0-44ff-be85-9cf833e8930f/scratchpad/brief-t602-scoutA.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (5427 chars)
- **Task ids:** `['t-602']`
- **Base SHA:** `6d79ad34c2aa26b5152ada8c5e18b85fa781e2ee`
- **Populations:** `claim-marker`, `ledger-terminal`
- **Hints (not verdicts):** HINT: ledger already records terminal state `failed` (not a queue-purge verdict)

### `grok-code-34851-1787725369`

- **TLDR:** Offer-review (`offer-review-brief.md`). Target: worktree `bt-d013` which was MISSING on disk.
- **Pinned prompt:** [`prompts/grok-code-34851-1787725369.md`](prompts/grok-code-34851-1787725369.md)
- **Project name:** battery-tool-v2 (worktree bt-d013)
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-d013`
- **Target cwd:** `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-d013`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `queued`
- **Agent:** `grok-code`
- **Created at / age:** `2026-08-26T06:22:49+00:00` / **1d 8h 31m**
- **Owner label:** `None`
- **Claim marker:** pid `27995` **dead**
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/9a67b5a3-991f-4854-b2a4-60cae5c8328a/scratchpad/offer-review-brief.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (6373 chars)
- **Task ids:** `[]`
- **Base SHA:** `None`
- **Populations:** `bare-json`, `claim-marker`, `ledger-nonterminal`, `queue-top`
- **Hints (not verdicts):** HINT: worker_cwd does not exist on disk: `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-d013`<br>HINT: project_root does not exist on disk: `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-d013`<br>HINT: worker_cwd missing: `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-d013`<br>HINT: project_root missing: `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-d013`

### `grok-code-58307-1787790827`

- **TLDR:** t-605 (G1 of ground-site mode t-602): get the lot polygon into the pipeline. Target: worktree `bt-t605`. Claim-only.
- **Pinned prompt:** [`prompts/grok-code-58307-1787790827.md`](prompts/grok-code-58307-1787790827.md)
- **Project name:** battery-tool-v2 (worktree bt-t605)
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-t605`
- **Queue payload state:** `claimed`
- **Ledger state (raw):** `starting`
- **Agent:** `grok-code`
- **Created at / age:** `2026-08-27T00:33:47+00:00` / **14h 20m**
- **Owner label:** `battery-main`
- **Claim marker:** pid `57031` **dead**
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/2ee708ed-cae0-44ff-be85-9cf833e8930f/scratchpad/brief-t605.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (7480 chars)
- **Task ids:** `['t-605']`
- **Base SHA:** `3de431f458cc09ab6b4d859a29d78206b4948d73`
- **Populations:** `claim-marker`, `ledger-nonterminal`

### `grok-code-70915-1787842258`

- **TLDR:** SC-150 class sweep (STORED-VERDICT.md) — read-only diagnosis. Target: worktree `bt-sc150-3`. Appeared mid-inventory with a live claim pid.
- **Pinned prompt:** [`prompts/grok-code-70915-1787842258.md`](prompts/grok-code-70915-1787842258.md)
- **Project name:** battery-tool-v2 (worktree bt-sc150-3)
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-sc150-3`
- **Queue payload state:** `claimed`
- **Ledger state (raw):** `running`
- **Agent:** `grok-code`
- **Created at / age:** `2026-08-27T14:50:58+00:00` / **3m**
- **Owner label:** `battery-bugs`
- **Claim marker:** pid `69178` **dead**
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/14146fbb-321f-44b3-8014-c2faa22b0e32/scratchpad/sc150/STORED-VERDICT.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (10883 chars)
- **Task ids:** `[]`
- **Base SHA:** `18fb8f6c948af003915a2c22a390e145635e8163`
- **Populations:** `claim-marker`, `ledger-nonterminal`

### `grok-code-77178-1787729728`

- **TLDR:** Offer-review (`offer-review-brief.md`), second copy. Target: worktree `bt-d013` which was MISSING on disk.
- **Pinned prompt:** [`prompts/grok-code-77178-1787729728.md`](prompts/grok-code-77178-1787729728.md)
- **Project name:** battery-tool-v2 (worktree bt-d013)
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-d013`
- **Target cwd:** `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-d013`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `queued`
- **Agent:** `grok-code`
- **Created at / age:** `2026-08-26T07:35:28+00:00` / **1d 7h 18m**
- **Owner label:** `None`
- **Claim marker:** pid `84166` **dead**
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-battery-tool-v2/9a67b5a3-991f-4854-b2a4-60cae5c8328a/scratchpad/offer-review-brief.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (6373 chars)
- **Task ids:** `[]`
- **Base SHA:** `None`
- **Populations:** `bare-json`, `claim-marker`, `ledger-nonterminal`, `queue-top`
- **Hints (not verdicts):** HINT: worker_cwd does not exist on disk: `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-d013`<br>HINT: project_root does not exist on disk: `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-d013`<br>HINT: worker_cwd missing: `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-d013`<br>HINT: project_root missing: `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-d013`

### `grok-code-84519-1787841683`

- **TLDR:** SC-150 class sweep — read-only diagnosis, do not fix. Ledger-only; worker pid dead. Target: battery-tool-v2.
- **Pinned prompt:** [`prompts/grok-code-84519-1787841683.md`](prompts/grok-code-84519-1787841683.md)
- **Project name:** battery-tool-v2
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `None`
- **Queue payload state:** `running`
- **Ledger state (raw):** `running`
- **Agent:** `grok-code`
- **Created at / age:** `2026-08-27T14:41:23+00:00` / **12m**
- **Owner label:** `battery-bugs`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/tmp/goal-flight-501/dispatch/grok-code-84519-1787841683.assembled.prompt` — EXISTS on disk
- **Prompt pin:** **pinnable** (12670 chars)
- **Task ids:** `None`
- **Base SHA:** `None`
- **Populations:** `ledger-nonterminal`

### `raw-b2753-studio-e02ec9816-r2`

- **TLDR:** Premise missing (no inline prompt, no prompt-file). Target cwd: worktree `bt-b2753`. Honest default: abandon.
- **Pinned prompt:** [`prompts/raw-b2753-studio-e02ec9816-r2.md`](prompts/raw-b2753-studio-e02ec9816-r2.md)
- **Project name:** battery-tool-v2 (worktree bt-b2753)
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-b2753`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `queued`
- **Agent:** `codex`
- **Created at / age:** `2026-08-27T14:21:10+00:00` / **32m**
- **Owner label:** `None`
- **Claim marker:** pid `26932` **dead**
- **Prompt source:** missing (no path)
- **Prompt pin:** **missing** (0 chars)
- **Task ids:** `[]`
- **Base SHA:** `18fb8f6c948af003915a2c22a390e145635e8163`
- **Populations:** `bare-json`, `claim-marker`, `ledger-nonterminal`, `queue-top`

### `zw-aw31`

- **TLDR:** Awaiting-review batch 31: verify 14 already-closed rows against current release tip. Target: battery-tool-v2. Ledger `running` but worker pid was dead at inventory.
- **Pinned prompt:** [`prompts/zw-aw31.md`](prompts/zw-aw31.md)
- **Project name:** battery-tool-v2
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `None`
- **Queue payload state:** `running`
- **Ledger state (raw):** `running`
- **Agent:** `codex`
- **Created at / age:** `2026-08-25T17:18:30+00:00` / **1d 21h 35m**
- **Owner label:** `battery-bugs`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/tmp/goal-flight-501/dispatch/zw-aw31.assembled.prompt` — EXISTS on disk
- **Prompt pin:** **pinnable** (20961 chars)
- **Task ids:** `None`
- **Base SHA:** `None`
- **Populations:** `ledger-nonterminal`

### `zw-fix_other_cross-layer__b1`

- **TLDR:** ZERO campaign fix wave 2, batch `fix_other_cross-layer__b1` (adapters/exporters). Target: worktree `bt-zw-fix_other_cross-layer__b1` DETACHED at origin/main 0bcbebe2a. Ledger `waiting_capacity`, age ~2d 18h.
- **Pinned prompt:** [`prompts/zw-fix_other_cross-layer__b1.md`](prompts/zw-fix_other_cross-layer__b1.md)
- **Project name:** battery-tool-v2
- **Project root:** `/Users/simonrowland/Repos/battery-tool-v2`
- **Target cwd:** `None`
- **Queue payload state:** `waiting_capacity`
- **Ledger state (raw):** `waiting_capacity`
- **Agent:** `codex`
- **Created at / age:** `2026-08-24T20:42:44+00:00` / **2d 18h**
- **Owner label:** `battery-bugs`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/tmp/goal-flight-501/dispatch/zw-fix_other_cross-layer__b1.assembled.prompt` — EXISTS on disk
- **Prompt pin:** **pinnable** (23892 chars)
- **Task ids:** `None`
- **Base SHA:** `None`
- **Populations:** `ledger-nonterminal`

