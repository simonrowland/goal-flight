---
name: goal-flight
version: 1.2.1
description: "Portable Goal Flight workflow for long-running repo work: planning, dispatch, review, recovery, file-backed resume."
tags:
  - orchestration
  - orchestrator
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
  - AskUserQuestion
  - TodoWrite
triggers:
  - /goal-flight
  - start a long refactor
  - begin chunked work
  - set up orchestrator for unattended run
  - decompose this plan into goal chunks
  - resume the goal-flight run
  - continue the goal queue
  - recover a dispatched worker
---

> ⚠️ **Read this skill end-to-end, including Worker Routing, State, and Context Discipline** before acting; also read Do Not. The back half carries routing, state, marker, rate-limit, permission, and safety contracts.

Frontier-tier controllers: this core is complete; proceed on it alone. Other controllers — and any controller that catches itself about to violate a rule it just read — load `protocols/guidance-extended.md` before continuing. That file elaborates only (worked examples, expanded rationale); every rule and fact lives in this core, and any rule found only there must be moved back here.

This checked-in `SKILL.md` is compiled from `docs/controller-behaviours.md` and is the Claude Code-compatible wrapper for the portable core; keep front matter and `allowed-tools` compatible until generated wrappers own host bindings, tool names, invocation details, and packaging.

## Activation Check

**Is goal-flight active in this project?** Run
`python3 <skill-root>/scripts/goalflight_session_status.py --text` before
auto-loading the rest of this skill. If the verdict is "no active
goal-flight session", you are NOT in a goal-flight run — do regular coding
without loading the back half. Only load end-to-end when the verdict is
"active" or when the user explicitly invokes `/goal-flight <command>`.

`<skill-root>` = this repo when working in goal-flight itself; for downstream projects it is the installed skill checkout (see per-host pointers) — resolve it before running scripts.

**Skill-freshness + designated-controller check.** If a previous-invocation
reminder exists but you can't quote this preamble verbatim, reload via State's
canonical resume order before acting; then compare
`scripts/goalflight_session_status.py --ensure-session` with the active queue's
`current_session.id`: match → you are the designated orchestrator; mismatch →
surface to the user before claiming.
Extended: `protocols/guidance-extended.md` §activation-check

## Per-host pointers

Per-host pointers tell non-native orchestrators where their installed wrapper lives.
If you are a non-Claude orchestrator (codex, grok, cursor, opencode), load your
host wrapper first, then root `SKILL.md` as canonical workflow:

| Host | Installed wrapper path |
|---|---|
| codex | `~/.codex/plugins/cache/goal-flight/goal-flight/<version>/skills/goal-flight/SKILL.md` or `~/.codex/skills/goal-flight/SKILL.md` |
| cursor | `.cursor/skills/goal-flight/SKILL.md`, `~/.cursor/skills/goal-flight/SKILL.md`, or `~/.agents/skills/goal-flight/SKILL.md` |
| grok | `~/.grok/skills/goal-flight/SKILL.md` or generated path from `configs/grok/skills/goal-flight/SKILL.md` |
| opencode | `.opencode/skills/goal-flight/SKILL.md`, `~/.config/opencode/skills/goal-flight/SKILL.md`, or `~/.agents/skills/goal-flight/SKILL.md` |

**Stale-wrapper warning:** non-native hosts hold a *copy* of `SKILL.md`. If
the source repo updated and `./install.sh <host>` was not re-run, the
installed copy is stale. The repository `SKILL.md` is canonical — when the
installed wrapper and the repo wrapper disagree, trust the repo. Doctor
probes the divergence; re-run install to resync.

**Windows (native host):** read/plan only — native worker dispatch (ACP / bash-tail)
is POSIX/WSL-only; use the `bin/goalflight.cmd` / `bin/goalflight.ps1` launchers and
see `docs/hosts/windows.md` (including the WSL path for worker dispatch).

