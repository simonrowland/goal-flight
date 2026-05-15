# init <topic>

`<topic>` is the slug for this initiative (e.g., `payments-rewrite`, `auth-migration`, `port-to-rust`). Lowercase, hyphens.

## Steps

### 1. Validate environment

Run in parallel:
- `git rev-parse --show-toplevel` → bail if not a git repo.
- `command -v codex` → capture path and `codex --version` if present.
- `command -v bun` → capture version.
- Check gstack install on **both sides** (it works for both Claude Code and codex):
  - Claude-side: `[ -d ~/.claude/skills/gstack ]`
  - Codex-side: `[ -d ~/.codex/skills/gstack ]`
  - Project-level (if present, takes precedence): `[ -d <repo-root>/.agents/skills/gstack ]`
  - Capture which sides are installed; report both.
- Check context-mode install on **both sides**:
  - Claude-side: grep `~/.claude/settings.json` or `~/.claude.json` for an MCP server entry named `context-mode` (or run `claude mcp list 2>&1 | grep context-mode`). Captured: registered or not.
  - Codex-side: grep `~/.codex/config.json` or `~/.codex/mcp.json` for `context-mode`. Captured: registered or not.
  - Plugin form: `[ -d ~/.claude/plugins/context-mode ]` may also be present.
  - Capture which sides registered; report both.

If `codex` missing: tell the user (do NOT auto-install):
> "codex CLI not found. Install with `bun install -g @openai/codex && codex auth login`. The skill works without codex (Claude subagents only) but loses parallel-reviewer capability for milestone reviews."

If `gstack` is missing on **either side**: **recommend install and offer to run it.**

Three cases:

1. **Both sides absent** → recommend full install:
   > "gstack not installed. Strongly recommended — Gary Tan's skill pack works for both Claude Code AND codex, providing `/review`, `/office-hours`, `/plan-eng-review`, `/cso`, `/investigate`, etc. that this skill leans on heavily. The official install registers it for both:
   >
   > ```bash
   > git clone --single-branch --depth 1 https://github.com/garrytan/gstack.git ~/.claude/skills/gstack && cd ~/.claude/skills/gstack && ./setup
   > ```
   >
   > Run it now? (y/n)"

2. **Codex-side present, Claude-side absent** (or vice versa) → recommend re-running setup:
   > "gstack is installed for `<side present>` but not `<side missing>`. To register for both, re-run:
   > ```bash
   > cd ~/.claude/skills/gstack 2>/dev/null || git clone --single-branch --depth 1 https://github.com/garrytan/gstack.git ~/.claude/skills/gstack && cd ~/.claude/skills/gstack && ./setup
   > ```
   > This makes `/review` etc. directly invokable from Claude (faster, no codex round-trip) AND keeps the codex-side parallel-review capability. Run it? (y/n)"

3. **Both sides present** → continue silently; note in summary.

If user says yes to install: run the command via Bash. After completion, re-check both `[ -d ~/.claude/skills/gstack ]` and `[ -d ~/.codex/skills/gstack ]`; report what registered.

If user says no: continue with whatever's installed; note in summary which gstack invocations will use Claude-direct, which will use codex-via-`exec`, and which will fall back to local prompts.

If `context-mode` is missing on either side, recommend installation:

> "context-mode is a strong multiplier for this controller pattern — it offloads large command outputs (diffs, test runs, greps, codex tails) to an FTS5 sandbox and lets the controller and executors query by pattern instead of pulling everything into context. Especially valuable on the codex side during `\goal` loops where shell output fills context fast. Install instructions: https://github.com/simonrowland/context-mode. Want me to walk through the install? (y/n)"

If user accepts: surface the install command from the project README. After install, re-check MCP registrations on both sides; report.

If user declines: continue. Note in summary that large-output handling will use direct Bash/Read tools and may consume more context per chunk.

**Register the project as codex-trusted** (one-time, idempotent — prevents codex MCP approval-gate stalls in non-interactive dispatches):

