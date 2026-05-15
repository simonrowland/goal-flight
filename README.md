# goal-flight

A [Claude Code](https://claude.ai/code) skill for long-running unattended controller-pattern work — refactors, multi-turn implementation from architecture docs, ports, recursive end-to-end testing, finite Karpathy / Ralph loops, scientific convergence against ground truth or first principles.

## What it does

Turn a fresh Claude Code session into a **controller** that:

- Decomposes a plan into numbered codex-cli `/goal` chunks with structured SCOPE / CHECKLIST / ACCEPTANCE / FORBIDDEN
- Dispatches each chunk to an **executor subagent** with a 5-layer wrapper (situational frame, template-provider pointer, file-anchors, environment caveats, goal-specific self-review specialization)
- Embeds adversarial self-review inside every dispatch (executor self-fixes P0/P1/P2 before reporting done)
- Runs **parallel codex + claude review sweeps** at milestone cadence (the "gstack" pattern)
- Builds a **RAG corpus** of curated dispatch-time context at init time so the controller stops re-pasting AGENTS.md hard invariants per dispatch
- Writes dated **handoff notes** (RESUME-NOTES-YYYY-MM-DD.md) before context fills so the next controller wakes up cleanly

Designed for ~12-hour unattended runs where you check in periodically or respond to decision notifications, rather than babysit.

The philosophy is to apply more tokens to improve software quality, and apply more tokens to reduce unnecessary user prompts (including Netflix-style "Are you still there?" prompts).

## Install

```bash
git clone https://github.com/simonrowland/goal-flight.git ~/.claude/skills/goal-flight
```

Then in a Claude Code session: `/goal-flight init <topic>` to start, or `/goal-flight` (no args) to print the high-level pattern reference.

## Quickstart

```bash
# 1. In your project repo, in a Claude Code session:
/goal-flight init <topic>            # audits the repo, scaffolds AGENTS.md +
                                     # docs-private/ + (optional) RAG corpus,
                                     # registers the project as codex-trusted
                                     # if codex is installed

/goal-flight decompose-plan          # breaks a plan into numbered \goal chunks
                                     # with SCOPE / CHECKLIST / ACCEPTANCE /
                                     # FORBIDDEN; parallel reviewer pass

/goal-flight execute                 # runs the per-chunk dispatch loop with
                                     # embedded self-review; milestone codex+
                                     # claude reviews every K commits

# 2. Resume after a break / new session:
/goal-flight resume                  # rebuilds RESUME-NOTES from current git state

# 3. (Maintenance, if codex was added to a project post-init)
/goal-flight register-codex          # registers the cwd project as codex-trusted

# 4. (Debugging — dry-run, no dispatch billed)
/goal-flight validate-dispatch <slug>   # render the 5-layer wrapper for review
/goal-flight validate-queue             # schema-check the goal-queue
```

`/goal-flight` with no arg prints the high-level pattern reference (`reference/pattern.md`). The full sub-command list is below.

## Sub-commands

| Command | What it does |
|---------|--------------|
| `/goal-flight` | Print the high-level pattern reference (`reference/pattern.md`) |
| `/goal-flight init <topic>` | Check tooling, audit repo via subagent, scaffold AGENTS / docs-private / RAG corpus |
| `/goal-flight decompose-plan [<plan-file>]` | Break a plan into `\goal` chunks; parallel reviewer pass for the decomposition |
| `/goal-flight ask-questions [<scope>]` | Spawn anticipatory subagents; surface clarifying questions for the user |
| `/goal-flight execute [--parallel <N>]` | Run the per-chunk loop; sequential default, parallel-safe opt-in |
| `/goal-flight build-corpus [<flags>]` | Extend / rebuild the `docs-private/rag/` corpus after init |
| `/goal-flight resume` | Rebuild RESUME-NOTES from current git state |
| `/goal-flight goal <SLUG>` | Append one goal to the queue using the skeleton |
| `/goal-flight register-codex [<path>]` | Register a project (or worktree path) as codex-trusted; bypass MCP approval-gate stalls |
| `/goal-flight validate-dispatch [<slug>]` | Render the 5-layer dispatch wrapper for a goal **without dispatching** — dry-run for wrapper-composition bugs |
| `/goal-flight validate-queue [<path>]` | Schema-check the goal-queue: chunk structure, numbering, parallel-safe tags, slug uniqueness |

## Why this pattern works (the gist)

The naive way to run a long unattended session is "Claude does everything; user comes back to check." This fails because the controlling agent's context window fills up with file reads, tool output, and re-derivation, and quality drops as it accumulates noise. The naive solution is summarization, which is lossy.

The controller pattern instead **delegates concrete work to bounded subagents**, each operating in their own context window. The controller stays small — it dispatches, verifies the result, commits, and dispatches again. Each subagent dispatch is a 5-layer briefing prepared from a curated RAG corpus built at init time, so executor subagents don't waste context re-reading the project's invariants/architecture/binding-spec from scratch.

Adversarial self-review is embedded inside every executor prompt — the executor treats its own work as if a different agent submitted it, runs through a P0/P1/P2/P3 checklist, self-fixes before reporting done. This catches roughly the same surface a separate reviewer subagent would, at a fraction of the controller-context cost. Full multi-agent review (codex + claude, concern-split) is reserved for milestone checkpoints.

The result: the user can step away in bunny slippers. The controller makes forward motion through routine chunks, holding state in files (RESUME-NOTES, goal queue, RAG corpus) instead of conversation history. When a real blocker arrives — a chemistry assumption the controller can't adjudicate, a destructive operation, a scope-vs-scope tradeoff requiring user values — the controller surfaces a well-prepared question with subagent-vetted options. "Step 1 done. Continue?" is not a blocker; the controller just continues.

## Guiding philosophy

- **Use more tokens to improve quality, especially when parallelisable.** Subagent tokens and codex tokens are free goods relative to engineering quality.
- **North star: code quality + first-principles scientific integrity** (configurable — see "Adapting" below). Interrupt the user only when a decision genuinely affects whatever your north star is.
- **Inline the landscape.** With 1M-context models, every Read the executor would have to do can be replaced by content pasted into the dispatch prompt (or sourced from the RAG corpus).
- **Per-intent self-review is cheaper than per-intent reviewer subagents.** Embed adversarial self-review inside the executor prompt; reserve full multi-agent review for milestones.
- **Cherry-pick over merge --ff-only** when integrating parallel-worktree subagent commits onto main.
- **Don't poll background subagents.** Wait for the harness's task-notification.

## Adapting this to your project

This skill ships tuned for **high-accuracy scientific programming** — projects where mass-balance closure, atom-conservation, stoichiometric integrity, and first-principles correctness are load-bearing. The patterns generalize but the defaults will want adjustment for other contexts. Fork the repo and have your own Claude Code session edit the relevant files (the skill is documentation-only, so adaptation is text editing not code work).

### The parameter space

| Knob | Where | Current default (scientific) | Tune for... |
|------|-------|------------------------------|-------------|
| **North star** | `SKILL.md` "Asking discipline" bullet | code quality + first-principles scientific integrity | security + threat-model coverage (security-sensitive backends); UX correctness + accessibility (frontend); reproducibility + dataset versioning (data/ML); API contract stability (libraries/SDKs) |
| **Self-review categories** | `prompts/executor-self-review.md` | INVARIANT GAP / SCOPE LEAK / MUTATION PURITY / BEHAVIOR DRIFT / DEAD CODE / CONTRACT LEAK / INTEGRITY | Add CSRF/XSS/SQLi for security work; add A11Y/RESPONSIVENESS for frontend; add MIGRATION-SAFETY for data work; add API-VERSION-COMPATIBILITY for libraries |
| **Review cadence** | `commands/execute.md` step 4 (`K=5` default) | Every 5 commits or at `[milestone]` chunks | K=3 for high-stakes (medical, finance); K=10 for prototypes; K=1 for security-critical paths |
| **Model + reasoning defaults** | `SKILL.md` "default code-writing dispatches..." bullet | Opus + xhigh reasoning | Sonnet + default for prototype speed; Opus + xhigh for anything correctness-critical |
| **RAG slice mix** | `templates/rag-corpus-schema.md.tpl` | invariants / file-map / binding-spec/* / patterns/* / decisions / verification / glossary | Add `threat-model.md` for security; `api-stability.md` for libraries; `reproducibility.md` for data; `style-guide.md` if the codebase has strong style conventions |
| **Word budgets per slice** | `templates/rag-corpus-schema.md.tpl` + per-slice templates | invariants ~300, file-map ~800, binding-spec ~400 each, patterns ~500, decisions ~1500, verification ~700, glossary ~700 | Tighter for prototype; looser for projects with many invariants or rich decision history |
| **Verification rubric** | `templates/rag-slice-verification.md.tpl` | pytest + grep-based invariants + mass-balance check | Add latency benchmarks for perf-critical; add OWASP scans for security; add visual regression for frontend; add data-quality checks for ETL |
| **Pre-flight skip gates** | `commands/init.md` step 3.5 | Skip corpus if <3 invariants AND no spec, OR <12 chunks AND <5000 LoC, OR sparse-doc | Tighter (always build) for high-investment refactors; looser (skip more often) for exploratory work |
| **Codex usage** | Multiple files | Parallel second-opinion at milestone reviews + cross-slice consolidation fallback | Drop codex entirely if you don't have OpenAI Pro; double-up codex reviewers for higher-stakes work |
| **Asking-discipline bar** | `SKILL.md` "Asking discipline" bullet | High bar — only blockers that affect the north star | Lower bar (more asks) for early-collaboration phases; higher bar (more autonomy) for established projects |
| **Dispatch-wrapper layers** | `prompts/dispatch-wrapper.md` | All 5 layers for non-trivial chunks; 1+5 for trivial | Drop layer 4 (env caveats) for projects with stable env; expand layer 2 (template-provider) for codebases with strong canonical patterns |
| **Worktree convention** | `commands/execute.md` parallel mode | Two-worktree (main + controller workspace); subagents in `.claude/worktrees/<adjective-noun>/` | Single worktree for solo small projects; per-team worktrees for shared repos |

### Example tunings

- **Frontend product team**: north star = UX correctness; self-review adds A11Y + RESPONSIVENESS; verification adds visual regression + Lighthouse; RAG slice mix includes `design-system.md` and `component-patterns.md`; review cadence K=3.
- **Security-sensitive backend**: north star = threat-model coverage; self-review adds CSRF / XSS / SQLi / AUTHN-AUTHZ / SECRETS; verification adds SAST + dependency scan; review cadence K=1 for auth paths, K=5 elsewhere; gstack `/cso` runs on every milestone not just security-relevant ones.
- **Data / ML pipeline**: north star = reproducibility + lineage; self-review adds MIGRATION-SAFETY + DATA-CONTRACT; verification adds dataset-version pinning + row-count invariants; RAG slice mix includes `data-contracts.md` and `pipeline-dag.md`.
- **Library / SDK**: north star = API contract stability; self-review adds API-VERSION-COMPAT + DEPRECATION-PATH; verification adds semver-checker + docs-coverage; RAG slice mix includes `public-api.md` and `breaking-changes.md`; review cadence K=3.
- **Quick prototype / hackathon**: north star = "ship something demonstrable"; skip RAG corpus entirely (pre-flight gate trips); use Sonnet default; review cadence K=20 or off; asking-discipline lower.

### Adapting via agent-edit

The intended workflow: clone this repo, open it in Claude Code, point at it and say "Adapt this skill for a [domain] project; my north star is [X]; my self-review categories should add [Y]; here's our verification command and our invariants." The skill is small enough (~34 markdown files, ~50 KB total) that a single Opus subagent can read the whole thing, propose a diff, and apply it in one pass. Then commit your fork.

## When NOT to use this

- **One-off scripts or quick fixes.** The overhead of init + RAG corpus + per-chunk wrappers is unjustified for <8 chunks or <2000 LoC delta. Pre-flight gates auto-skip the corpus build for projects this small; you can also skip `/goal-flight` entirely.
- **Pair-programming sessions.** The pattern is designed for unattended runs. If you're going to be present and steering every turn, the wrapper overhead just slows you down.
- **Sensitive operations the controller shouldn't autonomously trigger.** Production deploys, prod data writes, credential rotations, anything where human-in-the-loop is the point. `goal-flight` is for code-writing autonomy, not ops automation.
- **Projects without test signal.** The self-review and milestone-review patterns depend on tests / grep invariants / verification commands existing. If you can't test the work, the pattern's quality guarantees collapse.

## Companion tools

- **[gstack](https://github.com/garrytan/gstack)** — Gary Tan's skill pack works for both Claude Code and codex; provides `/review`, `/office-hours`, `/plan-eng-review`, `/cso`, `/investigate`. `goal-flight` leans on it for milestone reviews.
- **context-mode** — MCP plugin that offloads large command outputs (diffs, test runs, codex tails) to an FTS5 sandbox. Strongly recommended for the controller pattern.

## Directory layout

```
goal-flight/
├── SKILL.md                    # Top-level skill definition (slash command entry)
├── commands/                   # Sub-command implementations
│   ├── init.md
│   ├── decompose-plan.md
│   ├── ask-questions.md
│   ├── execute.md
│   └── build-corpus.md
├── prompts/                    # Subagent prompt templates
│   ├── dispatch-wrapper.md     # The 5-layer briefing pattern
│   ├── executor-self-review.md
│   ├── gstack-claude-review.md
│   ├── gstack-codex-challenge.md
│   ├── rag-slice-builder.md    # RAG corpus pipeline
│   ├── rag-slice-review.md
│   ├── rag-cross-slice-consolidation.md
│   ├── rag-final-assessment.md
│   ├── dual-plan-adversarial.md
│   ├── decomposition-review.md
│   ├── ask-anticipatory.md
│   └── repo-audit.md
├── templates/                  # Scaffolding templates rendered at init
│   ├── AGENTS.md.tpl
│   ├── RESUME-NOTES.tpl
│   ├── goal-statement.md.tpl
│   ├── goal-queue.tpl
│   ├── worker-context.md.tpl
│   ├── rag-corpus-schema.md.tpl
│   ├── rag-slice-invariants.md.tpl
│   ├── rag-slice-file-map.md.tpl
│   ├── rag-slice-binding-spec.md.tpl
│   ├── rag-slice-pattern.md.tpl
│   ├── rag-slice-decisions.md.tpl
│   └── rag-slice-verification.md.tpl
└── reference/
    └── pattern.md              # High-level controller pattern reference
```

## Provenance

Distilled from a sustained refactor session on a regolith pyrolysis simulator where the controller-pattern ran for ~12 hours across multiple chunked goals. The pattern proven there is what this skill formalizes — including the post-session audit that found the controller dispatched ~6-11 KB wrappers around 600-1200 char goal text, leading to the dispatch-wrapper 5-layer codification.

## License

MIT — see [LICENSE](LICENSE).