Load order: `AGENTS.md` -> installed host wrapper -> repository `SKILL.md` ->
only the invoked `commands/*.md` plus referenced `protocols/*.md`.
Companion tools: gstack `/review` is the canonical chunk reviewer; gstack
`/challenge` is the adversarial frame; fall back to `prompts/gstack-*.md` only
when gstack is absent. context-mode stores large outputs and searches them.
Orchestrator behaviour probes run through portable host adapters, not host-specific print-mode shortcuts.

## Navigation map: behaviour -> SKILL anchor -> protocol/script

| Topic | SKILL anchor | Protocol/script |
|---|---|---|
| **is goal-flight active here?** | preamble above | `scripts/goalflight_session_status.py --text` |
| status/doctor preflight | Session Pre-Flight | `protocols/session-preflight.md`, `scripts/goalflight_status.py --wait <ids>`, `scripts/goalflight_doctor.py` |
| **in-flight dispatch monitoring** | Session Pre-Flight | `scripts/goalflight_status.py`/`--wait <ids>`, `scripts/goalflight_watch.py`, `scripts/watch-dispatch-tail.sh` |
| **active leases / what's in flight** | Capacity and rate limits | `scripts/goalflight_capacity.py status` (surfaces adaptive walkback) |
| **per-chunk status snapshot** | Session Pre-Flight | `scripts/goalflight_chunk_summary.py --slug <slug> --json` |
| autonomous throughput | Autonomous throughput | `commands/execute.md`, `commands/goal.md` |
| **chat as requirements** | Chat as requirements | `commands/goal.md`, `protocols/user-status-cadence.md` |
| context lints | Autonomous throughput | `protocols/engagement-lint.md`, `foreground-duration-hook.md` |
| user-status-cadence | User progress reporting | `protocols/user-status-cadence.md` |
| project state layout | State | `protocols/project-state-layout.md` |
| task lifecycle/store behaviour | State | `protocols/task-lifecycle.md` |
| dashboard/task views | User progress reporting | `protocols/progress-dashboard.md` |
| chunk-vs-milestone review | Review layers | `protocols/chunk-review.md`, `protocols/milestone-review.md` |
| **bug-class mining / backwards sweeps** | Review layers | `protocols/review-mining.md` |
| dispatch axes | Dispatch Model, Worker Routing | `protocols/dispatch-routing.md` |
| worker context packages / lane pinning | Dispatch Model | `protocols/worker-context-package.md` |
| worker permissions | Worker Routing | `scripts/goalflight_acp_run.py`, doctor `--worker-write-probe`, `scripts/install_claude_acp_patch.sh` |
| **worker blocked: orchestrator takeover** | Worker Routing | `protocols/dispatched-worker-recovery.md` |
| rate limits & caps | Capacity and rate limits | `scripts/goalflight_capacity.py`, `scripts/goalflight_rate_pressure.py` |
| worker markers | Worker Markers | `protocols/worker-markers.md`, `scripts/goalflight_watch.py` |
| resume/compaction | State | `commands/resume.md`, `protocols/state-handoff.md`, `scripts/goalflight_session_status.py` |
| context discipline | Context Discipline | context-mode, `scripts/goalflight_*.py` |
| **Do Not / safety gates** | Do Not | (read-end-to-end is load-bearing for safety) |
| extended controller guidance | preamble | `protocols/guidance-extended.md` |

## Orchestrator Contract

Use this wrapper for work too large for one uninterrupted session: decomposed
implementation, long refactors, review flights, resumable queues, or unattended
dispatch. The orchestrator manages context and verification; it does not hoard
every file, log, or worker transcript in conversation.
Orchestrator context is scarce; delegate iteration so only the converged conclusion returns.

Always:
- read the invoked command file and only its referenced protocols
- run helpers for machine facts, status, logs, capacity, and tool probes
- keep raw logs and long reviews in files; reason over compact summaries
- Analyze/search/count/filter with procedural code or context-mode
- Explicit user-directed mission outranks the store frontier: park the queue, register the mission as store tasks, record the parking decision in RESUME-NOTES; do not silently hijack into the old frontier or work outside the store.
- Hosts may defer optional tool schemas (ToolSearch era): load/discover a deferred host or MCP tool's schema before first use; do not assume preload.