- Resolve the goal-flight skill root: `SKILL_ROOT=$(dirname "$(readlink -f ~/.claude/skills/goal-flight 2>/dev/null || echo ~/.claude/skills/goal-flight/SKILL.md)")` — or just `~/Repos/goal-flight` if installed locally.
- Run `bash "$SKILL_ROOT/scripts/install-codex-overrides.sh" --check` against the project root. Three outcomes:

  1. **Already trusted** (exit 0): report `codex trust: registered for <repo-root>` in the env summary; continue.
  2. **Codex not installed**: skip silently; no stall risk possible.
  3. **Codex installed but project missing trust** (exit 1): recommend install:
     > "codex `exec` will stall on the MCP approval gate in this project without a one-line user-config trust entry. I can register it via `bash <skill-root>/scripts/install-codex-overrides.sh` — adds a `[projects.\"<abs>\"].trust_level = \"trusted\"` block to `~/.codex/config.toml` (worktrees inherit via path prefix). Run now? (y/n)"
     - If yes: run the install. Re-check; report.
     - If no: continue, BUT note in env summary that every codex dispatch in this project must include `--ignore-user-config` (see `reference/pattern.md` §Codex reliability fallback shape), which loses MCP tool access during the dispatch.

Today's date: use the conversation's `currentDate` value, format `YYYY-MM-DD`.

### 2. Audit the repo via subagent (controller does NOT read docs directly)

**Spawn an Explore subagent** with the prompt at `prompts/repo-audit.md`. Substitute `{{TOPIC}}` and the repo root. The subagent reads README, AGENTS.md (if exists), `docs/`, recent git log, top-level test directories, package manifests. It returns a precis (project name, 3-5 hard invariants, file map, conversation/commit style, existing AGENTS.md status, tooling).

Wait for the subagent's report before scaffolding. Its report is the source of truth for the placeholders below.

### 2.5. Pin the high-level goal (LOAD-BEARING)

The user shouldn't need to remind future sessions what the point of `<topic>` is. Memorialize the goal here.

**Ask the user:**
> "What's the high-level goal of this `<topic>` work? In a paragraph: what does the success state look like, and what changes for the user/system when it's done?"

**If the user's reply is concrete** (>2 sentences, names a measurable outcome, identifies a user/system that benefits): proceed directly to write the goal-statement file.

**If the goal is fuzzy or abstract** ("just clean up X", "modernize Y", "fix the foo issue"): flag this and recommend interrogation. Three paths in priority order:

1. **gstack `/office-hours` Claude-side direct** (preferred — fastest, no codex round-trip):
   > "The goal is fuzzy — recommend running gstack `/office-hours` to interrogate. It'll ask the YC forcing questions: who's the user, what's the demand, narrowest wedge, etc. I can invoke it directly via the Skill tool. Run now? (y/n)"
   
   If yes: invoke `Skill(skill: "office-hours", args: "<topic-context>")`. Capture output and distill into the goal-statement file.

2. **gstack `/office-hours` via codex** (when only codex-side install exists):
   > "Same as above, but `/office-hours` isn't registered on the Claude side here — I'll invoke it via `timeout 300 codex exec '/office-hours <topic-context>'`. Run now? (y/n)"
   
   If yes: dispatch and capture stdout. Distill into the goal-statement file.

3. **Local YC-style subagent fallback** (when gstack is absent on both sides):
   Spawn a Claude subagent (Agent tool) with this prompt:
   > "You're running a YC-style office-hours interrogation. The user is starting work on `<topic>`. From the conversation context, their starting fuzzy goal is: `<paraphrase>`. Ask them, in order: (1) Who specifically benefits when this is done? (2) What demand or pain triggered this now? (3) What's the narrowest wedge that proves it works? (4) What's the success criterion you'd test against? (5) What's explicitly NOT in scope? Drive to a one-paragraph goal statement + measurable success criteria. Output as fields ready to populate `templates/goal-statement.md.tpl`."

**Write the goal-statement** to `<repo-root>/docs-private/<topic>-goal-statement-<today>.md` using `templates/goal-statement.md.tpl`. This is the load-bearing anchor; subsequent commands cite it.

If the user defers ("we can figure that out later"): write a stub goal-statement with the user's fuzzy version + a `STATUS: DRAFT — needs sharpening before execute` line. decompose-plan will refuse to proceed without sharpening.

### 3. Scaffold

For each template, read it, substitute placeholders (`{{TOPIC}}`, `{{DATE}}`, `{{REPO_ROOT}}`, `{{PROJECT_NAME}}`, plus audit-derived values like `{{INVARIANT_*}}`), then write:

