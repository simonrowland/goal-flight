---
name: goal-flight
version: 0.4.0-dev
description: "long-running unattended controller for chunked code work — init repo, decompose plan, anticipate questions, execute with embedded review and milestone gstack sweeps"
allowed-tools:
  - Bash
  - Read
  - Edit
  - Write
  - Glob
  - Grep
  - Agent
  - Skill
  - TodoWrite
  - AskUserQuestion
triggers:
  - /goal-flight
  - start a long refactor
  - begin chunked work
  - set up controller for unattended run
  - decompose this plan into goal chunks
---

Use this skill when the user wants Claude Code to manage code work that is too
large for one uninterrupted session: decomposed implementation, long refactors,
review flights, resumable queues, or unattended executor dispatch.

## Controller Contract

The controller manages context and verification. It does not hoard every file,
log, or worker transcript in the conversation.

Always:

- read the invoked `commands/*.md` file
- read only the protocols that command references
- run procedural helpers for machine facts, status, logs, capacity, and tool
  probes
- keep raw logs and long review output in files
- reason over compact JSON/status summaries

Never load fork, ACP, corpus, review, or tool-specific details just because the
skill loaded. Load those protocols on demand.

## Session Pre-Flight

For non-trivial commands, use `protocols/session-preflight.md`.

First preference:

```bash
python3 <skill-root>/scripts/goalflight_status.py --json
```

Use doctor when readiness is unknown or changed:

```bash
python3 <skill-root>/scripts/goalflight_doctor.py --project-root "$PWD" --json
```

Surface only actionable warnings: install ambiguity, missing required tool,
capacity cooldown, stale dispatch, surplus worker-like process, or fingerprint
drift against an in-flight queue.

## Commands

| Command | File | Required protocols |
|---|---|---|
| `/goal-flight init <topic>` | `commands/init.md` | `session-preflight`, `tool-readiness`, `premises`, `state-handoff` |
| `/goal-flight decompose-plan [<plan>]` | `commands/decompose-plan.md` | `premises`, `dispatch-routing` |
| `/goal-flight ask-questions [<scope>]` | `commands/ask-questions.md` | `dispatch-routing` |
| `/goal-flight execute [--parallel <N>]` | `commands/execute.md` | `dispatch-routing`, `worker-markers`, `state-handoff`, `milestone-review`; add `worktrees-parallel` for `--parallel` |
| `/goal-flight doctor` | `commands/doctor.md` | `tool-readiness` |
| `/goal-flight build-corpus [<flags>]` | `commands/build-corpus.md` | corpus docs referenced there |
| `/goal-flight resume` | `commands/resume.md` | `state-handoff` |
| `/goal-flight goal <SLUG>` | `commands/goal.md` | none |
| `/goal-flight register-codex [<path>]` | `commands/register-codex.md` | `tool-readiness` |
| `/goal-flight update` | `commands/update.md` | `tool-readiness` |
| `/goal-flight validate-dispatch [<slug>]` | `commands/validate-dispatch.md` | `dispatch-routing`, `worker-markers` |
| `/goal-flight validate-queue [<path>]` | `commands/validate-queue.md` | none |

Protocol index: `protocols/README.md`.

## Hard Invariants

- Verification first. Every executor prompt starts by checking repo state,
  target files, and assumptions before editing.
- Background anything expected to run longer than 10 seconds.
- No `tail -f` in the conversation. Use status files and
  `scripts/goalflight_watch.py`.
- No worker spawn without capacity consideration. Use
  `scripts/goalflight_capacity.py` directly or through the runner scripts.
- Every long worker or review job needs a ledger/status path.
- Missing or stalled review is inconclusive, not clean.
- Ask the user only for real product/permission blockers, destructive choices,
  or irreducible ambiguity.
- Keep `docs-private/` private. Public docs describe shipped behavior, not
  private review scratch.

## Dispatch Model

Two orthogonal axes. Pick one from each.

- **Iteration pattern**: `one-shot` (default) or `goal-mode loop` (iterative,
  for review-revise cycles or chunks that exceed one turn).
- **Comms shape**: `controller-direct` (tiny local edit, no spawn), `acp`
  (structured JSON-RPC, default when an adapter exists), or `bash-tail`
  (flat stdout watcher, fallback only).

Most compositions work. `goal-mode + bash-tail` requires a worker that emits
a detectable end-of-goal marker in the flat tail (so the watcher knows the
loop is complete). As of 2026-05-19, codex `/goal` is the only worker known
to qualify — via its structured "Final response" block. Grok and claude
headless do not qualify yet; a future worker that gains an equivalent marker
would join this cell. See `protocols/dispatch-routing.md` for the full table.

## Worker Routing

The controller is a Claude session; Claude Agent-tool subagents share its
rate-limit budget. Codex/grok/cursor workers consume their own provider
budgets and do not. Default routing by task:

| Task | Default | Fallback 1 | Fallback 2 |
|---|---|---|---|
| Code-writing chunks | codex (ACP) or cursor (ACP) — controller picks per chunk character | grok (ACP) | Claude Agent |
| Reviewer dispatches | gstack `/review` via codex + concern-diverse sweep (grok / cursor) | any one alone | Claude Agent (only when other reviewers unreachable) |
| Planning / decompose | codex | controller-direct | Claude Agent |
| Anticipatory questions | Claude Agent (its interactive strength) | controller-direct | — |
| Analysis / reflection | controller-direct | — | — |
| Voice-sensitive prose | Claude Agent (controller judgment per chunk) | — | — |

**Cursor note (2026-05-19)**: cursor-agent shipped a major model update
benchmarking on par with Claude Opus for coding. It joins codex as a
first-tier code-writing worker (both ACP-reachable, both sub-billed —
neither consumes the controller's Claude budget). Default routing now
treats them as co-equal choices; the controller picks per chunk
character (codex `/goal` for iterative workhorses; cursor for chunks
where Claude-like fluency matters and codex would over-engineer).

**Cursor model selection**: prefer cursor's leading internal (`composer-*`)
model — it's covered by the Cursor subscription's unlimited internal-model
tier. Cursor also exposes passthrough models (`gpt-*-xhigh`,
`claude-opus-*-thinking-high`, etc.) but those bill against the
subscription's paid-passthrough budget and burn it fast. Reserve passthrough
for chunks that specifically need those vendors.

**Discovery, not hardcoding**: cursor ships new models often. Don't paste a
model name from yesterday's recipe — ask `cursor-agent --list-models` for
the current set, or read the doctor probe:

```bash
python3 <skill-root>/scripts/goalflight_doctor.py --project-root "$PWD" --json | \
  python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["cursor"]["models"])'
```

The probe returns:
- `leading_internal` — highest-versioned `composer-X.Y` (non-`-fast`)
- `all_internal` — full composer-* list in cursor's listing order
- `current_user_model` — what `~/.cursor/cli-config.json` is set to
- `user_behind` — True when the user is on an older internal model or on a
  passthrough model (which burns paid budget)

The `cursor-agent acp` subcommand has no `--model` flag; the model is read
from `~/.cursor/cli-config.json`'s `modelId`. To update, edit:

```json
"model": {
  "modelId": "<leading_internal from doctor>",
  "displayModelId": "<same>",
  "displayName": "<human-readable>",
  "displayNameShort": "<same>",
  "aliases": [],
  "maxMode": false
},
"hasChangedDefaultModel": true
```

Cursor's internal models don't have a separate "xhigh" effort tier;
xhigh variants exist only on the OpenAI-passthrough models. Use the
plain `composer-X.Y` ID returned by the probe.

**Failover.** If a Claude Agent dispatch fails with a rate-limit signal,
re-dispatch the same chunk to codex or grok; don't retry Claude until the
documented reset window passes.

**Hard caps** live in `scripts/goalflight_capacity.py` (`DEFAULT_AGENT_CAPS`).
Per-agent caps are deliberately generous (claude=5, codex=10, grok=10,
cursor-agent=5) to support multi-session parallel work. The per-label caps
are **process-count** caps (RAM-aware); rate limits are provider-level (one
provider may have multiple agent labels — e.g., `codex` and `codex-acp`
share the same OpenAI budget; `cursor` and `cursor-agent` share the same
Cursor subscription).

**Adaptive walkback**: `scripts/goalflight_rate_pressure.py` reads the
dispatch ledger, classifies failures by provider, and emits a JSON
recommendation when 3+ rate-limit signatures hit the same provider within
10 minutes. Controllers should consult it before dispatching:

```bash
python3 <skill-root>/scripts/goalflight_rate_pressure.py
```

The recommendation includes halved-cap suggestions and per-provider
fallback chains. The script is read-only — the controller decides whether
to act on the recommendation (re-route the next chunk to a fallback
provider, surface a STATUS marker, etc.). No autonomous mutation of
capacity state in v1.

Bash-tail recipes live in `protocols/legacy/bash-tail.md` and load only
when ACP isn't viable. Forking lives in `protocols/self-delegation.md` and
is loaded only when explicitly needed.

## Worker Markers

Workers communicate with one-line markers:

- `STATUS:`
- `RESULT:`
- `USER-NEED:`
- `USER-CONFIRM:`
- `BLOCKED:`
- `COMPLETE:`

Details live in `protocols/worker-markers.md`.

## State

Use three state layers:

- project: git, tests, docs, queue
- machine: capacity leases, dispatch ledger, cooldowns
- conversation: current decisions and unresolved questions

On resume or after sleep, start from:

```bash
python3 <skill-root>/scripts/goalflight_status.py --json
```

Details live in `protocols/state-handoff.md`.

## Context Discipline

Read for edits narrowly. Analyze/search/count/filter with procedural code or
context-mode. Store long artifacts in files and return paths plus summaries.

When in doubt, move deterministic logic into `scripts/goalflight_*.py` and keep
the model responsible for judgment: choosing next action, interpreting findings,
and deciding whether a warning matters.

## Do Not

- Do not paste long logs, diffs, JSONL streams, or review transcripts.
- Do not treat PID alone as process identity.
- Do not let one goal-flight session consume all machine capacity.
- Do not silently skip Claude/Codex/Grok review when a provider hits rate or
  session limits.
- Do not load `/fork` instructions by default.