Never load fork, ACP, corpus, review, or tool-specific details just because the
skill loaded. Load those protocols on demand.

## Session Pre-Flight

For non-trivial commands, use `protocols/session-preflight.md`.

```bash
python3 <skill-root>/scripts/goalflight_status.py
```

Use doctor when readiness is unknown or changed:

```bash
python3 <skill-root>/scripts/goalflight_doctor.py --project-root "$PWD" --json
```

Surface only actionable warnings: install ambiguity, missing required tool,
capacity cooldown, stale dispatch, surplus worker-like process, or fingerprint
drift against an in-flight queue.
`goalflight_messages.py relay` output is unbounded and cross-project — scope it (pipe through `head`, or filter for the current project) before running it in controller context.

## Commands

| Command | File | Required protocols |
|---|---|---|
| `/goal-flight init <topic>` | `commands/init.md` | `session-preflight`, `tool-readiness`, `premises`, `state-handoff` |
| `/goal-flight decompose-plan [<plan>]` | `commands/decompose-plan.md` | `premises`, `dispatch-routing` |
| `/goal-flight ask-questions [<scope>]` | `commands/ask-questions.md` | `dispatch-routing` |
| `/goal-flight execute [--parallel <N>]` | `commands/execute.md` | `dispatch-routing`, `worker-markers`, `state-handoff`, `user-status-cadence`, `chunk-review`, `milestone-review`; add `worktrees-parallel` for `--parallel` |
| `/goal-flight doctor` | `commands/doctor.md` | `tool-readiness` |
| `/goal-flight migrate [<flags>]` | `commands/migrate.md` | `project-state-layout`, `task-lifecycle` |
| `/goal-flight build-corpus [<flags>]` | `commands/build-corpus.md` | corpus docs referenced there |
| `/goal-flight resume` | `commands/resume.md` | `state-handoff` |
| `/goal-flight goal <SLUG>` | `commands/goal.md` | none |
| `/goal-flight register-codex [<path>]` | `commands/register-codex.md` | `tool-readiness` |
| `/goal-flight update` | `commands/update.md` | `tool-readiness` |
| `/goal-flight validate-dispatch [<slug>]` | `commands/validate-dispatch.md` | `dispatch-routing`, `worker-markers` |
| `/goal-flight validate-queue [<path>]` | `commands/validate-queue.md` | none |

Protocol index: `protocols/README.md`.

## Command danger classification

Full detail + the drainer daemon + the incident writeup: `protocols/dispatch-danger.md`.

**READ-ONLY (safe, free):** `goalflight_task.py status` · `list` · `next` · `show` —
read/derive from the store only. `next` prints the frontier; it does NOT dispatch it.

**⚠ DISPATCHES WORKERS (spawns processes, leases capacity, costs money, may mutate a
worktree):** `dispatch-frontier` (legacy alias `pipe`) fans out the WHOLE frontier as
one worker per item — it is NOT a drainer, needs `--autodispatch-confirm`, and runs in
the shared project root with the raw prompt (no mandate). `/goal-flight execute` and
`goalflight_dispatch.py` also spawn workers — dispatcher default is a detached launch; `--submit` writes a durable queue entry AND runs one immediate drain pass by default (`--no-drain-on-submit` for queue-only).

**Always-on drainer:** the `com.goalflight.drain` launchd daemon runs `goalflight_dispatch.py
drain --json` every ~60s and LAUNCHES anything queued — queuing is not free, and the
ledger/queue are shared across projects (identify origin by `project_root`).

## Review layers

Reviews are cut by SUBJECT; `protocols/review-types.md` is operative (two waves
+ 3-cluster pilot). Distinct review layers: executor self-review, Type-1 chunk
review, Type-2 milestone review; Type 3 sweeps class predicates.

