---
name: goal-flight
version: 1.5.1
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
  - check mail
---

> ⚠️ **Read this skill end-to-end, including Worker Routing, State, and Context Discipline** before acting; also read Do Not. The back half carries routing, state, marker, rate-limit, permission, and safety contracts.

Frontier-tier controllers: this core is complete; proceed on it alone. Other controllers — and any controller that catches itself about to violate a rule it just read — load `protocols/guidance-extended.md` before continuing. That file elaborates only (worked examples, expanded rationale); every rule and fact lives in this core, and any rule found only there must be moved back here.

This checked-in `SKILL.md` is compiled from `docs/controller-behaviours.md` and is the Claude Code-compatible wrapper for the portable core; keep front matter and `allowed-tools` compatible until generated wrappers own host bindings, tool names, invocation details, and packaging.

## Activation Check

**Is goal-flight active in this project?** Run
`python3 <skill-root>/scripts/goalflight_session_status.py --text` before
auto-loading the rest of this skill. Verdict "no active goal-flight session"
→ you are NOT in a goal-flight run; do regular coding without loading the
back half. Load end-to-end only when the verdict is "active" or the user
explicitly invokes `/goal-flight <command>`.

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
installed copy is stale. Repository `SKILL.md` is canonical — when wrappers
disagree, trust the repo. Doctor probes the divergence; re-run install to resync.

