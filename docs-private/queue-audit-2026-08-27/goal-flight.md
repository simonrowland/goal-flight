# Queue inventory for **goal-flight** — 2026-08-27

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


This list is for the goal-flight controller. Six `acp-*` rows are ledger leftovers of launch-path probes whose prompt file is gone. `t372-w1` is the worker that wrote this inventory. `rev-t371` is a live review of the reconcile-abandoned identity probe.

See [INDEX.md](INDEX.md) for population counts and method. This file lists **8 / 81** union dispatch ids whose `project_root` (or cwd) belongs to this project.

Prompts: **2 / 8** pinnable, **6 / 8** missing.

## Quick list

| dispatch_id | age | owner | agent | claim pid | prompt | TLDR |
|---|---|---|---|---|---|---|
| [`acp-async-launch`](#acp-async-launch) | 1d 2h 53m | `None` | `codex-acp` | none | **MISSING** | Leftover ACP launch-path probe (ledger-only). Prompt path is relative `chunk.md` and is gone. Target: goal-flight repo. Honest default: abandon. |
| [`acp-launch-retry`](#acp-launch-retry) | 1d 2h 53m | `None` | `codex-acp` | none | **MISSING** | Leftover ACP launch-retry probe (ledger-only). Prompt path `chunk.md` is gone. Target: goal-flight repo. Honest default: abandon. |
| [`acp-launch-unconfirmed`](#acp-launch-unconfirmed) | 1d 2h 53m | `None` | `codex-acp` | none | **MISSING** | Leftover ACP launch_unconfirmed probe (ledger-only). Prompt path `chunk.md` is gone. Target: goal-flight repo. Honest default: abandon. |
| [`acp-ledger-lease`](#acp-ledger-lease) | 1d 2h 53m | `None` | `codex-acp` | none | **MISSING** | Leftover ACP ledger-lease probe (ledger-only). Prompt path `chunk.md` is gone. Target: goal-flight repo. Honest default: abandon. |
| [`acp-live-runner`](#acp-live-runner) | 1d 2h 53m | `None` | `codex-acp` | none | **MISSING** | Leftover ACP live-runner probe (ledger-only). Prompt path `chunk.md` is gone. Target: goal-flight repo. Honest default: abandon. |
| [`acp-node-ssh`](#acp-node-ssh) | 1d 2h 53m | `None` | `codex-acp` | none | **MISSING** | Leftover ACP node-SSH probe (ledger-only). Prompt path `chunk.md` is gone. Target: goal-flight repo. Honest default: abandon. |
| [`rev-t371`](#rev-t371) | 8m | `goal-flight` | `grok-code` | none | pinned | Review of t-371 reconcile-abandoned identity probe (55bcb7f / 76942df). Target: worktree t371-reconcile; findings-file only. Ledger worker pid was alive at inventory. |
| [`t372-w1`](#t372-w1) | 5m | `goal-flight` | `grok-code` | none | pinned | THIS inventory worker (t-372). Target: worktree `t372-queue` @ 27fb24f. In-flight; not a stuck queue entry. |

## Entries

### `acp-async-launch`

- **TLDR:** Leftover ACP launch-path probe (ledger-only). Prompt path is relative `chunk.md` and is gone. Target: goal-flight repo. Honest default: abandon.
- **Pinned prompt:** [`prompts/acp-async-launch.md`](prompts/acp-async-launch.md)
- **Project name:** goal-flight
- **Project root:** `/Users/simonrowland/Repos/goal-flight`
- **Target cwd:** `None`
- **Queue payload state:** `running`
- **Ledger state (raw):** `running`
- **Agent:** `codex-acp`
- **Created at / age:** `2026-08-26T12:00:07+00:00` / **1d 2h 53m**
- **Owner label:** `None`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `chunk.md` — MISSING on disk
- **Prompt pin:** **missing** (0 chars)
- **Task ids:** `None`
- **Base SHA:** `None`
- **Populations:** `ledger-nonterminal`
- **Hints (not verdicts):** HINT: prompt-file is gone — premise cannot be re-checked; honest default is abandon

### `acp-launch-retry`

- **TLDR:** Leftover ACP launch-retry probe (ledger-only). Prompt path `chunk.md` is gone. Target: goal-flight repo. Honest default: abandon.
- **Pinned prompt:** [`prompts/acp-launch-retry.md`](prompts/acp-launch-retry.md)
- **Project name:** goal-flight
- **Project root:** `/Users/simonrowland/Repos/goal-flight`
- **Target cwd:** `None`
- **Queue payload state:** `running`
- **Ledger state (raw):** `running`
- **Agent:** `codex-acp`
- **Created at / age:** `2026-08-26T12:00:07+00:00` / **1d 2h 53m**
- **Owner label:** `None`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `chunk.md` — MISSING on disk
- **Prompt pin:** **missing** (0 chars)
- **Task ids:** `None`
- **Base SHA:** `None`
- **Populations:** `ledger-nonterminal`
- **Hints (not verdicts):** HINT: prompt-file is gone — premise cannot be re-checked; honest default is abandon

### `acp-launch-unconfirmed`

- **TLDR:** Leftover ACP launch_unconfirmed probe (ledger-only). Prompt path `chunk.md` is gone. Target: goal-flight repo. Honest default: abandon.
- **Pinned prompt:** [`prompts/acp-launch-unconfirmed.md`](prompts/acp-launch-unconfirmed.md)
- **Project name:** goal-flight
- **Project root:** `/Users/simonrowland/Repos/goal-flight`
- **Target cwd:** `None`
- **Queue payload state:** `launch_unconfirmed`
- **Ledger state (raw):** `launch_unconfirmed`
- **Agent:** `codex-acp`
- **Created at / age:** `2026-08-26T12:00:07+00:00` / **1d 2h 53m**
- **Owner label:** `None`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `chunk.md` — MISSING on disk
- **Prompt pin:** **missing** (0 chars)
- **Task ids:** `None`
- **Base SHA:** `None`
- **Populations:** `ledger-nonterminal`
- **Hints (not verdicts):** HINT: prompt-file is gone — premise cannot be re-checked; honest default is abandon

### `acp-ledger-lease`

- **TLDR:** Leftover ACP ledger-lease probe (ledger-only). Prompt path `chunk.md` is gone. Target: goal-flight repo. Honest default: abandon.
- **Pinned prompt:** [`prompts/acp-ledger-lease.md`](prompts/acp-ledger-lease.md)
- **Project name:** goal-flight
- **Project root:** `/Users/simonrowland/Repos/goal-flight`
- **Target cwd:** `None`
- **Queue payload state:** `running`
- **Ledger state (raw):** `running`
- **Agent:** `codex-acp`
- **Created at / age:** `2026-08-26T12:00:07+00:00` / **1d 2h 53m**
- **Owner label:** `None`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `chunk.md` — MISSING on disk
- **Prompt pin:** **missing** (0 chars)
- **Task ids:** `None`
- **Base SHA:** `None`
- **Populations:** `ledger-nonterminal`
- **Hints (not verdicts):** HINT: prompt-file is gone — premise cannot be re-checked; honest default is abandon

### `acp-live-runner`

- **TLDR:** Leftover ACP live-runner probe (ledger-only). Prompt path `chunk.md` is gone. Target: goal-flight repo. Honest default: abandon.
- **Pinned prompt:** [`prompts/acp-live-runner.md`](prompts/acp-live-runner.md)
- **Project name:** goal-flight
- **Project root:** `/Users/simonrowland/Repos/goal-flight`
- **Target cwd:** `None`
- **Queue payload state:** `running`
- **Ledger state (raw):** `running`
- **Agent:** `codex-acp`
- **Created at / age:** `2026-08-26T12:00:07+00:00` / **1d 2h 53m**
- **Owner label:** `None`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `chunk.md` — MISSING on disk
- **Prompt pin:** **missing** (0 chars)
- **Task ids:** `None`
- **Base SHA:** `None`
- **Populations:** `ledger-nonterminal`
- **Hints (not verdicts):** HINT: prompt-file is gone — premise cannot be re-checked; honest default is abandon

### `acp-node-ssh`

- **TLDR:** Leftover ACP node-SSH probe (ledger-only). Prompt path `chunk.md` is gone. Target: goal-flight repo. Honest default: abandon.
- **Pinned prompt:** [`prompts/acp-node-ssh.md`](prompts/acp-node-ssh.md)
- **Project name:** goal-flight
- **Project root:** `/Users/simonrowland/Repos/goal-flight`
- **Target cwd:** `None`
- **Queue payload state:** `running`
- **Ledger state (raw):** `running`
- **Agent:** `codex-acp`
- **Created at / age:** `2026-08-26T12:00:07+00:00` / **1d 2h 53m**
- **Owner label:** `None`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `chunk.md` — MISSING on disk
- **Prompt pin:** **missing** (0 chars)
- **Task ids:** `None`
- **Base SHA:** `None`
- **Populations:** `ledger-nonterminal`
- **Hints (not verdicts):** HINT: prompt-file is gone — premise cannot be re-checked; honest default is abandon

### `rev-t371`

- **TLDR:** Review of t-371 reconcile-abandoned identity probe (55bcb7f / 76942df). Target: worktree t371-reconcile; findings-file only. Ledger worker pid was alive at inventory.
- **Pinned prompt:** [`prompts/rev-t371.md`](prompts/rev-t371.md)
- **Project name:** goal-flight
- **Project root:** `/Users/simonrowland/Repos/goal-flight`
- **Target cwd:** `None`
- **Queue payload state:** `running`
- **Ledger state (raw):** `running`
- **Agent:** `grok-code`
- **Created at / age:** `2026-08-27T14:45:22+00:00` / **8m**
- **Owner label:** `goal-flight`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/tmp/goal-flight-501/dispatch/rev-t371.assembled.prompt` — EXISTS on disk
- **Prompt pin:** **pinnable** (5248 chars)
- **Task ids:** `None`
- **Base SHA:** `None`
- **Populations:** `ledger-nonterminal`

### `t372-w1`

- **TLDR:** THIS inventory worker (t-372). Target: worktree `t372-queue` @ 27fb24f. In-flight; not a stuck queue entry.
- **Pinned prompt:** [`prompts/t372-w1.md`](prompts/t372-w1.md)
- **Project name:** goal-flight
- **Project root:** `/Users/simonrowland/Repos/goal-flight`
- **Target cwd:** `None`
- **Queue payload state:** `running`
- **Ledger state (raw):** `running`
- **Agent:** `grok-code`
- **Created at / age:** `2026-08-27T14:49:05+00:00` / **5m**
- **Owner label:** `goal-flight`
- **Claim marker:** none
- **Prompt source:** `--prompt-file` `/tmp/goal-flight-501/dispatch/t372-w1.assembled.prompt` — EXISTS on disk
- **Prompt pin:** **pinnable** (6917 chars)
- **Task ids:** `None`
- **Base SHA:** `None`
- **Populations:** `ledger-nonterminal`
- **Hints (not verdicts):** HINT: this is the in-flight inventory worker itself (t372-w1), not a stuck queue entry