| Layer / Type | Gate | Default |
|---|---|---|
| Executor self-review (floor) | Before handoff; self-refutation DRY | seven categories + null hypothesis; non-trivial: ≥2 lenses. Never replaces Type-1 FIND (field: 9 P1s) |
| Type 1 — patch multi-review | Every commit-worthy chunk | `protocols/review-types.md`: N FIND reviewers → one non-finder FIX executor; pinned findings, per-hunk attribution, fix null hypotheses (`protocols/review-fix-report.md`); controller samples |
| Type 2 — milestone review | 5 chunks, `[milestone]`, or pre-push | milestone/QA bug sweep; adversarial verify; disjoint fix groups |
| Type 3 — dictionary deep-sweep | Each class mint; under-searched predicates | predicate bug sweep; exit at marginal_real_yield ≈ 0 |

On chunk completion, dispatch gstack `/review` before committing; use
`/challenge` as the canonical adversarial frame; never hand-roll review prompts.
Controller re-takes the null stance with ≥2 concern-diverse lenses, scaling by complexity. Review routing follows `protocols/review-types.md`; non-code flights use `prompts/gstack-*.md`. [RT-005]
Reviewer misses become regression tests, not trust exemptions. Write review rubrics before first wave dispatch.
Review results are saved durably under `docs-private/reviews/` or the chunk research dir; /tmp-only verdicts cannot be mined.
Each NEW bug class triggers MINT-generalize (`protocols/review-mining.md`): mint, sweep backwards over code + saved reviews, record no-hits, encode the lens. One catch, one class, one sweep.
Reviews are one-shot; fixes loop to green and re-review; substantive closures get a refutation pass.
Diversify reviewer concern, not just model; scale perspectives by complexity/stakes. Use consolidation review for cross-slice contradictions.
Milestone review is a separate gate from chunk review; status prints chunks since last sweep; skipped due sweep = open liability.

## Nested Review Invocation

Canonical nested review shape (full rationale + flags: `protocols/chunk-review.md`):

```bash
codex exec --sandbox read-only \
  -c approval_policy=never \
  -c 'model_reasoning_effort="xhigh"' \
  --enable web_search_cached \
  "$REVIEW_PROMPT" \
  < /dev/null \
  > docs-private/reviews/<date>-<slug>/codex-review.final.md \
  2> docs-private/reviews/<date>-<slug>/codex-review.stderr.log
```

**`< /dev/null` is load-bearing.** Without it, `codex exec` reads stdin to EOF
and the bash-tail invocation blocks forever (observed wedge 2026-05-27).
**`-c approval_policy=never`** is the canonical non-interactive form (per
`protocols/legacy/bash-tail.md` worker recipe). Do NOT substitute the
deprecated `--dangerously-bypass-approvals-and-sandbox` flag — it is
rejected by classifiers and explicitly forbidden in adapter manifests
(`adapters/codex.json` `forbidden_args`). Apply P3-safe-easy findings
inline; fix P0/P1/P2 before commit.

## Hard Invariants

- Verification first. Every executor prompt starts by checking repo state,
  target files, and assumptions before editing.
- Background anything expected to run longer than 10 seconds.
- Subagent / Agent / Task / Explore dispatches whose returns may exceed
  ~5KB MUST write findings to
  `docs-private/research/<date>-<slug>/findings.md` and return a TL;DR +
  severity count, then `READY: <path>` as the **last**
  non-empty line (terminal marker — emit TL;DR/findings before it). The
  orchestrator reads the TL;DR and opens the file only when it signals real action.
- Read >5KB without an expected Edit follow-up within 2 turns → use
  `Agent`/Explore with a defined prompt; do not pull recon bodies into
  controller context.
- The host Agent / Task / Explore tool is for recon, analysis, and review ONLY
  — NEVER a code executor. Code-writing chunks use
  `scripts/goalflight_dispatch.py`, or controller-direct only when tiny.
- Out-of-scope findings go to the store's `deferred` lane via `goalflight_task.py capture`.
  Worker-doable findings are worker tasks, not host `spawn_task`/"chip"; capture
  worker RESULT fallout before moving on.
- No `tail -f` in conversation; liveness authority is the aggregate status command, not raw watcher heartbeat fields.
- No worker spawn without capacity consideration.
- No bare `git commit` while workers are in flight — commit guard
  `scripts/goalflight_commit_guard.py` refuses to prevent bundling worker
  WIP. Use `git commit -m '...' -- <files>` with explicit pathspecs.