| Template | Output | If exists |
|----------|--------|-----------|
| `templates/AGENTS.md.tpl` | `<repo-root>/AGENTS.md` | **Merge mode**: read existing; show user a diff of proposed additions/edits; ask which to apply. Never destructive. |
| (direct create) | `<repo-root>/docs-private/.gitkeep` | skip |
| `templates/RESUME-NOTES.tpl` | `<repo-root>/docs-private/RESUME-NOTES-<today>.md` | bump `(rev N)` |
| `templates/worker-context.md.tpl` | `<repo-root>/docs-private/worker-context.md` | create only if AGENTS.md is huge (>1000 lines) or multiple distinct worker profiles exist; otherwise skip — executors read AGENTS.md directly |

Forensics live in the harness-captured session JSONL plus the per-subagent
JSONL plus the codex tail files plus RESUME-NOTES + git log. No separate
`controller.log` is created; the structured-timeline view is recoverable
via `jq` over the session JSONL when needed.

The initial RESUME-NOTES should explicitly say "init complete; ready for `/goal-flight decompose-plan`" — most fields will be placeholders until decompose-plan runs.

### 3.5. Build the RAG corpus (context-engineering)

Now that AGENTS.md, the goal-statement, and the audit precis exist, scaffold the dispatch-time context library at `<repo-root>/docs-private/rag/` so executors stop re-reading the same domain context every dispatch and the controller stops re-pasting the same wrapper layers.

Refer to `templates/rag-corpus-schema.md.tpl` for the directory shape and per-slice word budgets.

#### Pre-flight gates (skip the corpus if any apply)

The corpus is overhead. Skip it on projects too small or too sparse to amortize the cost. Three gates — if ANY of them trips, skip step 3.5 and continue. Surface the skip reason in the init summary so the user can override.

1. **Greenfield no-source gate**: audit precis from step 2 identified <3 hard invariants AND no binding-spec / refactor-plan file exists. Slice-builders would distill empty placeholders. Defer until decompose-plan lands real chunks and decisions.
2. **Small-project gate**: anticipated chunk count is <12 AND planned LoC delta is <5000 (estimate from goal-statement). Note the AND — only trip when both fall below. Cost (~$1-3 of subagent dispatch + 3 passes of wall-clock) IS justified for any project with sustained dispatch volume; tokens are a free good relative to quality. Bias toward building the corpus.
3. **Sparse-doc gate**: no AGENTS.md beyond the bare template AND no `docs/` tree AND no `docs-private/` files. The audit precis would be all the source the corpus has.

If skipped: print "Corpus build skipped: <reason>. Run `/goal-flight build-corpus` later when source material exists." Leave the rest of init intact.

#### Source-list derivation per slice

Slice-builders need their source paths pinned in the dispatch. Mapping:

| Slice | Source materials (controller passes these paths to the builder) |
|-------|----------------------------------------------------------------|
| `invariants.md` | AGENTS.md hard-invariants section + any `tests/test_*_guards*.py` files identified by audit |
| `file-map.md` | The audit precis's file-map section + `ls -la <repo-root>` output + project manifest (package.json / pyproject.toml / etc.) |
| `binding-spec/<intent>.md` (one per intent) | The intent's section of the binding-spec / authority-matrix file (slice names derive from the headings in that file; controller enumerates them during pass-1 slice-builder dispatch, not later at dispatch composition time). Skip this whole subdirectory if no binding-spec exists. |
| `patterns/<pattern>.md` (one per pattern) | The canonical implementation file (controller picks from audit's "notable patterns" or from the goal-statement's named patterns) + sibling implementations the pattern needs to be consistent with. Skip this whole subdirectory if audit identified no recurring patterns. |
| `decisions.md` | Recent goal-queue entries with STATUS lines, recent commit messages, any inline `[Reviewer note: ...]` annotations |
| `verification.md` | The audit precis's tooling section + actual content of any `tests/conftest.py` or `tests/test_artifact_*.py` |

If the controller can't enumerate sources for a slice (e.g., no binding-spec exists), skip that slice entirely. Surface skipped slices in the init summary.

#### Three-pass pipeline

**Pass 1 — parallel slice builders (Claude subagents).**

For each slice in the source-list table above where sources exist:
- Dispatch one Claude subagent (Agent tool, general-purpose, `model: "opus"` for code-adjacent slices like `patterns/*` and `verification.md`, **also `decisions.md`** because its content is read by every Reviewer + Planner dispatch downstream so quality matters; default model only for the simplest prose slices).
- Use `prompts/rag-slice-builder.md` as the dispatch template.
- Pass the source-material absolute paths from the table above.
- Each subagent writes its slice to `docs-private/rag/<filename>` and reports back.

Spawn all in parallel; cap at ~10 concurrent subagents to avoid noise. Smaller projects: 4-6 slices; larger ones: 10-15.

