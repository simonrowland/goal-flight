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

> ⚠️ **Read this skill end-to-end, including Worker Routing, State, and Context Discipline** before acting; also read Do Not. The back half carries routing, state, marker, rate-limit, permission, and safety contracts.

This checked-in `SKILL.md` is the compiled controller distillation of
`docs/controller-behaviours.md`. It is the Claude Code-compatible wrapper for
the portable core. Keep front matter and `allowed-tools` compatible until
generated wrappers own host bindings, tool names, invocation details, and
packaging.

## Activation Check

**Is goal-flight active in this project?** Run
`python3 <skill-root>/scripts/goalflight_session_status.py --text` before
auto-loading the rest of this skill. If the verdict is "no active
goal-flight session", you are NOT in a goal-flight run — do regular coding
without loading the back half. Only load end-to-end when the verdict is
"active" or when the user explicitly invokes `/goal-flight <command>`.

**Skill-freshness + designated-controller check.** If your context has a
"skill: goal-flight (previously invoked)" reminder but you can't quote
this preamble verbatim, your loaded skill body is STALE — system reminders
carry truncated content across compactions and silently drop load-bearing
rules. Re-invoke `/goal-flight` to reload SKILL.md end-to-end before
acting on its rules. Then check your terminal session ID
(`scripts/goalflight_session_status.py --ensure-session`) against the
active queue's `current_session.id` — if they match, you are the
designated controller; if not, surface to user before claiming.

## Per-host pointers

Per-host pointers tell non-native controllers where their installed wrapper lives.
If you are a non-Claude controller (codex, grok, cursor, opencode), load your
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
Controller behaviour probes run through portable host adapters, not host-specific print-mode shortcuts.

## Navigation map: behaviour -> SKILL anchor -> protocol/script

| Topic | SKILL anchor | Protocol/script |
|---|---|---|
| **is goal-flight active here?** | preamble above | `scripts/goalflight_session_status.py --text` |
| status preflight | Session Pre-Flight | `protocols/session-preflight.md`, `scripts/goalflight_status.py`, `scripts/goalflight_doctor.py` |
| **in-flight dispatch monitoring** | Session Pre-Flight | `scripts/goalflight_status.py --json`, `scripts/goalflight_watch.py` (ACP), `scripts/watch-dispatch-tail.sh` (bash-tail) |
| **active leases / what's in flight** | Capacity and rate limits | `scripts/goalflight_capacity.py status` |
| **per-chunk status snapshot** | Session Pre-Flight | `python3 <skill-root>/scripts/goalflight_chunk_summary.py --slug <slug> --json` |
| autonomous throughput | Autonomous throughput | `commands/execute.md`, `commands/goal.md` |
| **chat as requirements** | Chat as requirements | `commands/goal.md`, `protocols/user-status-cadence.md` |
| context lints | Autonomous throughput | `protocols/engagement-lint.md`, `foreground-duration-hook.md` |
| user-status-cadence | User progress reporting | `protocols/user-status-cadence.md` |
| chunk-vs-milestone review | Review layers | `protocols/chunk-review.md`, `protocols/milestone-review.md` |
| dispatch axes (per-task routing table is in Worker Routing) | Dispatch Model, Worker Routing | `protocols/dispatch-routing.md` |
| **worker permissions / YOLO warning** | Worker Routing | `scripts/goalflight_acp_run.py` `make_title_allow_policy` |
| **worker blocked: controller takeover** | Worker Routing | `protocols/dispatched-worker-recovery.md` |
| rate limits & caps | Capacity and rate limits | `scripts/goalflight_capacity.py`, `scripts/goalflight_rate_pressure.py` |
| worker markers | Worker Markers | `protocols/worker-markers.md`, `scripts/goalflight_watch.py` |
| resume/compaction (canonical reload order) | State | `commands/resume.md`, `protocols/state-handoff.md`, `scripts/goalflight_session_status.py` |
| context discipline | Context Discipline | context-mode, `scripts/goalflight_*.py` |
| **Do Not / safety gates** | Do Not | (read-end-to-end is load-bearing for safety) |

## Controller Contract

Use this wrapper for work too large for one uninterrupted session: decomposed
implementation, long refactors, review flights, resumable queues, or unattended
dispatch. The controller manages context and verification; it does not hoard
every file, log, or worker transcript in conversation.
Controller context is scarce; delegate iteration so only the converged conclusion returns.

Always:
- read the invoked command file and only its referenced protocols
- run helpers for machine facts, status, logs, capacity, and tool probes
- keep raw logs and long reviews in files; reason over compact summaries
- Analyze/search/count/filter with procedural code or context-mode

Never load fork, ACP, corpus, review, or tool-specific details just because the
skill loaded. Load those protocols on demand.

## Session Pre-Flight

For non-trivial commands, use `protocols/session-preflight.md`.

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

## Review layers

Review layers: executor self-review, chunk review, milestone review.