- No broad `--permission-allow-tool-title-pattern '.*'` without
  `--os-sandbox=read-only` — title-allow layers AFTER hard gates, so
  execute/fetch escalates without sandbox; the warning fires at startup.
- Every long worker or review job needs a ledger/status path.
- Missing or stalled review is inconclusive, not clean.
- Ask the user only for real product/permission blockers, destructive choices,
  or irreducible ambiguity.
- Report progress at least every 15 minutes unless context is tight.
- Workers escalate sandbox / permission / tool blocks via `BLOCKED:` and return to the orchestrator. They do NOT execute workarounds; push and out-of-standard-path commits are the orchestrator's call.
- Keep `docs-private/` private.
Extended: `protocols/guidance-extended.md` §hard-invariants

## Gotchas from session traffic

Mined from repo-local failure traffic (full evidence when present: `docs-private/research/goal-flight-gotchas-audit/addendum.md`):

- **Stale skill body on resume.** Can't quote current Hard Invariants -> reload AGENTS -> host wrapper -> `SKILL.md` -> `commands/resume.md` before queue/status/git.
- **Inline output flood.** Logs/diffs/JSONL/review transcripts -> files/context-mode; controller reads status JSON + TL;DR + `READY: <path>`, not raw streams.
- **Nested review permission trap.** Don't run gstack/codex review as a nested ACP tool call inside the worker; use bash-tail read-only review (`--sandbox read-only`, `-c approval_policy=never`, `< /dev/null`).
- **Stdin wedge.** `codex exec` reads stdin to EOF even with a positional prompt; missing `< /dev/null` on bash-tail review hangs.
- **Command-form drift.** Deprecated/forbidden exec flags in old docs aren't precedent; adapter `forbidden_args` + the current canonical invocation win.
- **Worker bypass.** A worker hitting a sandbox/permission/write/commit block returns `BLOCKED:`; alternate delivery paths are orchestrator-only.
- **False worker death.** Don't discard work on PID/`comm`/one stale field; reconcile pid+start-time, status JSON, ledger, tail marker, output mtime, dirty tree.
- **Quiet is not dead.** Network waits + child-process test runs show no controller-visible output/CPU; liveness needs terminal markers + process-tree + idle confirmation.
- **Terminal marker not final until reconciled.** A live-but-quiet worker after COMPLETE/RESULT/READY must still pass idle/controller-dead logic.
- **Rollover loses notifications, not state.** Background completion signals are best-effort/session-local; status JSON, ledgers, resume/reconcile are authoritative.

## Capacity and rate limits

Consider capacity before any worker spawn. Defaults come from
`scripts/goalflight_agent_limits.py` (`DEFAULT_AGENT_CAPS`, imported by
`goalflight_capacity.py`).
Capacity acquire waits on machine, agent, RSS, or cooldown pressure.

Hard caps are RAM/process safeguards, not provider truth. Learn rate pressure from ledger, not constants. `scripts/goalflight_rate_pressure.py` reads recent
ledger failures and emits fallback/halved-cap recommendations after clustered
provider pressure.

Probe workers upward; keep orchestrator provider conservative. Orchestrator budget
loss can end the interactive session; worker-provider pressure can be rerouted.
Bound dispatch hangs with idle and quiet timeouts. Terminal leases leave active capacity after completion.

## Autonomous throughput

Goal Flight exists so long work survives compactions and unattended hours. The
orchestrator advances the queue; it does not poll the user for presence.

When the user invoked goal-flight, approved a plan, or gave scope:
- Keep working through code, tests, queue/ledger/resume updates, review, and
  commits until decomposition/execute drains or a real blocker stops it.
- Default is continue, not confirm.
- Do not use engagement prompts or permission-boxes over obvious matters; if an
  action is the obvious next step and not destructive/irreversible/a genuine
  product choice, do it and report.
- Record non-blocking uncertainty in files, then proceed with the plan default.
- Commits during execute follow **one commit per completed chunk**.
- Push to a remote only after the relevant tests pass and the user has permitted publish.