**Pass 2 — per-slice review (Claude subagents, parallel).**

For each slice that pass 1 produced:
- Spawn a reviewer subagent with `prompts/rag-slice-review.md`.
- Reviewer reads the slice + its source materials AND verifies any grep patterns the slice claims work against the actual code.
- Reports P0/P1/P2/P3 findings, including a `Dispatch-readiness` category (does the slice match its schema's per-slice format?).

If any P0/P1: re-dispatch the corresponding slice-builder with findings as input, OR patch directly if small (controller decides).

**Pass 3 — cross-slice consolidation (one Claude Opus 1M-context pass; codex fallback).**

After all per-slice reviews are clean:
- Default: dispatch a single Claude Opus subagent (Agent tool, `model: "opus"`) with `prompts/rag-cross-slice-consolidation.md`. Pass absolute paths of every corpus file. Opus's 1M context holds the aggregate corpus (~12 KB max for a typical project) cleanly; reliability is higher than codex.
- Fallback (codex): only if you specifically want a model-diversity second opinion, or Claude Opus is unavailable. Codex command pattern — point codex at the prompts file, don't paste it into the exec arg:

```bash
timeout --kill-after=10 300 codex exec \
  "cd <repo-root> && (Read ~/.claude/skills/goal-flight/prompts/rag-cross-slice-consolidation.md in full and execute it. The corpus files to consolidate are at <repo-root>/docs-private/rag/ — enumerate them yourself with \`ls docs-private/rag/**/*.md\`. If your context compacts mid-pass, re-read the prompts file — it is the unparaphrased source of truth.)" \
  > /tmp/goal-flight-rag-consolidation-<topic>-<iso>.txt 2>&1 &
```

Tail or wait. The pointer-based shape avoids spamming the controller's tokens with pre-pasted prompt content + corpus file paths, and survives codex session compaction (codex can re-Read the file). Assumes the project has been registered as codex-trusted in step 1; otherwise add `--ignore-user-config`. See `reference/pattern.md` §Codex reliability.
- Apply any P0/P1 fixes; surface P2/P3 as TODO comments in the affected slice.

**Pass 4 — final assessment (one Claude Opus subagent).**

After Pass 3 fixes are applied, dispatch one final-assessment subagent with `prompts/rag-final-assessment.md`. It:
- Aggregates per-slice reviewer scores into a quality dashboard (the reviewers emitted these per the score rubric in `prompts/rag-slice-review.md`).
- Walks through a hypothetical cold-executor dispatch to identify residual gaps.
- Recommends next-wave priorities.
- Issues a CORPUS IS DISPATCH-READY / NEEDS-MORE-ITERATION verdict.

The quality dashboard goes into RESUME-NOTES as a small table; the next-wave priorities feed the next iteration of step 3.5 (or a user-triggered `/goal-flight build-corpus --next-wave`).

**Outcome**: `docs-private/rag/` is now populated, reviewed, and scored. Future dispatches paste from these slices instead of reconstructing context from scratch. The controller's context budget is preserved for integration, requirements adjudication, and graph-orientation calls.

### 4. Ensure gitignore + AGENTS.md tracked

Read `<repo-root>/.gitignore`.

- If `docs-private/` is not present, append it. (docs-private holds dated per-session state — RESUME-NOTES, goal-queue, goal-statement, rag/ — which is correctly per-machine and should not be tracked.)
- **AGENTS.md should be tracked, not gitignored.** The worktree story is the key reason: tracked files propagate to every worktree's checkout automatically; gitignored files do not, so a controller or executor spawned inside a worktree silently has no AGENTS.md to read, defeating the auto-load directive in step 4.5. The CLAUDE.md pointer in 4.5 is also worktree-correct only when its target (AGENTS.md) propagates.
  - If AGENTS.md is currently listed in `.gitignore`: ask the user *"AGENTS.md is gitignored, which means it won't appear in worktree checkouts and the auto-read directive will silently fail there. Remove from .gitignore so it propagates? (y/n)"*. Default yes.
  - If AGENTS.md is not gitignored but is also not yet committed: note in summary; suggest the user `git add AGENTS.md` and commit on their next commit.
  - If the user prefers gitignored AGENTS.md (some teams do — competitive content, privacy, license): note in summary that worktrees won't auto-inherit AGENTS.md and the user is responsible for symlinking (`ln -s <main>/AGENTS.md <worktree>/AGENTS.md`) or copying per worktree, OR running every controller from the main worktree only.

### 4.5. Ensure AGENTS.md will be auto-read by future sessions

Claude Code does not auto-load `AGENTS.md` the way it auto-loads `CLAUDE.md` (Codex loads AGENTS.md natively; Claude Code currently does not). To make AGENTS.md reliably the first thing every future controller and executor reads, ensure a CLAUDE.md exists with a directive pointing at it.

**First, check what's already in place:**

- Global directive: `grep -l "AGENTS.md" ~/.claude/CLAUDE.md 2>/dev/null` — if present, the user has a machine-wide rule.
- Project directive: `grep -l "AGENTS.md" <repo-root>/CLAUDE.md 2>/dev/null` — if present, the project has a per-repo rule.

**Then dispatch on what's there:**

| Global has it? | Project has it? | Action |
|----------------|-----------------|--------|
| Yes | Yes | Done. Note both in summary. |
| Yes | No | Ask: *"Your global CLAUDE.md already auto-reads AGENTS.md, so this works for you on this machine. Want to also add a project-level pointer in `<repo-root>/CLAUDE.md`? It propagates to teammates via git so they get the same behavior. (y/n)"* |
| No | Yes | Ask: *"Project CLAUDE.md auto-reads AGENTS.md, so any session in this repo works. Want to also add it to your global `~/.claude/CLAUDE.md` so other projects of yours benefit? (y/n)"* |
| No | No | **Ask scope explicitly:** *"To make AGENTS.md auto-load reliably, I can add a directive to: (1) global `~/.claude/CLAUDE.md` — once, all your projects benefit, no team propagation; (2) project `<repo-root>/CLAUDE.md` — only this repo, propagates to teammates via git; (3) both — belt-and-suspenders, useful if you sometimes work without your global config. Which?"* |

**Snippet for project-level CLAUDE.md** (if user picks project or both):
```
## Read AGENTS.md first
This project pins agent operating instructions in `AGENTS.md` at the repo root.
Read it before doing any work — it carries the project invariants, file map,
and conversation style that shape everything downstream.
```
If a project CLAUDE.md already exists, append; show the diff and ask before applying. If not, create it.

**Snippet for global ~/.claude/CLAUDE.md** (if user picks global or both):
```
# session start
At session start in any project, if `AGENTS.md` exists at the repo root,
read it before doing other work — it carries the project invariants, file map,
and conversation style that shape everything downstream. Claude Code does not
auto-load AGENTS.md; this makes the behavior symmetric with Codex. If working
inside a git worktree where AGENTS.md is gitignored and absent, also check
the parent project root.
```
Append (don't overwrite); show the diff and ask before applying. Note this affects every project on this machine.

If user declines all options: note in summary that AGENTS.md will need to be Read manually at the start of each future session, OR be invoked via `/goal-flight` (which always Reads it). Future controllers / executors that don't go through the skill won't pick it up.

### 5. Self-review the init output

**Spawn a second subagent** (Explore) to audit what init produced. Prompt it:

> "init just scaffolded `<repo-root>/AGENTS.md` and `<repo-root>/docs-private/worker-context.md` based on a repo audit. Read both files plus the audit report below. Identify: (a) invariants the codebase enforces (in tests or guard rails) that aren't captured in either file; (b) file-map gaps; (c) anything in the conversation-style section that contradicts the existing commit log. Report under 300 words; format as TODO list the controller can paste into AGENTS.md."

If the self-review finds gaps, surface them to the user as a TODO comment block at the end of AGENTS.md (HTML comment so it doesn't render in viewers). Do not edit AGENTS.md to fix the gaps automatically — let the user decide.

### 6. Print summary

- Files created / modified (one path per line).
- Codex install status (path + version, or "missing — install command above").
- gstack install status (installed / installed-during-init / declined — using fallback prompts).
- Audit subagent's high-level findings (project type, invariant count, AGENTS.md status).
- Goal statement status (concrete / interrogated-via-office-hours / DRAFT — sharpen before execute).
- AGENTS.md auto-read directive: where it landed (project CLAUDE.md / global CLAUDE.md / already-present / declined-and-relying-on-skill-invocation).
- Self-review TODOs (if any), with the AGENTS.md location they'll appear at.
- Suggested next step: `/goal-flight decompose-plan <plan-file>` (or "decompose the plan you already discussed in this session"). If goal-statement is DRAFT: "decompose-plan will refuse until the goal is sharpened."
