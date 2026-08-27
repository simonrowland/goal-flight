# Queue inventory for **pm2** — 2026-08-27

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


This list is for the pm2 controllers (main, engine, bugs). Several entries are duplicate layermap-harvest probes against a pinned HEAD, or they target `/private/tmp/pm2-*` worktrees rather than `Repos/pm2`.

See [INDEX.md](INDEX.md) for population counts and method. This file lists **16 / 81** union dispatch ids whose `project_root` (or cwd) belongs to this project.

Prompts: **14 / 16** pinnable, **2 / 16** missing.

## Quick list

| dispatch_id | age | owner | agent | claim pid | prompt | TLDR |
|---|---|---|---|---|---|---|
| [`b13-1-reverse-mass-drivers`](#b13-1-reverse-mass-drivers) | 18m | `pm2-main` | `grok-code` | none | pinned | Implement B13 chunk 1 — reverse mass-driver family (t-261 / BATCH-PLAN B13). Target: pm2 main repo. Ledger running with a live worker pid at inventory — may still be in flight. |
| [`b264probe-1`](#b264probe-1) | 9h 55m | `None` | `grok-code` | pid `5987` **dead** | pinned | Duplicate of layermap-harvest (t-763): collect LAYER-MAP declaration-needs; do not edit docs/LAYER-MAP.md. Target: pm2 HEAD 590b1ae. Ledger already `complete`. |
| [`b264probe-2`](#b264probe-2) | 9h 54m | `None` | `grok-code` | pid `26544` **dead** | pinned | Duplicate of layermap-harvest (t-763) against pm2 HEAD 590b1ae. Same prompt as b264probe-1. Ledger already `complete`. |
| [`b264probe-4`](#b264probe-4) | 9h 53m | `None` | `grok-code` | pid `92483` **dead** | pinned | Duplicate of layermap-harvest (t-763) against pm2 HEAD 590b1ae. Same prompt as b264probe-1. Ledger already `complete`. |
| [`b285-ring-coherence-adjudication`](#b285-ring-coherence-adjudication) | 4m | `pm2-main` | `grok-code` | none | **MISSING** | Adjudicate ring coherence (b-285). Target: `/private/tmp/pm2-b285/pm2`. Prompt file MISSING. Bare json was in the first snapshot then vanished from the live dir; ledger running with a live worker pid. |
| [`bugs-b277a`](#bugs-b277a) | 16m | `pm2-bugs` | `grok-code` | none | pinned | Fix b-277: eleven copies of a refusal idiom make a false claim (bugs-lane fixer). Target: pm2. Ledger-only; worker pid dead. |
| [`fr-d1-r3-retry-b8fa0aba`](#fr-d1-r3-retry-b8fa0aba) | 1d 20h 48m | `pm2-main` | `codex` | none | pinned | Retry of force-rail carrier D1 (device-plasma, t-742). Target: pm2 main repo at the queued HEAD. No claim marker. |
| [`fuzzspoke`](#fuzzspoke) | 1d 15h 7m | `None` | `codex` | pid `12979` **dead** (retired-by-main) | pinned | Teach the fuzzer generator the SPOKE aggregate ceiling. Target: pm2 main repo. Already in retired-by-main (bare + claim marker). |
| [`layermap-harvest`](#layermap-harvest) | 12h 8m | `None` | `codex` | pid `8209` **dead** | pinned | Harvest every owed LAYER-MAP declaration-need (t-763); do not edit docs/LAYER-MAP.md. Target: pm2 HEAD 590b1ae. |
| [`layermap-harvest-d`](#layermap-harvest-d) | 9h 59m | `None` | `grok-code` | pid `41095` **dead** | pinned | Same layermap-harvest (t-763) prompt, dispatch id -d. Claim-only (no surviving bare json) — unrecoverable by drain. Target: pm2 HEAD 590b1ae. |
| [`layermap-harvest-g`](#layermap-harvest-g) | 11h 50m | `pm2-main` | `grok-code` | pid `11354` **dead** | pinned | Same layermap-harvest (t-763) prompt, dispatch id -g. Target: pm2 HEAD 590b1ae. |
| [`t292-relativistic-gathered-mass`](#t292-relativistic-gathered-mass) | 13m | `pm2-main` | `grok-code` | none | pinned | Adjudicate relativistic gathered-mass energization (t-292; extends t-287). Target: pm2 main repo. Ledger running with a live worker pid at inventory. |
| [`t702-rev-seam`](#t702-rev-seam) | 14h 21m | `pm2-engine` | `grok-code` | pid `15085` **dead** | **MISSING** | Review-seam work (t-702). Prompt file `docs-private/task-prompts/2026-08-26-engine/t702-rev-seam.md` is MISSING. Target: pm2 repo. Honest default: abandon unless the brief is reconstructed. |
| [`t746-r2-retry-15cc828e`](#t746-r2-retry-15cc828e) | 1d 20h 42m | `pm2-main` | `codex` | none | pinned | t-746 — the lossy numeric environment vector (retry). Target: pm2 HEAD 06e62d2 (main tree, not a worktree). No claim marker. |
| [`t800-pulse`](#t800-pulse) | 5h 40m | `pm2-engine` | `codex` | pid `43430` **dead** | pinned | t-800 pulse/reactive adequacy (store can be joule-rich and still miss the chirp edge). Target: `/private/tmp/pm2-engine-t800/pm2` branch `engine-t800-pulse`. Claim-only; ledger `failed`. |
| [`t801-fix1`](#t801-fix1) | 45m | `pm2-engine` | `codex` | none | pinned | t-801 fix round on physics+honesty review FAILs (commit b1b0a9c). Target: `/private/tmp/pm2-engine-t801/pm2`. Ledger running with a live worker pid at inventory. |

## Entries

### `b13-1-reverse-mass-drivers`

- **TLDR:** Implement B13 chunk 1 — reverse mass-driver family (t-261 / BATCH-PLAN B13). Target: pm2 main repo. Ledger running with a live worker pid at inventory — may still be in flight.
- **Pinned prompt:** [`prompts/b13-1-reverse-mass-drivers.md`](prompts/b13-1-reverse-mass-drivers.md)
- **Project name:** pm2
- **Project root:** `/Users/simonrowland/Repos/pm2`
- **Target cwd:** `None`
- **Queue payload state:** `running`
- **Ledger state (raw):** `running`
- **Agent:** `grok-code`
- **Created at / age:** `2026-08-27T14:35:24+00:00` / **18m**
- **Owner label:** `pm2-main`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/tmp/goal-flight-501/dispatch/b13-1-reverse-mass-drivers.assembled.prompt` — EXISTS on disk
- **Prompt pin:** **pinnable** (6994 chars)
- **Task ids:** `None`
- **Base SHA:** `None`
- **Populations:** `ledger-nonterminal`

### `b264probe-1`

- **TLDR:** Duplicate of layermap-harvest (t-763): collect LAYER-MAP declaration-needs; do not edit docs/LAYER-MAP.md. Target: pm2 HEAD 590b1ae. Ledger already `complete`.
- **Pinned prompt:** [`prompts/b264probe-1.md`](prompts/b264probe-1.md)
- **Project name:** pm2
- **Project root:** `/Users/simonrowland/Repos/pm2`
- **Target cwd:** `/Users/simonrowland/Repos/pm2`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `complete`
- **Agent:** `grok-code`
- **Created at / age:** `2026-08-27T04:58:44+00:00` / **9h 55m**
- **Owner label:** `None`
- **Claim marker:** pid `5987` **dead**
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-pm2/d9989eb0-0cdd-4ccc-be80-cc86a4d2a55f/scratchpad/layermap-harvest.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (5796 chars)
- **Task ids:** `[]`
- **Base SHA:** `4f73706cdc218ad52b31a7ee81d3546682358bd9`
- **Populations:** `bare-json`, `claim-marker`, `ledger-terminal`, `queue-top`
- **Hints (not verdicts):** HINT: ledger already records terminal state `complete` (not a queue-purge verdict)

### `b264probe-2`

- **TLDR:** Duplicate of layermap-harvest (t-763) against pm2 HEAD 590b1ae. Same prompt as b264probe-1. Ledger already `complete`.
- **Pinned prompt:** [`prompts/b264probe-2.md`](prompts/b264probe-2.md)
- **Project name:** pm2
- **Project root:** `/Users/simonrowland/Repos/pm2`
- **Target cwd:** `/Users/simonrowland/Repos/pm2`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `complete`
- **Agent:** `grok-code`
- **Created at / age:** `2026-08-27T04:59:18+00:00` / **9h 54m**
- **Owner label:** `None`
- **Claim marker:** pid `26544` **dead**
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-pm2/d9989eb0-0cdd-4ccc-be80-cc86a4d2a55f/scratchpad/layermap-harvest.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (5796 chars)
- **Task ids:** `[]`
- **Base SHA:** `4f73706cdc218ad52b31a7ee81d3546682358bd9`
- **Populations:** `bare-json`, `claim-marker`, `ledger-terminal`, `queue-top`
- **Hints (not verdicts):** HINT: ledger already records terminal state `complete` (not a queue-purge verdict)

### `b264probe-4`

- **TLDR:** Duplicate of layermap-harvest (t-763) against pm2 HEAD 590b1ae. Same prompt as b264probe-1. Ledger already `complete`.
- **Pinned prompt:** [`prompts/b264probe-4.md`](prompts/b264probe-4.md)
- **Project name:** pm2
- **Project root:** `/Users/simonrowland/Repos/pm2`
- **Target cwd:** `/Users/simonrowland/Repos/pm2`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `complete`
- **Agent:** `grok-code`
- **Created at / age:** `2026-08-27T05:00:12+00:00` / **9h 53m**
- **Owner label:** `None`
- **Claim marker:** pid `92483` **dead**
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-pm2/d9989eb0-0cdd-4ccc-be80-cc86a4d2a55f/scratchpad/layermap-harvest.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (5796 chars)
- **Task ids:** `[]`
- **Base SHA:** `None`
- **Populations:** `bare-json`, `claim-marker`, `ledger-terminal`, `queue-top`
- **Hints (not verdicts):** HINT: ledger already records terminal state `complete` (not a queue-purge verdict)

### `b285-ring-coherence-adjudication`

- **TLDR:** Adjudicate ring coherence (b-285). Target: `/private/tmp/pm2-b285/pm2`. Prompt file MISSING. Bare json was in the first snapshot then vanished from the live dir; ledger running with a live worker pid.
- **Pinned prompt:** [`prompts/b285-ring-coherence-adjudication.md`](prompts/b285-ring-coherence-adjudication.md)
- **Project name:** pm2
- **Project root:** `/Users/simonrowland/Repos/pm2`
- **Target cwd:** `/private/tmp/pm2-b285/pm2`
- **Queue payload state:** `queued`
- **Ledger state (raw):** `running`
- **Agent:** `grok-code`
- **Created at / age:** `2026-08-27T14:49:23+00:00` / **4m**
- **Owner label:** `pm2-main`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `docs-private/dispatch/b285-ring-coherence-adjudication.md` — MISSING on disk
- **Prompt pin:** **missing** (0 chars)
- **Task ids:** `['b-285']`
- **Base SHA:** `e806bf53b37ff416450608a89861e74823d00795`
- **Populations:** `bare-json`, `ledger-nonterminal`, `queue-top`
- **Hints (not verdicts):** HINT: prompt-file is gone — premise cannot be re-checked; honest default is abandon

### `bugs-b277a`

- **TLDR:** Fix b-277: eleven copies of a refusal idiom make a false claim (bugs-lane fixer). Target: pm2. Ledger-only; worker pid dead.
- **Pinned prompt:** [`prompts/bugs-b277a.md`](prompts/bugs-b277a.md)
- **Project name:** pm2
- **Project root:** `/Users/simonrowland/Repos/pm2`
- **Target cwd:** `None`
- **Queue payload state:** `running`
- **Ledger state (raw):** `running`
- **Agent:** `grok-code`
- **Created at / age:** `2026-08-27T14:37:19+00:00` / **16m**
- **Owner label:** `pm2-bugs`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/tmp/goal-flight-501/dispatch/bugs-b277a.assembled.prompt` — EXISTS on disk
- **Prompt pin:** **pinnable** (8673 chars)
- **Task ids:** `None`
- **Base SHA:** `None`
- **Populations:** `ledger-nonterminal`

### `fr-d1-r3-retry-b8fa0aba`

- **TLDR:** Retry of force-rail carrier D1 (device-plasma, t-742). Target: pm2 main repo at the queued HEAD. No claim marker.
- **Pinned prompt:** [`prompts/fr-d1-r3-retry-b8fa0aba.md`](prompts/fr-d1-r3-retry-b8fa0aba.md)
- **Project name:** pm2
- **Project root:** `/Users/simonrowland/Repos/pm2`
- **Target cwd:** `/Users/simonrowland/Repos/pm2`
- **Queue payload state:** `queued`
- **Ledger state (raw):** `None`
- **Agent:** `codex`
- **Created at / age:** `2026-08-25T18:05:26+00:00` / **1d 20h 48m**
- **Owner label:** `pm2-main`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-pm2/d9989eb0-0cdd-4ccc-be80-cc86a4d2a55f/scratchpad/fr-d1-r3.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (5349 chars)
- **Task ids:** `['t-742']`
- **Base SHA:** `10d2a006b8245634965385180b97a174f69174d3`
- **Populations:** `bare-json`, `queue-top`

### `fuzzspoke`

- **TLDR:** Teach the fuzzer generator the SPOKE aggregate ceiling. Target: pm2 main repo. Already in retired-by-main (bare + claim marker).
- **Pinned prompt:** [`prompts/fuzzspoke.md`](prompts/fuzzspoke.md)
- **Project name:** pm2
- **Project root:** `/Users/simonrowland/Repos/pm2`
- **Target cwd:** `/Users/simonrowland/Repos/pm2`
- **Queue payload state:** `claimed`
- **Ledger state (raw):** `inconclusive_no_final`
- **Agent:** `codex`
- **Created at / age:** `2026-08-25T23:46:55+00:00` / **1d 15h 7m**
- **Owner label:** `None`
- **Claim marker:** pid `12979` **dead** (retired-by-main)
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-pm2/d9989eb0-0cdd-4ccc-be80-cc86a4d2a55f/scratchpad/fuzzspoke.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (2899 chars)
- **Task ids:** `[]`
- **Base SHA:** `8f4e98d03cf3db718ec9fe1769d432c966a63531`
- **Populations:** `ledger-terminal`, `retired-by-main`, `retired-by-main/claim-marker`
- **Hints (not verdicts):** HINT: ledger already records terminal state `inconclusive_no_final` (not a queue-purge verdict)

### `layermap-harvest`

- **TLDR:** Harvest every owed LAYER-MAP declaration-need (t-763); do not edit docs/LAYER-MAP.md. Target: pm2 HEAD 590b1ae.
- **Pinned prompt:** [`prompts/layermap-harvest.md`](prompts/layermap-harvest.md)
- **Project name:** pm2
- **Project root:** `/Users/simonrowland/Repos/pm2`
- **Target cwd:** `/Users/simonrowland/Repos/pm2`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `queued`
- **Agent:** `codex`
- **Created at / age:** `2026-08-27T02:45:10+00:00` / **12h 8m**
- **Owner label:** `None`
- **Claim marker:** pid `8209` **dead**
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-pm2/d9989eb0-0cdd-4ccc-be80-cc86a4d2a55f/scratchpad/layermap-harvest.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (5796 chars)
- **Task ids:** `[]`
- **Base SHA:** `590b1ae6442913be3fc83a08b57cedbbe990c218`
- **Populations:** `bare-json`, `claim-marker`, `ledger-nonterminal`, `queue-top`

### `layermap-harvest-d`

- **TLDR:** Same layermap-harvest (t-763) prompt, dispatch id -d. Claim-only (no surviving bare json) — unrecoverable by drain. Target: pm2 HEAD 590b1ae.
- **Pinned prompt:** [`prompts/layermap-harvest-d.md`](prompts/layermap-harvest-d.md)
- **Project name:** pm2
- **Project root:** `/Users/simonrowland/Repos/pm2`
- **Target cwd:** `/Users/simonrowland/Repos/pm2`
- **Queue payload state:** `claimed`
- **Ledger state (raw):** `queued`
- **Agent:** `grok-code`
- **Created at / age:** `2026-08-27T04:55:01+00:00` / **9h 59m**
- **Owner label:** `None`
- **Claim marker:** pid `41095` **dead**
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-pm2/d9989eb0-0cdd-4ccc-be80-cc86a4d2a55f/scratchpad/layermap-harvest.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (5796 chars)
- **Task ids:** `[]`
- **Base SHA:** `4f73706cdc218ad52b31a7ee81d3546682358bd9`
- **Populations:** `claim-marker`, `ledger-nonterminal`

### `layermap-harvest-g`

- **TLDR:** Same layermap-harvest (t-763) prompt, dispatch id -g. Target: pm2 HEAD 590b1ae.
- **Pinned prompt:** [`prompts/layermap-harvest-g.md`](prompts/layermap-harvest-g.md)
- **Project name:** pm2
- **Project root:** `/Users/simonrowland/Repos/pm2`
- **Target cwd:** `/Users/simonrowland/Repos/pm2`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `queued`
- **Agent:** `grok-code`
- **Created at / age:** `2026-08-27T03:03:09+00:00` / **11h 50m**
- **Owner label:** `pm2-main`
- **Claim marker:** pid `11354` **dead**
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-pm2/d9989eb0-0cdd-4ccc-be80-cc86a4d2a55f/scratchpad/layermap-harvest.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (5796 chars)
- **Task ids:** `[]`
- **Base SHA:** `b85cf49fea7dd9a7ec5eda87fd1b4556b167d7f5`
- **Populations:** `bare-json`, `claim-marker`, `ledger-nonterminal`, `queue-top`

### `t292-relativistic-gathered-mass`

- **TLDR:** Adjudicate relativistic gathered-mass energization (t-292; extends t-287). Target: pm2 main repo. Ledger running with a live worker pid at inventory.
- **Pinned prompt:** [`prompts/t292-relativistic-gathered-mass.md`](prompts/t292-relativistic-gathered-mass.md)
- **Project name:** pm2
- **Project root:** `/Users/simonrowland/Repos/pm2`
- **Target cwd:** `None`
- **Queue payload state:** `running`
- **Ledger state (raw):** `running`
- **Agent:** `grok-code`
- **Created at / age:** `2026-08-27T14:40:53+00:00` / **13m**
- **Owner label:** `pm2-main`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/tmp/goal-flight-501/dispatch/t292-relativistic-gathered-mass.assembled.prompt` — EXISTS on disk
- **Prompt pin:** **pinnable** (7742 chars)
- **Task ids:** `None`
- **Base SHA:** `None`
- **Populations:** `ledger-nonterminal`

### `t702-rev-seam`

- **TLDR:** Review-seam work (t-702). Prompt file `docs-private/task-prompts/2026-08-26-engine/t702-rev-seam.md` is MISSING. Target: pm2 repo. Honest default: abandon unless the brief is reconstructed.
- **Pinned prompt:** [`prompts/t702-rev-seam.md`](prompts/t702-rev-seam.md)
- **Project name:** pm2
- **Project root:** `/Users/simonrowland/Repos/pm2`
- **Target cwd:** `/Users/simonrowland/Repos/pm2`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `queued`
- **Agent:** `grok-code`
- **Created at / age:** `2026-08-27T00:32:28+00:00` / **14h 21m**
- **Owner label:** `pm2-engine`
- **Claim marker:** pid `15085` **dead**
- **Prompt source:** `--prompt-file` `docs-private/task-prompts/2026-08-26-engine/t702-rev-seam.md` — MISSING on disk
- **Prompt pin:** **missing** (0 chars)
- **Task ids:** `[]`
- **Base SHA:** `None`
- **Populations:** `bare-json`, `claim-marker`, `ledger-nonterminal`, `queue-top`
- **Hints (not verdicts):** HINT: prompt-file is gone — premise cannot be re-checked; honest default is abandon

### `t746-r2-retry-15cc828e`

- **TLDR:** t-746 — the lossy numeric environment vector (retry). Target: pm2 HEAD 06e62d2 (main tree, not a worktree). No claim marker.
- **Pinned prompt:** [`prompts/t746-r2-retry-15cc828e.md`](prompts/t746-r2-retry-15cc828e.md)
- **Project name:** pm2
- **Project root:** `/Users/simonrowland/Repos/pm2`
- **Target cwd:** `/Users/simonrowland/Repos/pm2`
- **Queue payload state:** `queued`
- **Ledger state (raw):** `None`
- **Agent:** `codex`
- **Created at / age:** `2026-08-25T18:11:32+00:00` / **1d 20h 42m**
- **Owner label:** `pm2-main`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Repos-pm2/d9989eb0-0cdd-4ccc-be80-cc86a4d2a55f/scratchpad/t746.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (3406 chars)
- **Task ids:** `['t-746']`
- **Base SHA:** `10d2a006b8245634965385180b97a174f69174d3`
- **Populations:** `bare-json`, `queue-top`

### `t800-pulse`

- **TLDR:** t-800 pulse/reactive adequacy (store can be joule-rich and still miss the chirp edge). Target: `/private/tmp/pm2-engine-t800/pm2` branch `engine-t800-pulse`. Claim-only; ledger `failed`.
- **Pinned prompt:** [`prompts/t800-pulse.md`](prompts/t800-pulse.md)
- **Project name:** pm2
- **Project root:** `/Users/simonrowland/Repos/pm2`
- **Target cwd:** `/Users/simonrowland/Repos/pm2`
- **Queue payload state:** `claimed`
- **Ledger state (raw):** `failed`
- **Agent:** `codex`
- **Created at / age:** `2026-08-27T09:13:11+00:00` / **5h 40m**
- **Owner label:** `pm2-engine`
- **Claim marker:** pid `43430` **dead**
- **Prompt source:** `--prompt-file` `/private/tmp/pm2-engine-t800/BRIEF-t800.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (5384 chars)
- **Task ids:** `[]`
- **Base SHA:** `None`
- **Populations:** `claim-marker`, `ledger-terminal`
- **Hints (not verdicts):** HINT: ledger already records terminal state `failed` (not a queue-purge verdict)

### `t801-fix1`

- **TLDR:** t-801 fix round on physics+honesty review FAILs (commit b1b0a9c). Target: `/private/tmp/pm2-engine-t801/pm2`. Ledger running with a live worker pid at inventory.
- **Pinned prompt:** [`prompts/t801-fix1.md`](prompts/t801-fix1.md)
- **Project name:** pm2
- **Project root:** `/Users/simonrowland/Repos/pm2`
- **Target cwd:** `None`
- **Queue payload state:** `running`
- **Ledger state (raw):** `running`
- **Agent:** `codex`
- **Created at / age:** `2026-08-27T14:08:18+00:00` / **45m**
- **Owner label:** `pm2-engine`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/tmp/goal-flight-501/dispatch/t801-fix1.assembled.prompt` — EXISTS on disk
- **Prompt pin:** **pinnable** (3800 chars)
- **Task ids:** `None`
- **Base SHA:** `None`
- **Populations:** `ledger-nonterminal`