Stop only for `USER-NEED` / `USER-CONFIRM` blockers: permission, destructive
or irreversible action without a plan default, product choice the plan cannot
infer, auth/capacity hard stop, or explicit command gate.
Extended: `protocols/guidance-extended.md` §autonomous-throughput

## Chat as requirements

Orchestrator chat is requirements input, not an inline editor command. Mid-session
asks are steering/architecture/scope input. Append them to the active goal queue
or promote them to a plan revision plus re-review when they change scope.

Do not task-pivot or inline-edit on receipt. Plan before editing when scope is unsettled. Prepare ambiguous questions before asking the user. Relay USER-NEED through orchestrator, not worker chat. chat alone is not the backlog.

### In-flight steer mailbox

Steer a live worker via `scripts/goalflight_dispatch.py steer <id> '<msg>'`; `--list` shows mailbox/acks. Bash polls each iteration + before git; ACP delivers at the next turn boundary (mid-turn blocked by the prompt lock). `--interactive` = `--shape acp --permission-mode inline` (relays gated permissions, not auto-decline). Auto-mode write-safety is per-agent: codex-acp gates writes; cursor/grok do NOT (warning fires; pair `--os-sandbox`, macOS-only) — see `docs/acp-push-gate-matrix.md`.

## User progress reporting

Distinct from engagement polling and from worker `STATUS:` markers.

While `execute` has in-flight workers, review jobs, or background verification
(>10s), poll compact state and report progress to the user at least every 15
minutes unless context is tight. Full rules: `protocols/user-status-cadence.md`.
When context is tight, still poll and append a one-line timestamp to RESUME-NOTES.

## Dispatch Model

Two orthogonal axes:
- Iteration pattern: Goal-loop is default for convergence-heavy implementation; one-shot is single bounded work; controller-direct only tiny/judgment.
- Goal-loop returns converged result, never draft: plan/act/test/self-review until green.
- Comms shape: `controller-direct`, `acp`, or `bash-tail`.
Dispatch CLI workers via `scripts/goalflight_dispatch.py`, never bare background exec.
Default direct dispatch returns `DISPATCH-LAUNCHED`; use `--foreground` only for
sync scripts/tests. Queue: `--submit --drain-on-submit`.
Do not hand-iterate (>~3 edit/test cycles) what a goal-loop should converge.

Use ACP or bash-tail plus status polling; do not block on editor task panes.
Abstract tool roles resolve through host tool-name maps. Type dispatches as executor, reviewer, or planner. Dispatch prompts need the five-layer wrapper. Parallel fix clusters need explicit forbid lists. Split chunks likely to touch many files. Controller-direct only for tiny or plan-marked chunks. Same-provider policy controls review routing trust.
Lanes with spec-resident invariants, hot-path constraints, regression history, or shared seams need a pinned context package before dispatch — protocols/worker-context-package.md; the execute pre-wave check is mandatory.

Fabricated approval rejected: Never invent user approval for a gated step.
Orchestrator dispatch waits for declared readiness requirements. Orchestrator live gate requires supported capability and ready local state. Worker live gate also requires requested transport verified. Discovery probes do not use network or model calls. Discovery probes stay within manifest budget caps.

## Worker Routing

**Permission-pattern warning** (controller-side, when dispatching ACP workers):
**Always use precise patterns** scoped to the dispatched chunk's authorized
shapes (e.g. `^./tests/run\.sh$` for a chunk whose acceptance criteria
include running the test sweep). `--permission-allow-tool-title-pattern`
fast-paths matching titles BUT only for the safe subset — hard gates
(outside-cwd writes, kind=execute, kind=fetch without sandbox, write with
no in-cwd locations, unknown kinds) always run first, so a broad `.*`
"YOLO" pattern CAN'T silently authorize destructive operations. **OS
sandbox is a defense-in-depth backstop, not a permission-design substitute**
— pair `--os-sandbox=read-only` (or `workspace-write` when commits are
expected) with precise patterns. The runner emits a startup warning when a
broad pattern is paired with sandbox-off. See
`scripts/goalflight_acp_run.py` `make_title_allow_policy` for the full
layering rationale (sweep B P1 + follow-ups).