**Windows (native host):** read/plan only — native worker dispatch (ACP / bash-tail)
is POSIX/WSL-only; use the `bin/goalflight.cmd` / `bin/goalflight.ps1` launchers and
see `docs/hosts/windows.md`.

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
| pre-dispatch premise scouting | Dispatch Model, Worker Routing | `protocols/scout.md` |
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
| **dead worker: resume or redispatch** | Dispatch Model | `protocols/dispatch-resume.md`, `scripts/goalflight_dispatch.py resume <id>` |
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
python3 <skill-root>/scripts/goalflight_session_status.py --controller-startup
python3 <skill-root>/scripts/goalflight_status.py
```
Use doctor when readiness is unknown or changed:

```bash
python3 <skill-root>/scripts/goalflight_doctor.py --project-root "$PWD" --json
```

Surface only actionable warnings: install ambiguity, missing required tool,
capacity cooldown, stale dispatch, surplus worker-like process, or fingerprint
drift against an in-flight queue.
Mail is journal-assigned, not a private markdown file: `relay --new` peeks without acknowledging, `--list-controllers` lists leases, `post --to-controller` sends, and a generation-bound one-shot `listen` wakes. See `protocols/controller-mail.md`.

## Commands

| Command | File | Required protocols |
|---|---|---|
| `/goal-flight init <topic>` | `commands/init.md` | `session-preflight`, `tool-readiness`, `premises`, `scout`, `state-handoff` |
| `/goal-flight decompose-plan [<plan>]` | `commands/decompose-plan.md` | `premises`, `dispatch-routing`, `scout` |
| `/goal-flight ask-questions [<scope>]` | `commands/ask-questions.md` | `dispatch-routing` |
| `/goal-flight execute [--parallel <N>]` | `commands/execute.md` | `dispatch-routing`, `worker-markers`, `scout`, `state-handoff`, `user-status-cadence`, `chunk-review`, `milestone-review`; add `worktrees-parallel` for `--parallel` |
| `/goal-flight doctor` | `commands/doctor.md` | `tool-readiness` |
| `/goal-flight migrate [<flags>]` | `commands/migrate.md` | `project-state-layout`, `task-lifecycle` |
| `/goal-flight build-corpus [<flags>]` | `commands/build-corpus.md` | corpus docs referenced there |
| `/goal-flight resume` | `commands/resume.md` | `session-preflight`, `state-handoff` |
| `/goal-flight goal <SLUG>` | `commands/goal.md` | none |
| `/goal-flight usage` | `commands/usage.md` | none |
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
worktree):** `/goal-flight execute` and `goalflight_dispatch.py`. The dispatcher
launches one worker immediately in detached mode, waits for the lane's capacity
window, then writes `blocked_capacity`, prints `DISPATCH-BLOCKED`, and exits nonzero
if capacity remains unavailable. Dispatch frontier items individually; there is no
bulk fan-out command.

**Backlog drainer:** the `com.goalflight.drain` launchd daemon runs
`goalflight_dispatch.py drain --json` every ~60s only to finish queue entries created
before direct-only dispatch. The ledger/queue are shared across projects (identify
origin by `project_root`).
`drain --queue-dir <path>` scopes to envelopes already in that directory (it does
not restore ledger orphans into a private dir). Launch one id with
`drain --dispatch-id <id>`.

## Review layers

Reviews are cut by SUBJECT; `protocols/review-types.md` is operative (two waves
+ 3-cluster pilot). Distinct review layers: executor self-review, Type-1 chunk
review, Type-2 milestone review; Type 3 sweeps class predicates.

| Layer / Type | Gate | Default |
|---|---|---|
| Executor self-review (floor) | Before handoff; self-refutation DRY | seven categories + null hypothesis; every worker states concrete failure conditions and verifies defensively with evidence they are absent; non-trivial: ≥2 concern-diverse lenses as floor, not target. Never replaces Type-1 FIND (field: 9 P1s) |
| Type 1 — patch multi-review | Every commit-worthy chunk | `protocols/review-types.md`: N FIND reviewers → one non-finder FIX executor; pinned findings, per-hunk attribution, fix null hypotheses (`protocols/review-fix-report.md`); controller samples. Default gstack `/review`; `./scripts/autoreview.sh` as a complementary parallel option |
| Type 2 — milestone review | 5 chunks, `[milestone]`, or pre-push | `protocols/milestone-review.md`; milestone/QA bug sweep; adversarial verify; disjoint fix groups |
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

**`< /dev/null` is load-bearing** — without it, `codex exec` reads stdin to EOF
and bash-tail wedges (observed 2026-05-27). **`-c approval_policy=never`** is
the canonical non-interactive form (`protocols/legacy/bash-tail.md`). Do NOT
substitute `--dangerously-bypass-approvals-and-sandbox` — classifiers reject
it; `adapters/codex.json` `forbidden_args` forbids it. Apply P3-safe-easy
findings inline; fix P0/P1/P2 before commit.

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
  `scripts/goalflight_dispatch.py`, or controller-direct only with held context
  and no fleet stall (Axis 2).
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

Evidence: `docs-private/research/goal-flight-gotchas-audit/addendum.md`.

- **Stale skill body on resume.** Reload AGENTS -> host wrapper -> `SKILL.md` -> `commands/resume.md` before queue/status/git.
- **Inline output flood.** Logs/diffs/JSONL/review transcripts -> files/context-mode; read status JSON + TL;DR + `READY: <path>`.
- **Nested review permission trap.** Use bash-tail read-only review (`--sandbox read-only`, `-c approval_policy=never`, `< /dev/null`), not nested ACP.
- **Stdin wedge.** `codex exec` reads stdin to EOF even with a positional prompt; missing `< /dev/null` on bash-tail review hangs.
- **Command-form drift.** Adapter `forbidden_args` + the current invocation override old docs.
- **Worker bypass.** On sandbox/permission/write/commit block, return `BLOCKED:`; alternate delivery is orchestrator-only.
- **False worker death.** Reconcile pid+start-time, status, ledger, tail marker, output mtime, and dirty tree before discarding work.
- **Throttled/quota-killed is not failed — RESUME it.** `transient_throttle`, `quota_exhausted` and sandbox `BLOCKED:` say nothing about the work's quality, and the worktree usually holds finished or nearly-finished uncommitted edits. Run `git -C <worktree> status --short`, then `goalflight_dispatch.py resume <id> --prompt-file <brief> --cwd <worktree>`. Redispatching instead silently discards that work. (Controllers keep re-learning this one; it is an affordance gap, not a knowledge gap.)
- **Quiet is not dead.** Network waits and child tests may show no output/CPU; confirm terminal markers, process tree, and idle.
- **Terminal marker not final until reconciled.** COMPLETE/RESULT/READY still needs idle/controller-dead logic.
- **Rollover loses notifications, not state.** Status JSON, ledgers, resume/reconcile are authoritative.

## Capacity and rate limits

Consider capacity before any worker spawn. Defaults come from
`scripts/goalflight_agent_limits.py` (`DEFAULT_AGENT_CAPS`, imported by
`goalflight_capacity.py`).
Capacity acquire waits on machine, agent, RSS, or cooldown pressure.

Hard caps are RAM/process safeguards, not provider truth. Learn rate pressure from ledger, not constants. `scripts/goalflight_rate_pressure.py` reads recent
ledger failures and emits fallback/halved-cap recommendations after clustered
provider pressure.

Probe workers upward; keep orchestrator provider conservative.
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
(>10s), report event wakes and, if none, sample compact state for a user update
within each 15-minute window unless context is tight. Full rules:
`protocols/user-status-cadence.md`; when tight, append one RESUME-NOTES line.

## Dispatch Model

Two orthogonal axes:
- Iteration: Goal-loop for convergence; one-shot for bounded work; controller-direct needs held context and no fleet stall.
- Goal-loop returns converged result, never draft: plan/act/test/self-review until green.
- Comms shape: `controller-direct`, `acp`, or `bash-tail`.
Dispatch CLI workers via `scripts/goalflight_dispatch.py`, never bare background exec.
### ★ Worker worktrees: use the POOL, never `git worktree add`

```bash
python3 <skill-root>/scripts/goalflight_dispatch.py --worktree main   ... # NOT git worktree add
```

`--worktree <ref>` acquires a **pooled seat** prepared at that ref. Seats are
**REUSED**, so the pool sustains that many *concurrent* workers indefinitely —
it is not a budget of total dispatches. Exhaustion refuses and names every held
seat; it never silently falls back to `git worktree add`.

**★ THE SEAT COUNT IS NOT A PARALLELISM CAP. Do not treat it as one.**
Wide fan-out is wanted — parallelism is how this tool earns its keep. What is
being prevented is **worktree sprawl**: disk exhaustion and codedb
over-indexing, both of which scale with the number of checkout ROOTS, not with
how many workers you run. Twenty workers through eight reused seats is healthy;
twenty ad-hoc trees is the problem.

**The failure mode is well documented and recurs:** a controller reads the seat
count as a worker cap, decides it needs more concurrency than that, and
hand-rolls `git worktree add` for the excess. That bypass is how one repo
reached **210 ad-hoc worktrees of 211, 202GB, and a machine at 100% disk**. If
you genuinely need more concurrent seats, raise `GOALFLIGHT_WORKTREE_SEATS` —
**never** work around the pool, and never lower the seat count to "shape"
fan-out.

Reclaim with `scripts/goalflight_worktree_gc.py` (report by default, `--apply`
to remove). Its predicate is deliberately four-part — **merged, clean, unowned
by a live dispatch, not checked out** — so do not hand-roll a cheaper one: a
worker that has not yet reached its first commit looks exactly like an
abandoned tree, and "clean and merged" alone will delete live work.

### ★ A dead worker is usually a RESUMABLE worker — check before redispatching

```bash
python3 <skill-root>/scripts/goalflight_dispatch.py resume <dispatch_id> --prompt-file <brief> --cwd <worktree>
```

A dead worker is not automatically a lost worker: resume when its accumulated context outvalues a clean read (quota death mid-task, partial edits only its author understands), redispatch when the premise moved (fix rounds, steers, reviews — a reviewer must never resume the implementer). See `protocols/dispatch-resume.md`.

**Before deciding, look at the worktree** — `git -C <worktree> status --short`.
A throttled worker very often left uncommitted work that is complete or nearly
so. Discarding it is the expensive mistake, and a clean-looking worktree is NOT
proof the worker did nothing: it may simply not have reached its first commit.

- **Never resume and redispatch.** Two workers in one worktree is a known incident shape. If a
  replacement is already queued for that worktree, do not also resume.
Dispatch defaults detached; `--foreground` only for sync scripts/tests. Capacity
refusal is visible and nonzero. `drain` exists only for the pre-existing backlog.
Do not hand-iterate (>~3 edit/test cycles) what a goal-loop should converge.

Controller entry auto-claims without stealing a live different lease. Prefer one persistent `goalflight_messages.py supervise` monitor that owns the stream, backup doorbell pool, and watchdog and multiplexes them into a single stdout feed, re-arming children itself. By default it replaces each stream keepalive plus already-materialized advisory frontier with one actionable `kind=next` record and suppresses a verbatim-identical payload until the 15-minute floor (content change still wakes immediately): only a freshly empty projection says `Nothing pending`; unavailable or not-yet-observed state retains `goal-flight next`. Default terse mode emits no coverage record and suppresses `live` / `target`; startup writes `{"type":"probe","reason":"stdout-peer-liveness"}`. `--chatty` wires both the raw-forwarding `chatty` control and the distinct `emit_depth` control; `--debug` restores per-tick heartbeat records only. Arm it with **no timeout** for the session life. Never set, tune, or reason about a timeout: a bounded monitor is killed outside the supervisor (no `type=stop`; controller goes deaf without a diagnostic). On Claude Code use `persistent: true` (`timeout_ms` inert; a host-required value is a placeholder, never a knob). In the decomposed fallback, only after supervisor absence is proven, arm one generation-bound `goalflight_messages.py follow --project-root "$PWD" --controller-label <label> --lease-nonce <nonce>` through the host's persistent monitor, never shell `&`; then arm two tracked `listen --listener-slots 2 --report-pending` backup doorbells and one separately tracked `listen --watch-follow` watchdog. The watchdog holds its own generation lock, never consumes a delivery slot, reads durable record age, and treats three missed heartbeat intervals as channel death; the backup witnesses a missing watchdog lock, but all-tracked-task death remains unwitnessed. In the decomposed unsupervised path, `listener-dead` and `watchdog-dead` records carry the exact component re-arm command. Under `supervise`, they keep the reason but omit the component action; recovery is a supervisor restart. Persistent coverage is the shared four-component `live/4` fact and is detectable, not reap-proof. On codex, grok, cursor, opencode, or any host without such a monitor, retain the portable pool of four `listen --report-pending` calls; on each ring (exit 0), process reported or authoritative mail, cursor-CAS settled server-known positions, then restore depth. Exit 5 is settled did-not-arm (dead or mismatched lease nonce): do not treat it as a ring and do not re-arm that nonce. Exit 2 is retryable journal unreadability, not a dead nonce. Background fixed-id `goalflight_status.py --wait <ids>` only for an unclaimed join; exit 3 is mail, not completion. Timers cover non-notifiable external state, never worker completion. Full arming and JSON-line contracts: `protocols/controller-mail.md`.
A persistent (unbounded) monitor can also be killed for output volume: a child that falls behind its siblings re-emits the unread backlog every cycle, the host kills the monitor, no `type=stop` is written, and the controller goes deaf the same way. `supervise` caps that re-emission by envelope identity and names the stuck child (`cursor-lag` / `child-backlog`); distinct envelopes still forward, and a `distinct-withheld` record points at `relay --drain` when distinct volume itself is the risk. `goalflight_session_status.py --text` reports wake deafness and a lease past `renew_deadline_at` that still holds non-terminal dispatches (a roll would quarantine them); do not auto-renew.
Controller-direct: held context, fully stateable edit, clean Axis 2; plan marks do not waive it.
Routing detail: typed dispatch roles; five-layer prompts; parallel forbid lists; split broad chunks; host tool maps; same-provider review policy. See `protocols/dispatch-routing.md`.
Triggered lanes need pinned context and the execute pre-wave check (`worker-context-package.md`).

Fabricated approval rejected: Never invent user approval for a gated step.
Orchestrator dispatch waits for declared readiness requirements. Orchestrator live gate requires supported capability and ready local state. Worker live gate also requires requested transport verified. Discovery probes do not use network or model calls. Discovery probes stay within manifest budget caps.

## Worker Routing

**Permission-pattern warning** (controller-side, when dispatching ACP workers):
**Always use precise patterns** scoped to the chunk's authorized shapes
(e.g. `^./tests/run\.sh$` when the chunk runs the test sweep).
`--permission-allow-tool-title-pattern` fast-paths matching titles only for
the safe subset — hard gates (outside-cwd writes, kind=execute, kind=fetch
without sandbox, write with no in-cwd locations, unknown kinds) always run
first, so a broad `.*` cannot silently authorize destructive ops. OS sandbox
is a defense-in-depth backstop, not a permission-design substitute — pair
`--os-sandbox=read-only` (or `workspace-write` when commits are expected)
with precise patterns. The runner warns at startup when a broad pattern is
paired with sandbox-off. See `scripts/goalflight_acp_run.py` `make_title_allow_policy`.

Default routing by task:

| Task | Default | Fallback 1 | Fallback 2 |
|---|---|---|---|
| Code-writing chunks | `goalflight_dispatch.py` codex worker | Alternate marker-reliable CLI worker (`grok-code` or `moonshot`) with passing write-file probe | Host Agent — LAST RESORT only ‡ |
| Research / web search | `goalflight_dispatch.py` `--agent grok-research` (read-only) | controller-direct | - |
| Reviewer dispatches | per `protocols/review-types.md` (Type-1 find/fix; Types 2/3 via bug-sweep) | stakes carve-down: single concern-diverse reviewer for trivial chunks ONLY [RT-004] | Claude Agent only when others unreachable |
| Planning / decompose | code/planning worker | controller-direct | Claude Agent |
| Anticipatory questions | strongest interactive planner | controller-direct | - |
| Analysis / reflection | controller-direct | - | - |
| Voice-sensitive prose | orchestrator judgment per chunk | - | - |

Give a stronger-reasoning host-subagent tier a modest, deliberate judgment-heavy and read-heavy slice: nonzero in sustained runs, but never drain the limited pool; use it to relieve controller context, not to substitute for abundant CLI-worker capacity. Every judgment-bearing host-subagent prompt MUST begin with `protocols/subagent-preamble.md`. Scouts run before critical-path prompts with unverified premises fire; follow `protocols/scout.md`.

‡ **Host Agent as code executor = LAST RESORT, never a co-equal fallback.** Use
only when EVERY CLI worker (codex, grok-code, moonshot) is genuinely unreachable, not slow.
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

Capacity checks apply default per-agent caps. Per-machine overrides come from
`$GOALFLIGHT_CAPACITY_CONF` else `~/.goal-flight/capacity.local.json`;
`agent_caps` merge over defaults. Hard caps are placeholders, not laws;
provider budgets may be shared by labels.

### Adaptive walkback

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

Status contract requires heartbeat markers for live workers. Heartbeats are files; wake only on transitions. Stale workers trip on manifest stale-after thresholds. Terminal states are closed manifest values. Worker markers use goalflight dispatch transport sequence grammar.

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

`STATUS:` is progress. `READY:` and `COMPLETE:` are terminal-only, and
`FAILED:` is terminal. `RESULT:` is a completed-work summary that may precede
the final marker; watchers terminalize it only after worker exit or no-growth
idle.

`PERMISSION-OK-PROCEEDED:` is ACP-only. Details live in
`protocols/worker-markers.md`.

## State

### State layers

Use three state layers:
- project: git, tests, docs, queue
- machine: capacity leases, dispatch ledger, cooldowns
- conversation: current decisions, unresolved questions, optional controller-only tactical checklist (ephemeral, dies on compaction; never durable state — that is the queue + RESUME-NOTES)

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
   if "no active goal-flight session", stop; you are NOT in a goal-flight run.
3. Read repository `SKILL.md` end-to-end (this file).
4. Find newest RESUME-NOTES:
   `ls -1 docs-private/RESUME-NOTES-*.md | sort | tail -1`
   (canonical: `RESUME-NOTES-<YYYY-MM-DD>[-rev<N>].md`; ISO 8601 date so lexicographic sort is chronological; no topic prefixes).
5. Run store baseline: `python3 scripts/goalflight_status.py` + `python3
   goalflight_task.py list`; if degraded, use the handoff's last store command.
6. Read handoff prose for environment, ideas/decisions, facts, and carriers;
   task tables, dispatch codes, and next lists live in the store.
7. Run status again, then `python3 goalflight_task.py next`; continue the top
   task after compaction or side-mission without waiting for a re-prompt.

## Context Discipline

Read for edits narrowly. Store long artifacts in files and return paths plus summaries.
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
- After ~3 edit/test cycles, dispatch. Controller-direct needs held context and no exploration; non-mechanical patches need another reviewer.
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
