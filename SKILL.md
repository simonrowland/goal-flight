---
name: goal-flight
version: 1.0.0
description: "Portable Goal Flight controller workflow for planning, dispatching, reviewing, recovering, and resuming long-running repository work from file-backed state."
tags:
  - orchestration
  - controller
  - dispatch
  - review
  - handoff
paths:
  commands: commands/
  protocols: protocols/
  scripts: scripts/
  adapters: adapters/
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

This checked-in `SKILL.md` is the current Claude Code-compatible wrapper for
the portable goal-flight core. Keep this front matter and `allowed-tools`
surface compatible with Claude Code until generated host wrappers land. The
portable core is the command/protocol/script/adapter surface; adapter manifests
own host bindings, host tool names, invocation details, and wrapper packaging.

Use this wrapper when a controller must manage code work that is too large for
one uninterrupted session: decomposed implementation, long refactors, review
flights, resumable queues, or unattended executor dispatch.

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
| `/goal-flight execute [--parallel <N>]` | `commands/execute.md` | `dispatch-routing`, `worker-markers`, `state-handoff`, `user-status-cadence`, `chunk-review`, `milestone-review`; add `worktrees-parallel` for `--parallel` |
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
- During an active goal-flight run, **keep shipping** through decompose →
  execute until the queue is done or a real blocker stops you — do not pause
  for engagement checks (see **Autonomous throughput** below).
- While workers or review jobs are in-flight, **report progress to the user**
  at least every 15 minutes unless context is tight — see
  `protocols/user-status-cadence.md`. That is status reporting, not asking
  the user to confirm continuation.
- Keep `docs-private/` private. Public docs describe shipped behavior, not
  private review scratch.

## Autonomous throughput

Goal Flight exists so long work survives compactions and unattended hours.
The controller's job is to **advance the queue**, not to poll the user for
presence.

When the user has invoked a goal-flight command, approved a plan, or given a
substantive scope instruction (for example "implement Wave 1", "keep going",
"let's implement"):

- **Keep working** through the next durable artifacts: code, tests green,
  queue/ledger/resume notes updated, and **commits for each completed chunk**
  or logical atomic unit until decomposition is finished and execute drains
  the queue.
- **Default is continue**, not confirm. The next step implied by the plan,
  RESUME-NOTES, or goal-queue is authorization enough.
- **Do not use engagement prompts** when no actually-important user direction
  is required. Anti-patterns:
  - "Are you still there?"
  - "Want me to continue?"
  - "Say the word if you want me to …"
  - Ending a turn with optional follow-ups that stall work the user already
    scoped
- **Record non-blocking uncertainty in files**, not chat: inline-office-hours
  backlog, `docs-private/premises-*.md`, RESUME-NOTES, or scenario `notes` —
  then proceed with the best default from the plan.
- **Mid-session user requests:** when goal-flight is already in play and the
  user adds scope or a new ask, append a compact row to the active
  `docs-private/goal-queue-*.md` via `commands/goal.md` **before** dispatch or
  implementation — chat alone is not the backlog.
- **Stop and ask only** for `USER-NEED` / `USER-CONFIRM` tier blockers: permission,
  destructive or irreversible action without a plan default, product choice the
  plan cannot infer, auth/capacity hard stop, or explicit command gates (for
  example decompose-plan step 6 before launching execute).

Commits during execute follow **one commit per completed chunk** (plus
milestone fix-clusters) unless the user forbade commits for this run. That is
part of the active workflow, not a separate permission request per chunk.

Before each chunk commit: focused tests green **and** at least one independent
review per `protocols/chunk-review.md` (default gstack `/review`, with
`./scripts/autoreview.sh` as a complementary parallel option for diff-local
findings — controller decides per chunk; background if >10s).

At milestone cadence or `[milestone]` chunks, run `protocols/milestone-review.md`
(gstack `/review` + concern-diverse sweep) — a separate gate from chunk review.

**Push is not commit.** Land commits locally as chunks complete. Push to a
remote only after the relevant tests pass and the user has permitted publish
(see `AGENTS.md` §Git workflow).

## User progress reporting

Distinct from engagement polling and from worker `STATUS:` markers.

While `execute` has in-flight workers, review jobs, or background verification
(>10s), poll compact state and give the user a short progress update **at least
every 15 minutes** unless **context is tight** (compaction imminent, user asked
for minimal chatter, or the turn must stay small). Full rules:
`protocols/user-status-cadence.md`.

When context is tight, still poll and append a one-line timestamp to
RESUME-NOTES — skip the chat digest until there is room.

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

The checked-in wrapper currently runs in a Claude Code controller session.
Native Claude Agent-tool subagents share that session's rate-limit budget.
Codex/grok/cursor workers consume their own provider budgets and do not. These
are host-specific examples for this wrapper; portable routing decisions should
flow through adapter manifests as generated wrappers land. Default routing by
task:

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
model name from yesterday's recipe — ask `cursor-agent models` for the current
set, or read the doctor probe:

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

**ACP SDK dependency.** ACP dispatch uses the official Python SDK
(`agent-client-protocol==0.10.*`) from
`~/.goal-flight/venvs/acp-0.10/bin/python`. The documented `python3
<skill-root>/scripts/goalflight_acp_run.py ...` command re-execs into that
managed venv when system Python lacks `acp`; set `GOALFLIGHT_ACP_PYTHON` to
override the interpreter. If doctor reports `SDK missing -- run install`, run
`/goal-flight init` to create/update that venv before ACP dispatch.

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

**The caps are placeholders, not laws.** Today's numbers are best-guesses
calibrated against the maintainer's current vendor plans + 2026-05-19
service health. Reality is variable: vendor capacity fluctuates, plans
change, bad model releases happen, your own plan tier differs from the
maintainer's. The intended trajectory is for the rate-pressure walkback to
**learn** observed bounce thresholds per provider over time — see the
backlog entry "learned rate-pressure thresholds". Until that lands, treat
the static caps as starting defaults and adjust via `DEFAULT_AGENT_CAPS`
edits if your environment bounces consistently below them.

**The controller's own provider is asymmetric.** When goal-flight is hosted
by a Claude Code session, the controller's own service is `anthropic-session`
— the same budget the controller's interactive turns consume. If that
provider gets rate-limited, **the user's terminal stops working**. Other
providers (codex / grok / cursor) failing just means re-routing. Implication
for the learned-thresholds work:

- Worker providers: cautious upward exploration is OK. If 5 codex workers
  in parallel is working cleanly and there are many dispatches queued,
  try 6 and observe — if the 6th errors, back off. Standard EMA dynamics.
- Controller's own provider: bias conservative. Don't probe upward; only
  ratchet downward on pressure. Surface a STATUS marker early. Losing the
  controller ends the user's workday — the cost asymmetry justifies the
  caution asymmetry.

If goal-flight is hosted by a non-Claude controller (e.g., a codex-native
or editor-native port), the same asymmetric treatment applies to
whichever provider the controller runs on.

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