Default routing by task:

| Task | Default | Fallback 1 | Fallback 2 |
|---|---|---|---|
| Code-writing chunks | `goalflight_dispatch.py` codex worker | Alternate marker-reliable CLI worker with passing write-file probe | Host Agent — LAST RESORT only ‡ |
| Research / web search | `goalflight_dispatch.py` `--agent grok-research` (read-only) | controller-direct | - |
| Reviewer dispatches | per `protocols/review-types.md` (Type-1 find/fix; Types 2/3 via bug-sweep) | stakes carve-down: single concern-diverse reviewer for trivial chunks ONLY [RT-004] | Claude Agent only when others unreachable |
| Planning / decompose | code/planning worker | controller-direct | Claude Agent |
| Anticipatory questions | strongest interactive planner | controller-direct | - |
| Analysis / reflection | controller-direct | - | - |
| Voice-sensitive prose | orchestrator judgment per chunk | - | - |

‡ **Host Agent as code executor = LAST RESORT, never a co-equal fallback.** Use
only when EVERY CLI worker (codex, grok-code) is genuinely unreachable, not slow.
1. Confirm CLI workers are down with doctor/probe.
2. `log()` + record degraded host-Agent fallback and why in RESUME-NOTES.
3. Return to `goalflight_dispatch.py` when a CLI worker recovers.
Read-only review/analysis via Explore/Agent is covered by Hard Invariants.
Extended: `protocols/guidance-extended.md` §worker-routing

Use adapter manifests and doctor probes for current host/model details; do not
hardcode yesterday's model list. Cursor internal models do not need passthrough
unless that chunk explicitly needs the vendor. ACP SDK dispatch uses the managed
`agent-client-protocol==0.10.*` venv unless overridden.

### Hard caps

`DEFAULT_AGENT_CAPS` lives in `scripts/goalflight_agent_limits.py` and is
imported by `goalflight_capacity.py`. Capacity checks apply default per-agent caps.
Per-machine overrides come from `$GOALFLIGHT_CAPACITY_CONF` else
`~/.goal-flight/capacity.local.json`; `agent_caps` merge over defaults.
Hard caps are placeholders, not laws; provider budgets may be shared by labels.

### Adaptive walkback

Adaptive walkback reads the ledger through `scripts/goalflight_rate_pressure.py`.
If one provider shows repeated recent rate-limit signatures, re-route next work,
surface status, or reduce effective cap. No autonomous capacity mutation in v1.

### Controller-provider asymmetry

Controller-provider-asymmetry: protect the orchestrator's own provider more
conservatively than worker providers. Worker failures can reroute; orchestrator
failure can strand the user.

Bash-tail recipes live in `protocols/legacy/bash-tail.md`; forking lives in
`protocols/self-delegation.md`; worker-blocking recovery lives in
`protocols/dispatched-worker-recovery.md`.

## Verification and test gates

Before each chunk commit: focused tests green. Background tests are pending until results are read. `./tests/run.sh` is the repo-wide gate when scope or risk justifies it. `./scripts/autoreview.sh` is a complementary parallel option, never the default; gstack `/review` remains default. GOALFLIGHT_AUTOREVIEW=1 is an optional maintainer tier, not a default review path.

For each Golden Master entry, SKILL.md contains the entry's compressed-form text. Wave 2 scenarios: draft-goal-office-hours, vague-goal-premise-backlog, context-load-order. Build corpus eagerly; it audits source truth. Use primary sources, not precis, for corpus slices. Specialize self-review bullets to project nouns. Check source-truth contradictions before corpus build. Preflight noninteractive workers for MCP approval stalls. No remote dispatch before phase gate is green.

## Worker Markers

Long worker and review jobs require a ledger/status path. Status contract requires heartbeat markers for live workers. Heartbeats are files; wake only on transitions. Stale workers trip on manifest stale-after thresholds. Terminal states are closed manifest values. Worker markers use goalflight dispatch transport sequence grammar.