| Layer | Gate | Default |
|---|---|---|
| Executor self-review | In-worker prompt before handoff | Executor self-review covers seven categories before handing off a chunk |
| Chunk review | Every commit-worthy chunk | default gstack `/review`; `./scripts/autoreview.sh` as a complementary parallel option |
| Milestone review | K-commit cadence or `[milestone]` chunks | `protocols/milestone-review.md`, gstack `/review` + concern-diverse sweep |

On chunk completion, dispatch gstack `/review` before committing.
Reviews go through gstack `/review` and `/challenge`; do not hand-roll review prompts.
Reviewer misses become regression tests, not trust exemptions. Write review rubrics before first wave dispatch.
Reviews are one-shot; fixes loop to green and re-review.
Diversify reviewer concern, not just model. Use consolidation review for cross-slice contradictions.
Milestone review is a separate gate from chunk review.

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
  ~5KB MUST instruct the worker to write findings to
  `docs-private/research/<date>-<slug>/findings.md` and return only
  `READY: <path>` plus a one-paragraph TL;DR + severity-tagged finding
  count. The controller reads the TL;DR; opens the file only when TL;DR
  signals a real action. Returning a 9KB report inline defeats the
  dispatch — the bytes land back in controller context anyway.
- Read >5KB without an expected Edit follow-up within 2 turns → use
  `Agent` (Explore for read-only, general-purpose for tool-using
  investigation) with a defined prompt instead. Recon-Reads pull the
  full body into controller context; an Agent dispatch returns a
  conclusion at ~10x compression.
- No `tail -f` in conversation. Use status files instead:
  - Aggregate snapshot: `python3 <skill-root>/scripts/goalflight_status.py --json`
  - ACP dispatch: `python3 <skill-root>/scripts/goalflight_watch.py --pid <pid> --tail <tailfile> --status-json <path>`
  - Bash-tail dispatch: `<skill-root>/scripts/watch-dispatch-tail.sh` (content-aware completion watcher with terminal-marker / pid-dead / idle / controller-dead exit codes)
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
- During an active goal-flight run, keep shipping through decompose -> execute
  until the queue is done or a real blocker stops you.
- Report progress at least every 15 minutes unless context is tight.
- Workers escalate sandbox / permission / tool blocks via `BLOCKED:` and return to the controller. They do NOT execute workarounds (alternate APIs, git plumbing, inline content dumps when a file-write is blocked). Push and out-of-standard-path commits are the controller's call, not the worker's. Detail + session examples: `protocols/dispatched-worker-recovery.md` §"Worker bypass anti-pattern".
- Keep `docs-private/` private.

## Capacity and rate limits

Consider capacity before any worker spawn. Capacity checks apply default
per-agent caps from `scripts/goalflight_capacity.py` (`DEFAULT_AGENT_CAPS`).
Capacity acquire waits on machine, agent, RSS, or cooldown pressure.

Hard caps are RAM/process safeguards, not provider truth. Learn rate pressure from ledger, not constants. `scripts/goalflight_rate_pressure.py` reads recent
ledger failures and emits fallback/halved-cap recommendations after clustered
provider pressure.

Probe workers upward; keep controller provider conservative. Controller budget
loss can end the interactive session; worker-provider pressure can be rerouted.
Bound dispatch hangs with idle and quiet timeouts. Terminal leases leave active capacity after completion.

## Autonomous throughput

Goal Flight exists so long work survives compactions and unattended hours. The
controller advances the queue; it does not poll the user for presence.

When the user invoked goal-flight, approved a plan, or gave scope:
- Keep working through code, tests, queue/ledger/resume updates, review, and
  commits until decomposition/execute drains or a real blocker stops it.
- Default is continue, not confirm.
- Do not use engagement prompts.
- Record non-blocking uncertainty in files, then proceed with the plan default.
- Commits during execute follow **one commit per completed chunk**.
- Push to a remote only after the relevant tests pass and the user has permitted publish.

Stop only for `USER-NEED` / `USER-CONFIRM` blockers: permission, destructive
or irreversible action without a plan default, product choice the plan cannot
infer, auth/capacity hard stop, or explicit command gate.

## Chat as requirements

Controller chat is requirements input, not an inline editor command. Mid-session
asks are steering/architecture/scope input. Append them to the active goal queue
or promote them to a plan revision plus re-review when they change scope.

Do not task-pivot or inline-edit on receipt. Plan before editing when scope is unsettled. Prepare ambiguous questions before asking the user. Relay USER-NEED through controller, not worker chat. chat alone is not the backlog.

### In-flight steer mailbox

Steer live bash worker via `scripts/goalflight_dispatch.py steer <id> '<msg>'`; `--list` shows mailbox/acks; worker polls each iteration + before git; ACP inline pending #8.

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
Do not hand-iterate (>~3 edit/test cycles) what a goal-loop should converge.

Use ACP or bash-tail plus status polling; do not block on editor task panes.
Abstract tool roles resolve through host tool-name maps. Type dispatches as executor, reviewer, or planner. Dispatch prompts need the five-layer wrapper. Parallel fix clusters need explicit forbid lists. Split chunks likely to touch many files. Controller-direct only for tiny or plan-marked chunks. Same-provider policy controls review routing trust.

