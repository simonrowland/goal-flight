# goal-flight

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Last commit](https://img.shields.io/github/last-commit/simonrowland/goal-flight)
![Stars](https://img.shields.io/github/stars/simonrowland/goal-flight)

goal-flight turns a Claude Code session into the **orchestrator** of a long-running
software project. You bring a goal. The orchestrator collects the requirements worth your
attention, settles the architecture with you before code is written, then dispatches the
work to a range of coding agents: codex, grok, cursor, claude, running locally or on
remote nodes over SSH. Each worker iterates its own plan/act/test/review loop, and every
commit is gated behind independent review. The run's state lives in durable project files
(plan, queue, worker status, review evidence, resume notes) that survive compaction,
restarts, and unattended overnight hours. A multi-hour run lands as reviewed,
one-commit-per-chunk work on your branch; nothing is pushed without your permission.

**Hosts and workers.** [Claude Code](https://claude.ai/code) is the supported
orchestrator. [Codex](https://github.com/openai/codex) is the standard worker; cursor,
grok, claude-cli, and other worker adapters also serve. Orchestrator ports for
Codex, [Cursor](https://cursor.com), and [OpenCode](https://opencode.ai) ship in-repo —
same `SKILL.md`, file-backed queue, and dispatch machinery — but are implemented and
unsupported; the Claude Code wrapper is the maintained path.

**What the orchestrator session is for**: requirements gathering, architecture decisions,
question escalation, and monitoring — not execution. The orchestrator holds enough context
about your project's goal, scenery (constraints, architecture, prior decisions, failure
modes), and intent to exercise discretion and recommend the next move — then dispatches
the work to workers running iterative code-review loops. Your task shifts from supervising
coding to steering design: you check in, ratify or redirect, and answer the questions the
run escalates.

[Quickstart](#quickstart-from-idea-to-dispatched-work) • [What it gets you](#what-it-gets-you) • [Architecture](docs/architecture.md) • [Commands](#sub-commands)

## Quickstart: from idea to dispatched work

Install once:

```bash
# Claude Code (supported orchestrator):
git clone https://github.com/simonrowland/goal-flight.git ~/.claude/skills/goal-flight
```

(Orchestrator ports for Codex / Cursor / OpenCode exist but are unsupported — install
commands are under [Host install notes](#host-install-notes).)

Primary platform is macOS. Linux hosts work too ([docs/hosts/linux.md](docs/hosts/linux.md));
the macOS-only OS-sandbox backstop is absent there, so workers rely on their own sandbox
and approval policies. Native Windows is read/plan only — see [Windows](#windows).

Restart the host, then open your project repo in an orchestrator session. From here the
flow is four moves:

**1. Init the project and meet your workers.**

```
/goal-flight init <topic>
```

Audits the repo, scaffolds the run's durable state (`docs-private/`, a goal statement, an
operator-local `AGENTS.md`), probes which worker CLIs are installed and healthy, registers
codex trust, measures machine capacity, records environment caveats workers will need, and
optionally builds a RAG corpus so workers stop re-reading the same project landscape. You
leave init knowing which agents are available and healthy.
(`/goal-flight doctor` re-checks all of it any time.)

**2. Capture the requirements.**

```
/goal-flight ask-questions
```

Anticipatory subagents read your repo and goal statement, then surface the few clarifying
questions actually worth your time — product choices and trade-offs, not trivia the code
already answers. Your chat answers are treated as requirements and folded into the goal
files: ten minutes of conversation becomes durable project state that survives compaction.
The phase exits with the goal file's **north star** settled — the durable statement of
what the project must do, drafted at init and refined here — and later architecture calls
and reviews are tested against it.

**3. Iterate on the architecture.**

```
/goal-flight decompose-plan [<plan>]
```

Show up with whatever signal you have — a written plan, an architecture doc, or just the
conversation so far. Decompose-plan breaks it into closed chunks (SCOPE / CHECKLIST /
ACCEPTANCE / FORBIDDEN), flags what can run in parallel, and runs a reviewer pass over the
decomposition itself — so the plan gets adversarial scrutiny *before* any code is written.
Disagree with a chunk? Say so in chat; mid-session steering is requirements input, and the
plan revises. (With [gstack](https://github.com/garrytan/gstack) installed, `/office-hours`
and `/plan-eng-review` add structured interrogation at this step.)

**4. Dispatch the work.**

```
/goal-flight execute [--parallel <N>]
```

The orchestrator dispatches each chunk to the best-fit worker, watches status files rather
than babysitting terminals, routes around rate-limited providers, and gates every chunk
behind executor self-review plus an independent reviewer before it lands. The run stops
and asks for permission gates, destructive choices, auth or capacity hard stops, failed
review or test gates, and product choices the plan can't infer; it does not push without
your permission. Come back
hours later to reviewed, one-commit-per-chunk work on your branch, with review evidence
and resume notes on disk. If the session compacts or the laptop sleeps,
`/goal-flight resume` rebuilds the orchestrator's working state from files and keeps
going.

> **Working signal, not rigid gates**: the skill pins a `goal-<topic>-<date>.md` file at
> init for compaction-survival, but it's an anchor — not a contract. `decompose-plan`
> proceeds on whatever signal exists (the goal-statement when present, or the plan source,
> architecture doc, and in-session conversation), surfacing any inferred assumptions as
> inline-office-hours backlog items the user can validate during the run. Show up with
> "here's my architecture doc plus ten minutes of context-setting chat" and
> `decompose-plan` proceeds from there. The premises file accumulates validated answers as the run progresses.
> **DRAFT goal-statement is fine** — `decompose-plan` proceeds anyway; sharpen any time by
> editing `docs-private/goal-<topic>-<date>.md` directly.

`/goal-flight` with no args prints `SKILL.md` — the full pattern reference.

**Terms you'll see**: a *chunk* is one closed unit of work (scope, checklist, acceptance
criteria, forbidden paths); *goal-mode* is a worker loop that iterates plan/act/test/review until its
chunk converges; *ACP* ([Agent Client Protocol](https://agentclientprotocol.com)) gives
the orchestrator structured worker events, with *bash-tail* (a watched log file) as the
fallback; the *RAG corpus* is an optional set of curated project notes workers read
instead of rediscovering the codebase; *walkback* is the rate-pressure response that
reroutes work away from a limited provider.

## What it gets you

- **A range of agents, local or remote.** Workers include codex, grok, cursor, and
  claude-cli over ACP or bash-tail; the fleet layer runs the same dispatch on remote SSH
  nodes. The orchestrator picks executor and iteration pattern per chunk (one-shot,
  goal-mode loop to convergence, or controller-direct for trivial edits) and divides usage
  and rate limits across vendors, routing around a pressured provider instead of stalling
  the run.
- **Independent review before commit.** Every `/goal` chunk runs a 7-category adversarial
  self-review to convergence inside the worker loop; commit-worthy chunks then get an
  independent reviewer pass (via [gstack](https://github.com/garrytan/gstack)'s `/review`
  when installed), and milestone reviews sweep at configured cadence with concern-diverse
  lenses. Each new bug shape caught is minted as a durable bug-class predicate; your
  already-reviewed code and the saved review archive are swept for further instances, and
  the predicate joins the standing lenses for every later review
  (`protocols/review-mining.md`).
- **A curated project corpus (RAG).** Optionally at init — or via `build-corpus` later —
  goal-flight distills your repo and docs into curated corpus slices under
  `docs-private/rag/`. Dispatch briefs anchor to those files instead of re-pasting
  project context into every worker prompt, so the read cost lands on workers and the
  corpus is written once.
- **Verification-first dispatch.** Wrappers point at files for the agent to investigate,
  not pre-pasted "facts" that go stale on the timescale of minutes. Frontier models trust
  orchestrator-text uncritically; pointers force them to re-verify against live disk and
  surface drift.
- **Multi-hour unattended runs with light supervision.** Check in periodically or respond
  to decision notifications. The orchestrator's context primarily holds architecture, plan,
  and metadata (queue state, recent commits, in-flight dispatch headers); real work happens
  in subagent context windows, and the orchestrator escalates to you only when a decision
  is genuinely yours.
- **Deterministic ops scripts.** Capacity leases, dispatch ledgers, compact status,
  structured ACP monitoring, rate-pressure detection, and file-backed review jobs live in
  `scripts/goalflight_*.py`; the model spends its judgment on what the numbers mean.
  Doctor checks runtime readiness and worker-CLI currency, so you know when to run
  `/goal-flight update`.

## How it differs from the alternatives

- vs. **running one coding session to context exhaustion** — the orchestrator doesn't
  itself write code. It dispatches and verifies, holding requirements and architecture
  while workers burn their own context windows; the run outlives any single session.
- vs. **cloud agent swarms or editor agents** — workers run on your machine (or your SSH
  nodes) with your CLIs and provider subscriptions; the orchestrator coordinates them from
  your host session.
- vs. **writing prompts manually** — make the plan, not the code. The skill asks a frontier
  model to decompose your plan into chunks, flagging what can run in parallel and what can
  have a `/goal` pattern. Every `/goal` reviews to convergence by default; the 7-category
  adversarial self-review runs inside the loop until reviews pass, so the orchestrator
  never sees a non-converged result.

## Sub-commands

| Command | What it does |
|---------|--------------|
| `/goal-flight init <topic>` | Tool check, repo audit, scaffold, codex-trust registration |
| `/goal-flight decompose-plan [<plan>]` | Break a plan into `/goal` chunks with parallel reviewer pass |
| `/goal-flight ask-questions [<scope>]` | Anticipatory subagents; surface clarifying questions |
| `/goal-flight execute [--parallel <N>]` | Per-chunk loop; sequential default, parallel-safe opt-in |
| `/goal-flight doctor` | Read-only health check for plugin/package/runtime readiness, model currency, rate-pressure |
| `/goal-flight update` | Pull latest goal-flight from origin + run each worker CLI's self-update |
| `/goal-flight build-corpus [<flags>]` | Extend / rebuild the optional RAG corpus |
| `/goal-flight resume` | Rebuild RESUME-NOTES from current git state |
| `/goal-flight goal <SLUG>` | Append one goal to the queue |
| `/goal-flight register-codex [<path>]` | Register a project as codex-trusted |
| `/goal-flight validate-dispatch [<slug>]` | Render a chunk's dispatch wrapper without dispatching |
| `/goal-flight validate-queue [<path>]` | Schema-check the goal-queue |

Plus an opt-in self-delegation pattern via `/fork` — orchestrator writes a marker contract;
forked session detects via env var and follows the contract; orchestrator monitors compact
status. See `protocols/self-delegation.md`; fork instructions are not always-loaded.

Detailed operating procedures are split into load-on-demand files under
`protocols/`. The always-loaded `SKILL.md` is intentionally small.

## Dispatch routing (two orthogonal axes)

You normally don't need this section unless you're tuning worker selection.

**Iteration pattern** — how many turns the chunk needs:

| Pattern | When | Cost |
|---|---|---|
| **`controller-direct`** | Trivially small (single-file, < ~30 LoC), OR orchestrator already has the session-loaded context a fresh subagent would have to re-discover | Inline; no subagent |
| **one-shot subagent** | Default for most chunks. Frontier model picks the executor target based on chunk shape | One subagent dispatch per chunk |
| **goal-mode loop** | Multi-step refactor, code migration, prototype implementation, converge code to ground-truth — anything that benefits from plan/act/test/review-to-convergence | Multi-hour autonomous session (codex `/goal` natively, or orchestrator-driven iteration loop) |

**Comms shape** (orthogonal axis) — how the orchestrator observes the worker. Goal-flight
uses the [Agent Client Protocol](https://agentclientprotocol.com) wherever the worker has
an adapter (codex / cursor / claude / grok all do today); bash-tail with a `tail -f`-style
marker-grep watcher is the cold-storage fallback. ACP composes with `goal-mode` for any
worker; `goal-mode + bash-tail` composes only with codex `/goal` today (codex emits a
Final-response marker the watcher detects; other workers' headless modes don't).

The orchestrator picks executor + comms per chunk based on chunk shape, available adapters,
and the rate-pressure walkback's recent observations. The shipped routing defaults lean
toward sub-billed workers (codex / cursor / grok) for code-writing — calibrated against the
maintainer's current vendor plans, not a project-wide prescription. Adjust to your
environment by editing the routing table in `SKILL.md` "Worker Routing"; the walkback
adapts dynamically when any one provider gets pressured.

## Multi-node fleet (1.0)

For remote workers over SSH, bootstrap a fleet store and use `goalflight_fleet.py`
dispatch / watch / reconcile. OpenCode, Cursor, Codex, and Claude ACP workers
can run on registered nodes while the orchestrator stays local. See
[docs/fleet.md](docs/fleet.md) for the operator guide; live smoke:
`GOALFLIGHT_LIVE_SSH=1 ./tests/manual/test_fleet_live_smoke.sh`.

Unified CLI: `bin/goalflight <domain> <resource> <verb>` (action router over
`config/actions/`).

## When NOT to use this

- **One-off scripts or quick fixes.** Overhead unjustified for <8 chunks or <2000 LoC
  delta. Pre-flight gates auto-skip the RAG corpus for projects this small.
- **Pair-programming sessions.** Designed for unattended runs. If you're steering every
  turn, the wrapper overhead just slows you down.
- **Sensitive operations the orchestrator shouldn't autonomously trigger.** Production
  deploys, prod data writes, credential rotations — human-in-the-loop is the point.
- **When speed matters more than rigor.** The review-to-convergence design targets
  reference-quality code — its home domain is scientific programming. For a small one-shot
  task it is slower than just writing the script.

## Companion tools

Review skills (recommended — the review gates lean on them):

- **[gstack](https://github.com/garrytan/gstack)** — Garry Tan's skill pack provides
  `/review`, `/office-hours`, `/plan-eng-review`, `/cso`, `/investigate` for both Claude
  Code and codex. Goal-flight invokes `/review` as the **default independent reviewer** for
  chunk-level pre-commit review (`protocols/chunk-review.md`) and for milestone reviews
  (`protocols/milestone-review.md`, gstack + concern-diverse sweep); `/office-hours` covers
  fuzzy-goal interrogation at init. **Optional** — without gstack, goal-flight falls back
  to local prompts at `prompts/gstack-claude-review.md` +
  `prompts/gstack-codex-challenge.md` (executor self-review still runs its seven-category
  pass). With gstack installed, you get consistent severity-ranking framing across both
  review lenses on long runs.
- **autoreview** — Complementary diff-local pre-commit pass (`protocols/chunk-review.md`,
  `./scripts/autoreview.sh`). Runs in parallel with gstack at chunk level when the
  orchestrator chooses; does **not** replace gstack as the default review path. Catches
  diff-local issues (API footguns, missing tests on touched paths, regression invariants)
  that a structural reviewer may not prioritize. Requires upstream autoreview (typically
  the Cursor autoreview skill or `AUTOREVIEW_HELPER`); doctor reports WARN when absent.

Also recommended:

- **[context-mode](https://github.com/simonrowland/context-mode)** — MCP plugin that
  offloads large command outputs (diffs, integration test runs, codex tail files, large
  greps) to an FTS5 sandbox the orchestrator queries by pattern. On long runs, raw tool
  output fills the orchestrator's context and triggers early compaction; context-mode
  keeps that output out of the session.
- **[codedb](https://github.com/justrach/codedb)** — code-intelligence MCP (tree,
  outline, symbol, search, deps) the orchestrator uses to anchor dispatch briefs to exact
  files and lines, and to spot-check worker findings, at a fraction of the context cost
  of grep-and-read. Optional; use it when indexed lookup beats a broad grep — most useful
  on large or unfamiliar codebases.

## Host install notes

Orchestrator ports for Codex, Cursor, and OpenCode are implemented and unsupported; the
Claude Code wrapper is the maintained path. To install a port anyway:

```bash
git clone https://github.com/simonrowland/goal-flight.git ~/.goal-flight && cd ~/.goal-flight
./install.sh cursor /path/to/your/project
./install.sh opencode /path/to/your/project
./install.sh codex
```

After source `SKILL.md`, `commands/`, `protocols/`, `templates/`, or `adapters/`
changes, copied host installs must be resynced from the source repo with
`./install.sh <host>` unless the host skill is symlinked to the source. The
doctor's `installed_skill_drift` probe hashes only the installed `SKILL.md`
file (per host); a hash divergence WARN there means the wrapper is stale,
which usually correlates with the other directories being stale too. Run
the resync command in the probe's `resync_command` field; text mode prints
`installed_skill_md_hash` WARNs.

Same flags via `setup.sh`: `--cursor-install`, `--opencode-install`, and
`--codex-install` (each implies `--apply --yes`). Dry-run, link-to-Claude, and
agents-standard paths are in [docs/hosts/cursor.md](docs/hosts/cursor.md) and
[docs/hosts/opencode.md](docs/hosts/opencode.md).

After any install, run doctor:
`python3 scripts/goalflight_doctor.py --project-root /path/to/your/project`.

### Windows

Windows runs goal-flight through WSL. Ask your agent to set it up — starting with:

```powershell
wsl --install
```

then install goal-flight inside the distro as on Linux. Native Windows (no WSL) is a
read/plan control plane only: doctor, status, and ledger reads work; dispatch honestly
refuses. Full details — WSL baseline, capability matrix, two-install procedure, launcher
and CRLF caveats — are in [docs/hosts/windows.md](docs/hosts/windows.md).

## Maintainer test tiers

Default `./tests/run.sh` stays hermetic and cheap. Set
`GOALFLIGHT_AUTOREVIEW=1` to include `tests/bash/test-autoreview-smoke.sh`,
which runs `scripts/autoreview.sh --engine claude` against a known-good fixture
commit through `scripts/autoreview_claude_acp`. Each invocation consumes one
Claude ACP-sub-billed autoreview pass.
Set `GOALFLIGHT_ACP_LIVE=1` to include the real codex-acp dispatch smoke.

## Adapting

This skill ships tuned for high-accuracy scientific programming but the patterns
generalize. Workflow: clone the repo, open it in your orchestrator host (Claude Code
wrapper today), and ask the host to adapt the skill for a domain project with a north
star, verification command, invariants, and any domain-specific self-review categories. A
subagent can read the whole thing, propose a diff, and apply it in one pass.

Main tuning knobs:

- **North star + asking discipline + token bias** — `SKILL.md` hard-conventions section.
- **Self-review categories** — `prompts/executor-self-review.md` lines 14–35. Seven
  abstract categories; add domain-specific ones (e.g. SCHEMA GAP for ETL, A11Y GAP for
  frontend).
- **Milestone review cadence** — milestone review flights run at configured cadence or on
  `[milestone]`-marked chunks (`commands/execute.md`, `protocols/milestone-review.md`);
  mark chunks in the goal queue or adjust the cadence there.
- **RAG corpus slice mix + word budgets** — `templates/rag-corpus-schema.md.tpl`.
- **`/goal` mode prompt shape** — `templates/codex-goal-prompt.md.tpl` (Objective /
  Workspace / Rules / Acceptance / Test gates / Final response schema).

## License

MIT — see [LICENSE](LICENSE).