Workers communicate with one-line markers:
- `STATUS:`
- `STEER-ACK:`
- `RESULT:`
- `USER-NEED:`
- `USER-CONFIRM:`
- `BLOCKED:`
- `FAILED:`
- `COMPLETE:`
- `READY:`

`PERMISSION-OK-PROCEEDED:` is ACP-only. Details live in
`protocols/worker-markers.md`.

## State

### State layers

Use three state layers:
- project: git, tests, docs, queue
- machine: capacity leases, dispatch ledger, cooldowns
- conversation: current decisions, unresolved questions, optional controller-only host todo/checklist tool (ephemeral, dies on compaction; never durable state — that is the queue + RESUME-NOTES)

Repository files are the canonical memory backend.
Memory writeback requires migration lock ownership.

### Status plane and liveness

Use one status plane across transports.
Ledger liveness matches PID plus process identity.
Never `pgrep` for worker liveness; use dispatch/status identity.
Isolate pidfiles per orchestrator session.
Classify ACP failures as upstream, local, or repo.

### Resume and handoff

Remote workers execute; orchestrator remains designated surface.
Propose AGENTS.md changes as diffs only.

On resume or after sleep:

```bash
python3 <skill-root>/scripts/goalflight_status.py
```

Active run + compaction: if already in play, invoke `/goal-flight resume` for fresh `SKILL.md`/`commands/resume.md`, then stay in-skill: dispatch workers, review before commit, one commit/chunk; never default-fallback to inline edits, task pivot, or hand-rolled review.

**Canonical post-compaction reload order:**
1. Read `AGENTS.md` (entry point).
2. `python3 <skill-root>/scripts/goalflight_session_status.py --text` —
   single command, definitive verdict. If "no active goal-flight session",
   stop here; you are NOT in a goal-flight run.
3. Read repository `SKILL.md` end-to-end (this file).
4. Find newest RESUME-NOTES:
   `ls -1 docs-private/RESUME-NOTES-*.md | sort | tail -1`
   (canonical pattern: `RESUME-NOTES-<YYYY-MM-DD>[-rev<N>].md` — ISO 8601
   date so lexicographic sort = chronological; no topic prefixes).
5. Run store baseline: `python3 scripts/goalflight_status.py` + `python3
   goalflight_task.py list`; if degraded, use the handoff's last store command.
6. Read handoff prose for environment, ideas/decisions, facts, and carriers;
   task tables, dispatch codes, and next lists live in the store.
7. Run status again, then `python3 goalflight_task.py next`; continue the top
   task after compaction or side-mission without waiting for a re-prompt.

## Context Discipline

Read for edits narrowly. Analyze/search/count/filter with procedural code or
context-mode. Store long artifacts in files and return paths plus summaries.
Prebuild corpus; do not inline landscape per dispatch. Keep worker-context optional when canonical docs fit; triggered lanes are the exception — they REQUIRE a pinned context package (`protocols/worker-context-package.md`).

When in doubt, move deterministic logic into `scripts/goalflight_*.py`; keep the
model responsible for judgment: choosing next action, interpreting findings, and
deciding whether a warning matters.

## Trigger and publish discipline

Git-visible trigger aliases stay out of filenames, manifests, and commit
messages. Push to a remote only after the relevant tests pass and the user has
permitted publish.

## Do Not

- Do not paste long logs, diffs, JSONL streams, or review transcripts.
- Do not treat PID alone as process identity.
- Do not hand-iterate (>~3 edit/test cycles) a chunk in orchestrator context — goal-loop it. Controller-direct is for tiny or judgment-only edits.
- Do not let one goal-flight session consume all machine capacity.
- Do not silently skip review when a provider hits rate or session limits.
- Do not load `/fork` instructions by default.
- Do not substitute print-mode prompts for live behaviour probes or canonical review dispatch.
- Forbidden shell families never enter orchestrator dispatch.
- Auto-approve detection is strict-fail, not advisory.
- Irreversible operations require explicit user gate.
- Secrets stay out of probes, wrappers, and logs.
- Forbidden exec args are rejected in every dispatch surface.
- Risky exec args need explicit justification before use.
- Inline permits use request, decision, and ack files.
- Install actions need user gates and backup paths.
