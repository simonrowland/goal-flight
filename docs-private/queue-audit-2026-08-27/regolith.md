# Queue inventory for **regolith-pyrolysis-simulator** — 2026-08-27

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


This list is for the regolith-pyrolysis-simulator controllers (regolith-main, regolith-engine). **Stale-target risk is the whole point of this inventory:** several prompts name a specific worktree (`rps-b234`, `rps-t481`, `rps-b236`) or an uncommitted `engine-2026-08-16` working tree. Re-firing those from the Dropbox main tree, or after that tree has moved, would audit the wrong code and report confidently.

See [INDEX.md](INDEX.md) for population counts and method. This file lists **11 / 81** union dispatch ids whose `project_root` (or cwd) belongs to this project.

Prompts: **11 / 11** pinnable, **0 / 11** missing.

## Quick list

| dispatch_id | age | owner | agent | claim pid | prompt | TLDR |
|---|---|---|---|---|---|---|
| [`b234-mre-scope`](#b234-mre-scope) | 21h 41m | `regolith-main` | `moonshot` | none | pinned | Scope whether the MRE charge-accounting fail-open (b-234, `simulator/extraction.py` ~2113) fires on any golden feedstock. Target: `/Users/simonrowland/Repos/rps-b234` branch `work-b234` — NOT the Dropbox main tree. Stale-target risk is exactly the regolith worked example. |
| [`magemin-status-review`](#magemin-status-review) | 21h 58m | `None` | `codex` | pid `21784` **dead** | pinned | Review an uncommitted MagmaMin bugfix (`engines/magemin/`) on branch `engine-2026-08-16`. Target: Dropbox working-tree diff. Also copied under retired-by-engine. |
| [`sr2-closure`](#sr2-closure) | 21h 17m | `regolith-engine` | `codex` | pid `32218` **dead** | pinned | Closure review of a round-2 bugfix on branch `engine-2026-08-16` (uncommitted tree + untracked test). Claim-only, no surviving bare json. |
| [`status-r2-closure`](#status-r2-closure) | 21h 19m | `regolith-engine` | `codex` | pid `6554` **dead** (quarantine) | pinned | Same round-2 closure review as sr2-closure. Quarantined claim marker (pid dead). Ledger `failed`. |
| [`status-r2-notfixed`](#status-r2-notfixed) | 21h 20m | `None` | `grok-code` | pid `97635` **dead** | pinned | Adversarial NOT-FIXED review, round 2, of the uncommitted `engine-2026-08-16` tree. Also copied under retired-by-engine. |
| [`t481-registry-decide`](#t481-registry-decide) | 21h 42m | `regolith-main` | `moonshot` | none | pinned | Decide the fate of a fully-built, never-read phase-aware volatile-property registry (t-481). Target: `/Users/simonrowland/Repos/rps-t481` branch `work-t481` (HEAD 236553f9), not the Dropbox main tree. |
| [`t697-dunite`](#t697-dunite) | 22h 7m | `None` | `grok-code` | pid `11223` **dead** | pinned | Diagnose an unexplained residual in the dunite melt-activity benchmark. Target: Dropbox `regolith-pyrolysis-simulator` working tree. Also copied under retired-by-engine. |
| [`t743-exclusion-audit`](#t743-exclusion-audit) | 21h 14m | `regolith-main` | `moonshot` | none | pinned | Exclusion audit: does the validation battery only discard data that flatters the model? (task_ids t-754 / KEMS). Target: Dropbox `regolith-pyrolysis-simulator` tree. |
| [`t748-stage-pressures`](#t748-stage-pressures) | 22h 10m | `regolith-main` | `moonshot` | none | pinned | Derive per-species STAGE partial pressure and whether each condenser captures its species (t-748). Target: `/Users/simonrowland/Repos/rps-b236` branch `work-b236`. Ledger `worker_dead`. |
| [`t750-r2-notfixed`](#t750-r2-notfixed) | 18h 56m | `None` | `grok-code` | pid `28414` **dead** | pinned | Adversarial NOT-FIXED review of an uncommitted changeset (`docs-private/review/t750/changeset.diff`). Target: the Dropbox working tree as it stood when queued — firing later reviews whatever is dirty now. |
| [`webqa-D-leaderboard-0827`](#webqa-D-leaderboard-0827) | 12h 18m | `regolith-engine` | `grok-research` | pid `73159` **dead** (quarantine) | pinned | Web QA surface D: optimizer leaderboard (b-089); regenerate findings that were never written to disk. Quarantined. Target: Dropbox regolith tree. |

## Entries

### `b234-mre-scope`

- **TLDR:** Scope whether the MRE charge-accounting fail-open (b-234, `simulator/extraction.py` ~2113) fires on any golden feedstock. Target: `/Users/simonrowland/Repos/rps-b234` branch `work-b234` — NOT the Dropbox main tree. Stale-target risk is exactly the regolith worked example.
- **Pinned prompt:** [`prompts/b234-mre-scope.md`](prompts/b234-mre-scope.md)
- **Project name:** regolith
- **Project root:** `/Users/simonrowland/Library/CloudStorage/Dropbox/Starship Mission Design/Regolith Processing/regolith-pyrolysis-simulator`
- **Target cwd:** `/Users/simonrowland/Repos/rps-b234`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `queued`
- **Agent:** `moonshot`
- **Created at / age:** `2026-08-26T17:12:09+00:00` / **21h 41m**
- **Owner label:** `regolith-main`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Library-CloudStorage-Dropbox-Starship-Mission-Design-Regolith-Processing-regolith-pyrolysis-simulator/70f1d3c1-4b7e-4455-bf2d-31eabd2ee767/scratchpad/b234.prompt` — EXISTS on disk
- **Prompt pin:** **pinnable** (3524 chars)
- **Task ids:** `['b-234']`
- **Base SHA:** `31e90d85996700d88ed3b8a083699832c438c969`
- **Populations:** `bare-json`, `ledger-nonterminal`, `queue-top`

### `magemin-status-review`

- **TLDR:** Review an uncommitted MagmaMin bugfix (`engines/magemin/`) on branch `engine-2026-08-16`. Target: Dropbox working-tree diff. Also copied under retired-by-engine.
- **Pinned prompt:** [`prompts/magemin-status-review.md`](prompts/magemin-status-review.md)
- **Project name:** regolith
- **Project root:** `/Users/simonrowland/Library/CloudStorage/Dropbox/Starship Mission Design/Regolith Processing/regolith-pyrolysis-simulator`
- **Target cwd:** `/Users/simonrowland/Library/CloudStorage/Dropbox/Starship Mission Design/Regolith Processing/regolith-pyrolysis-simulator`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `queued`
- **Agent:** `codex`
- **Created at / age:** `2026-08-26T16:55:16+00:00` / **21h 58m**
- **Owner label:** `None`
- **Claim marker:** pid `21784` **dead**
- **Prompt source:** inline text (pinned)
- **Prompt pin:** **pinnable** (4127 chars)
- **Task ids:** `[]`
- **Base SHA:** `31e90d85996700d88ed3b8a083699832c438c969`
- **Populations:** `bare-json`, `claim-marker`, `ledger-nonterminal`, `queue-top`, `retired-by-engine`

### `sr2-closure`

- **TLDR:** Closure review of a round-2 bugfix on branch `engine-2026-08-16` (uncommitted tree + untracked test). Claim-only, no surviving bare json.
- **Pinned prompt:** [`prompts/sr2-closure.md`](prompts/sr2-closure.md)
- **Project name:** regolith
- **Project root:** `/Users/simonrowland/Library/CloudStorage/Dropbox/Starship Mission Design/Regolith Processing/regolith-pyrolysis-simulator`
- **Target cwd:** `/Users/simonrowland/Library/CloudStorage/Dropbox/Starship Mission Design/Regolith Processing/regolith-pyrolysis-simulator`
- **Queue payload state:** `claimed`
- **Ledger state (raw):** `queued`
- **Agent:** `codex`
- **Created at / age:** `2026-08-26T17:36:35+00:00` / **21h 17m**
- **Owner label:** `regolith-engine`
- **Claim marker:** pid `32218` **dead**
- **Prompt source:** inline text (pinned)
- **Prompt pin:** **pinnable** (3915 chars)
- **Task ids:** `[]`
- **Base SHA:** `31e90d85996700d88ed3b8a083699832c438c969`
- **Populations:** `claim-marker`, `ledger-nonterminal`

### `status-r2-closure`

- **TLDR:** Same round-2 closure review as sr2-closure. Quarantined claim marker (pid dead). Ledger `failed`.
- **Pinned prompt:** [`prompts/status-r2-closure.md`](prompts/status-r2-closure.md)
- **Project name:** regolith
- **Project root:** `/Users/simonrowland/Library/CloudStorage/Dropbox/Starship Mission Design/Regolith Processing/regolith-pyrolysis-simulator`
- **Target cwd:** `/Users/simonrowland/Library/CloudStorage/Dropbox/Starship Mission Design/Regolith Processing/regolith-pyrolysis-simulator`
- **Queue payload state:** `quarantined`
- **Ledger state (raw):** `failed`
- **Agent:** `codex`
- **Created at / age:** `2026-08-26T17:34:21+00:00` / **21h 19m**
- **Owner label:** `regolith-engine`
- **Claim marker:** pid `6554` **dead** (quarantine)
- **Prompt source:** `--prompt-file` `/tmp/goal-flight-501/dispatch/status-r2-closure.assembled.prompt` — EXISTS on disk
- **Prompt pin:** **pinnable** (5044 chars)
- **Task ids:** `[]`
- **Base SHA:** `31e90d85996700d88ed3b8a083699832c438c969`
- **Populations:** `ledger-terminal`, `quarantine`
- **Hints (not verdicts):** HINT: ledger already records terminal state `failed` (not a queue-purge verdict)

### `status-r2-notfixed`

- **TLDR:** Adversarial NOT-FIXED review, round 2, of the uncommitted `engine-2026-08-16` tree. Also copied under retired-by-engine.
- **Pinned prompt:** [`prompts/status-r2-notfixed.md`](prompts/status-r2-notfixed.md)
- **Project name:** regolith
- **Project root:** `/Users/simonrowland/Library/CloudStorage/Dropbox/Starship Mission Design/Regolith Processing/regolith-pyrolysis-simulator`
- **Target cwd:** `/Users/simonrowland/Library/CloudStorage/Dropbox/Starship Mission Design/Regolith Processing/regolith-pyrolysis-simulator`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `queued`
- **Agent:** `grok-code`
- **Created at / age:** `2026-08-26T17:33:45+00:00` / **21h 20m**
- **Owner label:** `None`
- **Claim marker:** pid `97635` **dead**
- **Prompt source:** inline text (pinned)
- **Prompt pin:** **pinnable** (3982 chars)
- **Task ids:** `[]`
- **Base SHA:** `31e90d85996700d88ed3b8a083699832c438c969`
- **Populations:** `bare-json`, `claim-marker`, `ledger-nonterminal`, `queue-top`, `retired-by-engine`

### `t481-registry-decide`

- **TLDR:** Decide the fate of a fully-built, never-read phase-aware volatile-property registry (t-481). Target: `/Users/simonrowland/Repos/rps-t481` branch `work-t481` (HEAD 236553f9), not the Dropbox main tree.
- **Pinned prompt:** [`prompts/t481-registry-decide.md`](prompts/t481-registry-decide.md)
- **Project name:** regolith
- **Project root:** `/Users/simonrowland/Library/CloudStorage/Dropbox/Starship Mission Design/Regolith Processing/regolith-pyrolysis-simulator`
- **Target cwd:** `/Users/simonrowland/Repos/rps-t481`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `queued`
- **Agent:** `moonshot`
- **Created at / age:** `2026-08-26T17:11:20+00:00` / **21h 42m**
- **Owner label:** `regolith-main`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Library-CloudStorage-Dropbox-Starship-Mission-Design-Regolith-Processing-regolith-pyrolysis-simulator/70f1d3c1-4b7e-4455-bf2d-31eabd2ee767/scratchpad/t481.prompt` — EXISTS on disk
- **Prompt pin:** **pinnable** (2838 chars)
- **Task ids:** `['t-481']`
- **Base SHA:** `31e90d85996700d88ed3b8a083699832c438c969`
- **Populations:** `bare-json`, `ledger-nonterminal`, `queue-top`

### `t697-dunite`

- **TLDR:** Diagnose an unexplained residual in the dunite melt-activity benchmark. Target: Dropbox `regolith-pyrolysis-simulator` working tree. Also copied under retired-by-engine.
- **Pinned prompt:** [`prompts/t697-dunite.md`](prompts/t697-dunite.md)
- **Project name:** regolith
- **Project root:** `/Users/simonrowland/Library/CloudStorage/Dropbox/Starship Mission Design/Regolith Processing/regolith-pyrolysis-simulator`
- **Target cwd:** `/Users/simonrowland/Library/CloudStorage/Dropbox/Starship Mission Design/Regolith Processing/regolith-pyrolysis-simulator`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `queued`
- **Agent:** `grok-code`
- **Created at / age:** `2026-08-26T16:47:05+00:00` / **22h 7m**
- **Owner label:** `None`
- **Claim marker:** pid `11223` **dead**
- **Prompt source:** inline text (pinned)
- **Prompt pin:** **pinnable** (3621 chars)
- **Task ids:** `[]`
- **Base SHA:** `31e90d85996700d88ed3b8a083699832c438c969`
- **Populations:** `bare-json`, `claim-marker`, `ledger-nonterminal`, `queue-top`, `retired-by-engine`

### `t743-exclusion-audit`

- **TLDR:** Exclusion audit: does the validation battery only discard data that flatters the model? (task_ids t-754 / KEMS). Target: Dropbox `regolith-pyrolysis-simulator` tree.
- **Pinned prompt:** [`prompts/t743-exclusion-audit.md`](prompts/t743-exclusion-audit.md)
- **Project name:** regolith
- **Project root:** `/Users/simonrowland/Library/CloudStorage/Dropbox/Starship Mission Design/Regolith Processing/regolith-pyrolysis-simulator`
- **Target cwd:** `/Users/simonrowland/Library/CloudStorage/Dropbox/Starship Mission Design/Regolith Processing/regolith-pyrolysis-simulator`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `queued`
- **Agent:** `moonshot`
- **Created at / age:** `2026-08-26T17:39:38+00:00` / **21h 14m**
- **Owner label:** `regolith-main`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Library-CloudStorage-Dropbox-Starship-Mission-Design-Regolith-Processing-regolith-pyrolysis-simulator/70f1d3c1-4b7e-4455-bf2d-31eabd2ee767/scratchpad/briefs/t743-selection.md` — EXISTS on disk
- **Prompt pin:** **pinnable** (8702 chars)
- **Task ids:** `['t-754']`
- **Base SHA:** `31e90d85996700d88ed3b8a083699832c438c969`
- **Populations:** `bare-json`, `ledger-nonterminal`, `queue-top`

### `t748-stage-pressures`

- **TLDR:** Derive per-species STAGE partial pressure and whether each condenser captures its species (t-748). Target: `/Users/simonrowland/Repos/rps-b236` branch `work-b236`. Ledger `worker_dead`.
- **Pinned prompt:** [`prompts/t748-stage-pressures.md`](prompts/t748-stage-pressures.md)
- **Project name:** regolith
- **Project root:** `/Users/simonrowland/Library/CloudStorage/Dropbox/Starship Mission Design/Regolith Processing/regolith-pyrolysis-simulator`
- **Target cwd:** `/Users/simonrowland/Repos/rps-b236`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `worker_dead`
- **Agent:** `moonshot`
- **Created at / age:** `2026-08-26T16:43:32+00:00` / **22h 10m**
- **Owner label:** `regolith-main`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/private/tmp/claude-501/-Users-simonrowland-Library-CloudStorage-Dropbox-Starship-Mission-Design-Regolith-Processing-regolith-pyrolysis-simulator/70f1d3c1-4b7e-4455-bf2d-31eabd2ee767/scratchpad/t748.prompt` — EXISTS on disk
- **Prompt pin:** **pinnable** (4750 chars)
- **Task ids:** `['t-748']`
- **Base SHA:** `31e90d85996700d88ed3b8a083699832c438c969`
- **Populations:** `bare-json`, `ledger-terminal`, `queue-top`
- **Hints (not verdicts):** HINT: ledger already records terminal state `worker_dead` (not a queue-purge verdict)

### `t750-r2-notfixed`

- **TLDR:** Adversarial NOT-FIXED review of an uncommitted changeset (`docs-private/review/t750/changeset.diff`). Target: the Dropbox working tree as it stood when queued — firing later reviews whatever is dirty now.
- **Pinned prompt:** [`prompts/t750-r2-notfixed.md`](prompts/t750-r2-notfixed.md)
- **Project name:** regolith
- **Project root:** `/Users/simonrowland/Library/CloudStorage/Dropbox/Starship Mission Design/Regolith Processing/regolith-pyrolysis-simulator`
- **Target cwd:** `/Users/simonrowland/Library/CloudStorage/Dropbox/Starship Mission Design/Regolith Processing/regolith-pyrolysis-simulator`
- **Queue payload state:** `restore_prepared`
- **Ledger state (raw):** `queued`
- **Agent:** `grok-code`
- **Created at / age:** `2026-08-26T19:57:57+00:00` / **18h 56m**
- **Owner label:** `None`
- **Claim marker:** pid `28414` **dead**
- **Prompt source:** inline text (pinned)
- **Prompt pin:** **pinnable** (2199 chars)
- **Task ids:** `[]`
- **Base SHA:** `4b8a80477dfd2dbe24ab985a2ae5e9933c37c349`
- **Populations:** `bare-json`, `claim-marker`, `ledger-nonterminal`, `queue-top`

### `webqa-D-leaderboard-0827`

- **TLDR:** Web QA surface D: optimizer leaderboard (b-089); regenerate findings that were never written to disk. Quarantined. Target: Dropbox regolith tree.
- **Pinned prompt:** [`prompts/webqa-D-leaderboard-0827.md`](prompts/webqa-D-leaderboard-0827.md)
- **Project name:** regolith
- **Project root:** `/Users/simonrowland/Library/CloudStorage/Dropbox/Starship Mission Design/Regolith Processing/regolith-pyrolysis-simulator`
- **Target cwd:** `/Users/simonrowland/Library/CloudStorage/Dropbox/Starship Mission Design/Regolith Processing/regolith-pyrolysis-simulator`
- **Queue payload state:** `quarantined`
- **Ledger state (raw):** `failed`
- **Agent:** `grok-research`
- **Created at / age:** `2026-08-27T02:35:51+00:00` / **12h 18m**
- **Owner label:** `regolith-engine`
- **Claim marker:** pid `73159` **dead** (quarantine)
- **Prompt source:** `--prompt-file` `/tmp/goal-flight-501/dispatch/webqa-D-leaderboard-0827.assembled.prompt` — EXISTS on disk
- **Prompt pin:** **pinnable** (5308 chars)
- **Task ids:** `[]`
- **Base SHA:** `088807f150ce4675751305b4f80467bb16971f8c`
- **Populations:** `ledger-terminal`, `quarantine`
- **Hints (not verdicts):** HINT: ledger already records terminal state `failed` (not a queue-purge verdict)