Fabricated approval rejected: Never invent user approval for a gated step.
Controller dispatch waits for declared readiness requirements. Controller live gate requires supported capability and ready local state. Worker live gate also requires requested transport verified. Discovery probes do not use network or model calls. Discovery probes stay within manifest budget caps.

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
| Code-writing chunks | ACP code worker chosen per chunk | Alternate ACP code worker | Claude Agent |
| Reviewer dispatches | gstack `/review` via review worker + concern-diverse sweep | any one alone | Claude Agent only when others unreachable |
| Planning / decompose | code/planning worker | controller-direct | Claude Agent |
| Anticipatory questions | strongest interactive planner | controller-direct | - |
| Analysis / reflection | controller-direct | - | - |
| Voice-sensitive prose | controller judgment per chunk | - | - |

Use adapter manifests and doctor probes for current host/model details; do not
hardcode yesterday's model list. Cursor internal models do not need passthrough
unless that chunk explicitly needs the vendor. ACP SDK dispatch uses the managed
`agent-client-protocol==0.10.*` venv unless overridden.

### Hard caps

Hard caps are defined in `scripts/goalflight_capacity.py`; they are placeholders,
not laws. Capacity checks apply default per-agent caps and provider-level rate
budgets may be shared by multiple labels.

### Adaptive walkback

Adaptive walkback reads the ledger through `scripts/goalflight_rate_pressure.py`.
If one provider shows repeated recent rate-limit signatures, re-route next work,
surface status, or reduce effective cap. No autonomous capacity mutation in v1.

### Controller-provider asymmetry

Controller-provider-asymmetry: protect the controller's own provider more
conservatively than worker providers. Worker failures can reroute; controller
failure can strand the user.

Bash-tail recipes live in `protocols/legacy/bash-tail.md`; forking lives in
`protocols/self-delegation.md`; worker-blocking recovery lives in
`protocols/dispatched-worker-recovery.md`.

## Verification and test gates

Before each chunk commit: focused tests green. Background tests are pending until results are read. `./tests/run.sh` is the repo-wide gate when chunk scope or release risk justifies it. GOALFLIGHT_AUTOREVIEW=1 is an optional maintainer tier, not a default review path.

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
- `COMPLETE:`

Details live in `protocols/worker-markers.md`.

## State

### State layers

Use three state layers:
- project: git, tests, docs, queue
- machine: capacity leases, dispatch ledger, cooldowns
- conversation: current decisions and unresolved questions

Repository files are the canonical memory backend.
Memory writeback requires migration lock ownership.

### Status plane and liveness

Use one status plane across transports.
Ledger liveness matches PID plus process identity.
Never `pgrep` for worker liveness; use dispatch/status identity.
Isolate pidfiles per controller session.
Classify ACP failures as upstream, local, or repo.

### Resume and handoff

Remote workers execute; controller remains designated surface.
Propose AGENTS.md changes as diffs only.

On resume or after sleep:

```bash
python3 <skill-root>/scripts/goalflight_status.py --json
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
5. Read newest queue file at `docs-private/goal-queue-*.md` for the chunk
   table + frontmatter `state` and `current_session`.
6. `python3 <skill-root>/scripts/goalflight_status.py --json` for live
   capacity + ledger.
7. Continue from status, not from chat memory.

## Context Discipline

Read for edits narrowly. Analyze/search/count/filter with procedural code or
context-mode. Store long artifacts in files and return paths plus summaries.
Prebuild corpus; do not inline landscape per dispatch. Keep worker-context optional when canonical docs fit.

When in doubt, move deterministic logic into `scripts/goalflight_*.py`; keep the
model responsible for judgment: choosing next action, interpreting findings, and
deciding whether a warning matters.

## Trigger and publish discipline

Git-visible trigger aliases stay out of filenames, manifests, and commit
messages. Push to a remote only after the relevant tests pass and the user has
permitted publish.
Git-visible trigger aliases stay out of filenames, manifests, and commit messages.

## Do Not

- Do not paste long logs, diffs, JSONL streams, or review transcripts.
- Do not treat PID alone as process identity.
- Do not hand-iterate (>~3 edit/test cycles) a chunk in controller context — goal-loop it. Controller-direct is for tiny or judgment-only edits.
- Do not let one goal-flight session consume all machine capacity.
- Do not silently skip review when a provider hits rate or session limits.
- Do not load `/fork` instructions by default.
- Do not substitute print-mode prompts for live behaviour probes or canonical review dispatch.
- Forbidden shell families never enter controller dispatch.
- Auto-approve detection is strict-fail, not advisory.
- Irreversible operations require explicit user gate.
- Secrets stay out of probes, wrappers, and logs.
- Forbidden exec args are rejected in every dispatch surface.
- Risky exec args need explicit justification before use.
- Inline permits use request, decision, and ack files.
- Install actions need user gates and backup paths.
