---
description: "Break a plan into verified goal chunks."
---

# decompose-plan [<plan-file>]

Read:

- `protocols/premises.md`
- `protocols/dispatch-routing.md`

Break a plan into numbered `/goal` chunks. The plan source can come from:

1. **A file path argument** — `/goal-flight decompose-plan docs-private/refactor-plan.md`
2. **An in-context conversation** — no arg; the plan was discussed in this session (the user reviewed the plan with you and it's in the chat log)
3. **A file Read'd earlier in this session** — no arg; you find the most relevant from session context

If the source is ambiguous, ask the user which plan they want decomposed before proceeding.

## Steps

### 0. Anchor against available context (read, don't gate)

The goal here is not to enforce a formal goal-statement gate — that would turn the goal-statement into the same kind of invisible-assumption document the skill exists to surface. The goal is to anchor the decomposition against *whatever signal the user has already given* about what they actually want, with explicit visibility into anything inferred.

Read whatever exists, in priority order:

1. **A pinned goal-statement** at `<repo-root>/docs-private/goal-<topic>-*.md` (or legacy `<topic>-goal-statement-*.md`). Read as the canonical anchor when present.
2. **The plan source itself** — file argument, in-session conversation, or referenced architecture doc (handled in step 1).
3. **Conversation context** — anything the user told you about intent / success criteria / non-goals in this session.

Handle the goal-statement's `Status:` field as information, not as a refusal trigger:

- **`Status: CONCRETE` or absent (= concrete-by-default)**: proceed normally; keep content in mind for steps 4.5 and 6.
- **`Status: DRAFT — <reason>`**: proceed, but surface a high-priority inline-office-hours backlog item ("Goal-statement is DRAFT (`<reason>`). The decomposition will proceed on the plan source + conversation; do you want to interrogate the goal first via `/office-hours`, or sharpen the file directly, or just steer mid-run as decomposition surfaces the implicit-goal questions?"). Don't bail.
- **Goal-statement absent entirely**: proceed on the plan source / conversation; surface a backlog item ("No pinned goal-statement found. The decomposition will use the plan source and our conversation as the anchor; if you want a more durable anchor across compactions, recommend `/goal-flight init <topic>` later — for now, proceeding."). Don't bail.

**Why this is right**: see `protocols/premises.md`. The whole point of premise-checking is to surface invisible assumptions for validation rather than acting on them silently. A rigid "refuse if goal isn't pinned in a specific file format" gate would do the opposite — block on a single static document the user may not have engaged with, treating it as the only acceptable form of "user intent." User intent lives in the conversation, in architecture docs, in plans, in commit messages; the goal-statement is one durable anchor among many. Surface gaps in the anchor as backlog items; don't refuse the work.

### 0.4. Anticipatory rate-pressure check (silent unless something to surface)

Before generating a chunk plan that will spawn many workers, check that
the provider budgets are healthy. A decomposition output of, say, 20
chunks plus parallel reviewers is exactly the workload shape that can
push the controller's session budget past the rate-limit cliff.

```bash
python3 <skill-root>/scripts/goalflight_rate_pressure.py --json
```

Behavior:
- `providers_under_pressure` empty → continue silently. Don't pre-warn
  about hypothetical limits; the controller has the routing table
  defaults and the caps. Silence is correct here.
- `providers_under_pressure` non-empty → surface a single STATUS line
  ("rate-pressure on <provider>; will lean on <fallback> for chunks
  this decomposition spawns") and skew the chunk tagging accordingly
  (prefer `[acp]` toward fallback providers in the queue).

This is the "anticipate before spam" check. The same probe lives in
`commands/execute.md` step 1 for the per-dispatch view; do not duplicate
the call here unnecessarily.

### 0.5. Offer a polish-skill pass on the plan source (optional, non-blocking)

Before decomposing, **offer the user a polish-skill pass** on the plan source (the file arg, the in-session conversation, or whichever signal you found in step 0). The polish-skill class has two sub-classes with different outputs:

- **Interrogative skills** (return *validated user answers* — load these into the premises file): gstack `/office-hours` (YC-style forcing questions), `/grill-me` (adversarial interrogation). These ask the user; the user replies; the replies are the artifact.
- **Reviewer skills** (return *findings*, not answers — surface as backlog for the user to triage): gstack `/plan-eng-review` (engineering critique), `/eng-design-review` (design review). These produce P0/P1/P2/P3 lists about the plan; nothing in the output is itself a validated premise.

Most often `/office-hours` (interrogative, default). The frontier model can pick differently based on what the plan most needs, or embody the gist directly in its own assistant text. See `protocols/premises.md` for the polish-skill class and the architectural rule.

**Architectural rule** (mirror of `commands/init.md` step 2.5): user-interrogation runs on the controller via `ask_user` plus, when useful, the controller host's `delegate` equivalent for an interrogative skill. In the current Claude wrapper, that means the wrapper-owned delegate path; other hosts use their adapter's delegate equivalent. The controller may also embody the gist directly in assistant text. **Never** dispatch an interrogative skill to a non-user-facing worker — workers have no user-facing channel. Reviewer skills (the second sub-class above) CAN dispatch as workers because they return findings rather than ask questions.

Prompt the user (concisely):

> "Before I decompose this plan, want a polish-skill pass to sharpen it? gstack `/office-hours` is the default (forcing questions on who's the user, what changes when done, narrowest wedge, success criteria) — interrogative, returns your validated answers. Other interrogative: `/grill-me` (adversarial). Reviewer skills (return findings, not answers): `/plan-eng-review` (engineering critique). Or I can skip and just decompose the plan as-is — premise-checks will surface during execution. (y for office-hours / `<name>` for a specific skill / n to skip)"

Three outcomes:

- **Accept interrogative**: invoke through the controller host's `delegate` equivalent (current Claude wrapper: wrapper-owned delegate path; other hosts: adapter delegate equivalent) or embody the gist; distill the user's validated answers into `docs-private/premises-<topic>-<today>.md` Validated section; use the polished plan as input to step 1.
- **Accept reviewer**: invoke through the controller host's `delegate` equivalent or dispatch a review worker through the chosen adapter; surface the P0/P1 findings to the user; per-finding, the user decides whether to revise the plan, accept the finding into the goal-statement, or note as a known-limitation. Findings are NOT premises; they're todos against the plan. Use the (possibly revised) plan as input to step 1.
- **Decline**: proceed directly to step 1 with the plan as-is. The inline-office-hours backlog (per `protocols/premises.md`) will surface premise-checks during execution — declining here doesn't disable that.
- **Already-concrete plan**: if step 0 found a `Status: CONCRETE` goal-statement AND the plan source has explicit acceptance criteria, you can skip the offer silently — the polish is unlikely to surface anything new.

This is the front-end complement to inline-office-hours' per-chunk premise-checks during execution. Polish the upstream artifact before decomposing; pepper for drift during execution.

### 1. Establish plan source

- File arg present: read it (delegate to an explorer worker if >300 lines; current Claude wrapper: Explore subagent; other hosts: adapter delegate equivalent).
- No arg: scan the recent conversation for plan-shaped content (numbered phases, "step 1 / step 2", architecture sections, refactor discussions). Summarize what plan you think they mean (1-2 sentences) and confirm with the user before proceeding.
- If multiple candidates: list them and ask which.

### 2. Spawn drafter + analyst (sequential — analyst depends on drafter)

**Drafter** (general-purpose worker via the host `delegate` operation; current Claude wrapper: general-purpose subagent; other hosts: adapter delegate equivalent): produce the numbered `/goal` decomposition.

> "Read the plan below. Decompose it into N self-contained `/goal` chunks. Each chunk must have SCOPE / CHECKLIST / ACCEPTANCE / FORBIDDEN sections (skeleton at the bottom of this prompt). Smallest-first; imperative voice. Number them 1..N. Surface anything in the plan that resists decomposition or requires controller-side judgement. Plan: <paste plan text or path>.
>
> Per-chunk skeleton:
> ```
> ## <N>. /goal <SLUG>
> STATUS (optional — pin observed reality if goal is touched post-write)
> SCOPE
> <1-3 sentence problem + boundary; what module(s), what contract>
>
> PRECONDITION (optional)
> - <upstream goal slug + commit, or env requirement>
>
> REFERENCE
> - AGENTS.md (hard invariants)
> - docs-private/<topic>-binding-spec.md §<N>
>
> CHECKLIST
> 1. <smallest-first imperative>
>    [Reviewer note: <margin annotation when checklist amended mid-flight>]
> 2. <...>
>
> ACCEPTANCE
> - <testable criterion>
> - All passing tests stay green
>
> FORBIDDEN
> - <explicit anti-scope: paths or patterns not to touch>
> ```"

When drafter completes, **analyst** (explorer worker via the host `delegate` operation; current Claude wrapper: Explore subagent; other hosts: adapter delegate equivalent): identify parallel-safe chunks AND trivial chunks the controller can handle inline.

> "Given these N drafted chunks, two tagging passes:
>
> (1) **`[parallel-safe:<group-id>]`** — chunks that touch disjoint files/modules and could safely run in parallel worktrees. Chunks in the same group can run together; different groups must be sequential relative to each other if they share dependencies. Conservative bias: when unsure, do not tag.
>
> (2) **`[controller-direct]`** — chunks where dispatching a worker would cost more than just doing the work inline. Two distinct triggers:
>
>     **A. Trivially small work.** Single-file change, < ~30 LoC delta, no cross-module coupling, no new public surface, no test-harness changes. Dispatch overhead exceeds the work. Examples: typo fixes, version bumps, single-constant renames, single-line bug fixes confirmed against an existing failing test.
>
>     **B. Too much context to explain.** The controller has already loaded substantial relevant state (read files, traced data flow, accumulated reasoning across prior chunks in this session) that a fresh worker would have to re-discover via dispatch wrapper. When the cost of EXPLAINING the context (wrapper rendering + executor re-load) exceeds the cost of just doing the work, controller-direct wins on the same overhead-arbitrage logic as case A, but for a different reason. Heuristic: if a clean dispatch wrapper for this chunk would exceed ~5 KB (the verification-first target size per `prompts/dispatch-wrapper.md`) primarily because the chunk depends on session-loaded controller state, prefer inline. Common shapes: mid-debug chunks where the controller has just diagnosed the bug; chunks that resolve a P0 from a milestone review the controller just consumed; chunks that depend on rolling decisions made in the last 10 turns that haven't been promoted to `docs-private/rag/decisions.md` yet.
>
> Conservative bias: when unsure, do NOT tag — let `execute.md` dispatch a worker. The default worker-dispatch path is safer for ambiguous cases (clean context, transcript record, parallel-safe candidates).
>
> Report which file paths each chunk touches (audit trail for parallel safety + controller-direct triviality). Drafter output: <paste>."

### 3. Write to goal-queue

Write to `<repo-root>/docs-private/goal-queue-<topic>-<today>.md` (new naming; legacy `<topic>-goal-queue-<date>.md` from <0.3.0 still accepted on read) with this shape:

```
# <TOPIC> Goal Queue
Date: <today>
Working directory: <repo-root>
Skill-loaded: <LOADED_LINE from protocols/session-preflight.md fingerprint, captured at decomposition time>

Each goal is a self-contained `/goal` dispatch directive. Read together with AGENTS.md, the binding-spec, and the plan of record.

## Progress (as of <today>, main @ <head>)
| Goal | Status | Commit |
|------|--------|--------|
| 1. `<slug>` | TODO | — |
| 2. `<slug>` [parallel-safe:A] | TODO | — |
...

Status: ✅ DONE — `<hash>` · 🟡 IN-FLIGHT — `<executor-id>` · TODO · BLOCKED — `<reason>` · PARTIAL — `<reason; see #N>`

Tags (see SKILL.md for full definitions):
- [parallel-safe:<group>] — chunks in the same group can run via `--parallel N`
- [milestone] — trigger gstack review sweep after this chunk lands
- [controller-direct] — controller handles inline (trivial OR too much session-loaded context to explain)
- [goal-mode] — chunk warrants the iteration loop primitive. Composes with `[acp]` for any worker that has an ACP adapter (codex/grok/cursor-agent/claude-code-cli-acp), OR with `[bash-tail]` ONLY for codex `/goal` (codex emits a Final-response marker giving the watcher a turn-boundary signal; other workers don't qualify today — see `protocols/dispatch-routing.md`)
- [max-iterations:<N>] — cap for [goal-mode] external loops
- [mixed-executor] — iterations cross executor types for model-diversity stuck-loop recovery
- [acp] — force ACP transport (structured events; persistent session); see `protocols/dispatch-routing.md`. Untagged-ACP-capable chunks default to ACP if doctor/capacity status shows the adapter installed
- [bash-tail] — force legacy Bash-`&`-tail-file dispatch (explicit fallback when ACP overhead isn't worth it, or target worker doesn't speak ACP). `[bash-tail] + [goal-mode]` is codex-only; for other workers in `[goal-mode]`, drop `[bash-tail]` and route via `[acp]`

## Universal preconditions
- All passing tests stay green; new tests added per goal
- No silent fallback between providers on unit failure
- <other AGENTS.md-derived invariants>

## <N>. `/goal <SLUG>` (per-chunk skeleton from the drafter above)
```

If a same-day file exists, append new chunks numbered after the last existing entry (do not duplicate). Tags `[parallel-safe:<group>]` come from the analyst (step 2 above); other tags applied by analyst or controller as judgment dictates.

### 4. Review the decomposition itself (parallel reviewers)

**Two reviewers in parallel.** The decomposition is the artifact under review. Both reviewers use the same gstack `/plan-eng-review` framing (when installed) for consistent severity ranking; they produce independent findings since they're different models.

**Controller-host challenger** — primary reviewer through the controller's host adapter:

- Use the controller host's `delegate` equivalent for a gstack `/plan-eng-review` run when that host has gstack registered. In the current Claude wrapper, this is the wrapper-owned delegate path; other hosts use their adapter's delegate equivalent. Reference: `<path to docs-private/goal-queue-<topic>-<today>.md>`, `docs-private/goal-<topic>-*.md`, `AGENTS.md`. (Legacy file naming patterns from <0.3.0 also accepted on read.)
- If gstack is absent for the controller host: delegate a general-purpose reviewer through the host adapter with `prompts/decomposition-review.md` prompt + same plan + decomposition + the goal-statement pasted in.

**Peer challenger** (background, parallel second opinion through a different ready adapter):

- Choose a concern-diverse ready peer adapter (codex, cursor, grok, or another adapter that passes readiness). Use that adapter's declared `delegate` / invocation mapping. The codex commands below are adapter examples, not the default host path.
- Codex adapter example, if gstack registered on codex side (`~/.codex/skills/gstack/` exists):
  ```bash
  timeout --kill-after=10 300 codex exec '/plan-eng-review <path to docs-private/goal-queue-<topic>-<today>.md>. Reference: docs-private/goal-<topic>-*.md, AGENTS.md.' > /tmp/goal-flight-decomp-codex-<topic>.txt 2>&1 &
  ```
- Codex adapter example, if gstack absent on codex side — point codex at the prompt file on disk, don't paste its contents into the exec arg:
  ```bash
  timeout --kill-after=10 300 codex exec \
    "Read <skill-root>/prompts/decomposition-review.md in full and execute it. Plan: <path-to-plan-file>. Drafted decomposition: docs-private/goal-queue-<topic>-<today>.md (or legacy <topic>-goal-queue-<today>.md from <0.3.0). Goal-statement: docs-private/goal-<topic>-*.md (or legacy <topic>-goal-statement-*.md). If your context compacts mid-review, re-read the prompts file — the file is the unparaphrased source of truth." \
    > /tmp/goal-flight-decomp-codex-<topic>.txt 2>&1 &
  ```
  Avoids spamming the controller's tokens with pre-pasted prompt + plan + decomposition; survives codex session compaction; bypasses any CLI argument length limit. Same principle as the context discipline in `SKILL.md`: keep the prompt short and pass pointers.

Capture the PID. The output goes to a temp file.

Wait for both. (Codex adapter example: poll the temp file or `wait $PID`. Controller-host reviewer returns when done.)

If the peer challenger stalls (codex adapter example: the `timeout(1)` wrapper fires after 300 s, or the optional watchdog kills on zero-output >=90 s / no-progress >=180 s): proceed with the controller-host reviewer's findings only. Note in RESUME-NOTES' "In-flight" section.

### 4.5. Verify the decomposition serves the goal

Spawn a quick coherence worker through the host `delegate` operation (current Claude wrapper: Explore subagent; other hosts: adapter delegate equivalent):

> "Read goal-statement: `<paste file content>`. Read drafted decomposition: `<paste numbered chunks>`. Does this decomposition, if executed end-to-end, plausibly achieve the goal-statement's success criteria? List any chunks that don't trace to a goal element. List any goal elements that no chunk addresses. Score: aligned / partial / divergent."

If `partial` or `divergent`: surface to user; do NOT proceed to ask-questions until the gap is resolved (revise either the decomposition or the goal-statement). The user shouldn't have to remind the session what the goal is mid-execute — catch divergence here.

### 5. Consolidate findings; offer revision

Dedupe findings across the two reviewers. Severity-rank P0/P1/P2/P3. Surface to user:

> "Decomposition reviewed by controller-host reviewer + peer reviewer. Found N P0 (must-fix), M P1 (should-fix), K P2 (nice-to-have). [Brief summary of each P0/P1.] Want me to revise the decomposition before continuing, or proceed to ask-questions?"

If user says revise: re-spawn the drafter with the findings as input; loop back to step 3.

### 6. Confirm and summarize

Before declaring decompose-plan done, print to the user:

- **Total chunks**: N
- **Parallel-safe groups**: K (with chunk counts per group)
- **Estimated commit count**: N (one per chunk) + ~N/5 for milestone fix-clusters
- **Files the user should review before launching execute**:
  - `docs-private/goal-queue-<topic>-<today>.md`
  - `docs-private/RESUME-NOTES-<today>.md` (if updated)
  - Any binding-spec or plan-of-record referenced

> "Plan looks right? Want me to launch ask-questions, or does anything need revision first?"

### 7. Auto-chain to ask-questions

Unless the user explicitly said no in step 6, immediately invoke `commands/ask-questions.md` with `--scope decomposition`. (Do this without re-prompting; the auto-chain is part of decompose-plan's contract.)

### 8. Update RESUME-NOTES

Refresh `docs-private/RESUME-NOTES-<today>.md` (bump rev) with: decomposition complete, N chunks, K parallel-safe groups, queue file path, next step `/goal-flight execute`. Delegate the writing to a worker through the host `delegate` operation so the controller doesn't burn its own context.
