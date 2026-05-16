# init <topic>

`<topic>` is the slug for this initiative (e.g., `payments-rewrite`, `auth-migration`, `port-to-rust`). Lowercase, hyphens.

## Steps

### 1. Validate environment

**Agent roles in goal-flight:**
- **Controller** — the agent running `/goal-flight` (currently Claude Code; Hermes is the future candidate). Owns dispatch + verify + commit + handoff.
- **Executor / Reviewer** — the agent receiving `/goal` dispatches (Agent-tool Claude subagents by default, with `Bash codex exec` for the parallel codex reviewer at milestones). **Codex is a dispatch target, not a controller** — never expected to invoke `/goal-flight <sub>` itself.

Run in parallel:
- `git rev-parse --show-toplevel` → bail if not a git repo.
- `command -v codex` → capture path. If present, also capture `codex --version` (e.g. `codex-cli 0.130.0`) and record it in the init summary — codex CLI behaviour shifts between versions (flag names, MCP semantics, plugin defaults), and the dispatch-shape assumptions in `SKILL.md` are pinned to a version. RESUME-NOTES forensics later are easier with the version recorded.
- `command -v bun` → capture version.
- `command -v grok` → capture path + `grok --version` if present. Grok is a peer dispatch target for `/goal`-mode chunks via the Opus/Grok iteration loop fallback (see `SKILL.md` §Fallback: Grok iteration loop). If absent, the skill still works — Opus iteration (via Agent tool) is the no-extra-install fallback. If present, surface availability in the summary so the controller knows the model-diversity option is available for stuck-iteration recovery.
- Check gstack install on the **Claude side** (the controller side) plus codex side for parallel-reviewer milestone use:
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
> "codex CLI not found. Install with `npm install -g @openai/codex && codex login`. The skill works without codex (Claude subagents only) but loses parallel-reviewer capability for milestone reviews."

If `codex` present, check version + `/goal` feature flag. `/goal` mode (codex CLI's multi-hour autonomous loop) requires codex ≥ 0.128.0 + `features.goals = true` in `~/.codex/config.toml`. Older codex still works for short-prompt review dispatches but loses the loop primitive.

```bash
INSTALLED=$(codex --version 2>&1 | awk '{print $NF}')
GOAL_MIN="0.128.0"
older() { [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -1)" = "$1" ]; }

if older "$INSTALLED" "$GOAL_MIN"; then
  echo "codex $INSTALLED installed; /goal mode requires $GOAL_MIN+. Recommend: codex update"
elif command -v npm >/dev/null 2>&1; then
  LATEST=$(npm view @openai/codex version 2>/dev/null)
  [ -n "$LATEST" ] && [ "$INSTALLED" != "$LATEST" ] && \
    echo "codex $INSTALLED installed; $LATEST available — run 'codex update' for the latest."
fi

# Features flag
codex features list 2>&1 | grep -q '^[[:space:]]*goals.*enabled' || \
  echo "Recommend: codex features enable goals (enables /goal mode)"
```

Surface recommendations but don't auto-update — environment mutation is the user's call. If features.goals is off, ask y/n before enabling.

If `gstack` is missing on either side, offer install:

```bash
git clone --single-branch --depth 1 https://github.com/garrytan/gstack.git ~/.claude/skills/gstack \
  && cd ~/.claude/skills/gstack && ./setup
```

The setup script registers gstack on both Claude and codex sides. If only one side is present, re-running setup adds the other. Ask y/n before running; if declined, fall back to `prompts/gstack-*.md` for review dispatches and note in summary which gstack invocations use Claude-direct vs codex-via-exec vs local-prompts.

If `context-mode` MCP is missing on either side, offer install with a pointer to https://github.com/simonrowland/context-mode. Context-mode offloads large command outputs (diffs, test runs, greps, codex tails) to an FTS5 sandbox — a real multiplier on `/goal` loops where shell output fills context fast. Decline = note in summary that large-output handling falls back to direct Bash/Read.

**Register the project as codex-trusted** (one-time, idempotent — prevents codex MCP approval-gate stalls in non-interactive dispatches):

- Resolve the goal-flight skill root: `SKILL_ROOT=$(dirname "$(readlink -f ~/.claude/skills/goal-flight 2>/dev/null || echo ~/.claude/skills/goal-flight/SKILL.md)")` — or just `~/Repos/goal-flight` if installed locally.
- Run `bash "$SKILL_ROOT/scripts/install-codex-overrides.sh" --check` against the project root. Three outcomes:

  1. **Already trusted** (exit 0): report `codex trust: registered for <repo-root>` in the env summary; continue.
  2. **Codex not installed**: skip silently; no stall risk possible.
  3. **Codex installed but project missing trust** (exit 1): recommend install:
     > "codex `exec` will stall on the MCP approval gate in this project without a one-line user-config trust entry. I can register it via `bash <skill-root>/scripts/install-codex-overrides.sh` — adds a `[projects.\"<abs>\"].trust_level = \"trusted\"` block to `~/.codex/config.toml` (worktrees inherit via path prefix). Run now? (y/n)"
     - If yes: run the install. Re-check; report.
     - If no: continue, BUT note in env summary that every codex dispatch in this project must include `--ignore-user-config` (see `SKILL.md` §Codex reliability fallback shape), which loses MCP tool access during the dispatch.

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

**Write the goal-statement** to `<repo-root>/docs-private/<topic>-goal-statement-<today>.md` with this shape (compose; no template file):

```
# <TOPIC> — Goal Statement
Date: <today>
Owner: <user>
Source: <user statement | office-hours | refactor-plan §N>
Status: <CONCRETE | DRAFT — <reason>>

## What changes when this is done
<one paragraph: concrete success state; names a user/system that benefits and what they observe differently>

## Why now
<one paragraph: what triggered this, what cost is being paid by not doing it, deadline window>

## Success criteria
<bulleted; each criterion testable>

## Explicitly NOT in scope
<bulleted; the negative space>
```

This is the load-bearing anchor; subsequent commands cite it. If the user defers ("figure that out later"): write a stub with `Status: DRAFT — needs sharpening` and decompose-plan will refuse to proceed without sharpening.

### 3. Scaffold

Three files to write, all from inline shapes (no .tpl files — frontier model composes from these descriptions):

**`<repo-root>/AGENTS.md`** — project operating instructions. Shape:

```
# Agent Operating Instructions — <PROJECT_NAME>

Private (gitignored). Read this before touching code. Applies to every coding agent: Claude Code, Codex, review subagents.

## What this project is
<one paragraph: scope; one paragraph: what's explicitly NOT in scope>

## Hard invariants — never break
<numbered list, smallest set you'd reject any PR for; 3-7 typical; the shorter, the louder>

## <DOMAIN> policy (binding)
<authority matrix if applicable; forbidden actions>

## File map
<table: Area | Path>

## Commands
<shell commands for build / test / typecheck>
```

If AGENTS.md exists: **merge mode** — read existing; surface a diff of proposed additions/edits to the user; ask which to apply. Never overwrite destructively.

**`<repo-root>/docs-private/RESUME-NOTES-<today>.md`** — controller handoff. Shape:

```
# Resume Notes — <DATE> (rev 0)

## TL;DR
<one paragraph: where we are, what's in flight, what's queued>

## Code state
Branch: <branch> @ <head>
<git log --oneline -10>

## Reading order on wake
1. AGENTS.md
2. docs-private/<topic>-goal-statement-<today>.md
3. docs-private/<topic>-goal-queue-<date>.md
4. <next: whatever is queued>

## First 5 minutes
<exact next steps for the next controller>
```

After init: `(rev 0)` H1 line literally says "init complete; ready for `/goal-flight decompose-plan`." Bump `(rev N)` for subsequent revisions; never overwrite.

**`<repo-root>/docs-private/worker-context.md`** — OPTIONAL, only create if AGENTS.md is huge (>1000 lines) or the project has multiple distinct worker profiles. Default: skip — executors read AGENTS.md directly per the "Worker context is optional" hard convention in `SKILL.md`. If you create it, it's a ~150-line precis with: one-line scope, the 3-5 most load-bearing invariants, where to put new code (path table), build/test commands. Executors read this instead of full AGENTS.md.

Also create `<repo-root>/docs-private/.gitkeep` (empty file) so docs-private/ is tracked-as-directory but its dated contents are gitignored per the repo's existing .gitignore policy.

Forensics live in the harness-captured session JSONL + per-subagent JSONL + codex tail files + RESUME-NOTES + git log. No separate `controller.log` is created.

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

#### 4-pass pipeline (canonical shape — invoke from here or via `/goal-flight build-corpus`)

`commands/build-corpus.md` documents the full 4-pass pipeline (slice builders → per-slice reviewers → cross-slice consolidation → final assessment) in detail. Init step 3.5 invokes the same pipeline; the cost calculus matches.

For init: spawn the pipeline now. Each pass uses Claude subagents (Opus for code-adjacent slices: `patterns/*`, `verification.md`, `decisions.md`; default model for prose-only slices). Slices land in `docs-private/rag/<filename>` with `verified-at: <HEAD-SHA>` frontmatter per `templates/rag-corpus-schema.md.tpl`. No per-pass prompt files needed — the slice schema + source-list table + verification-first principle (slices are starting hypotheses the executor verifies, not authoritative facts) are sufficient brief for frontier-model subagents.

Pass-specific briefs (controller composes from these on dispatch — frontier model fills in details):

- **Pass 1 (builders, parallel)**: read source paths from the table above; produce slice file at schema-defined path; frontmatter `verified-at: <current-HEAD>`. ~10 concurrent max; 4–6 slices for small projects, 10–15 for larger.
- **Pass 2 (reviewers, parallel, one per slice)**: read slice + sources; verify grep patterns against actual code; score 1–5 (Factual / Complete / Voice / Dispatch-ready); P0/P1/P2 findings. Block Pass 3 until P0+P1 patched.
- **Pass 3 (consolidator, one Opus pass)**: pass all corpus file absolute paths; identify cross-slice contradictions, deduplicate, refresh `verified-at` on slices reviewed-but-not-rebuilt. Codex fallback only if Opus unavailable.
- **Pass 4 (assessment, one Opus pass)**: aggregate scores into quality dashboard; recommend next-wave priorities; issue CORPUS IS DISPATCH-READY / NEEDS-MORE-ITERATION verdict. Dashboard → RESUME-NOTES; priorities → drive future `/goal-flight build-corpus --next-wave`.

**Outcome**: `docs-private/rag/` is populated, reviewed, scored. Future dispatches reference these slices as starting hypotheses (per `prompts/dispatch-wrapper.md` corpus integration); controller's context budget preserved for integration / requirements adjudication / orientation calls.

### 4. Ensure gitignore + AGENTS.md tracked

Read `<repo-root>/.gitignore`.

- Append `docs-private/` if missing (holds per-session state: RESUME-NOTES, goal-queue, goal-statement, rag/).
- **AGENTS.md should be tracked, not gitignored** — worktrees inherit tracked files automatically; gitignored AGENTS.md silently disappears from worktree checkouts and defeats the auto-load directive (step 4.5). If gitignored: ask y/n to remove from gitignore (default yes). If not committed: suggest `git add AGENTS.md`. If user explicitly wants AGENTS.md gitignored (privacy/license): note in summary that worktree controllers need a symlink (`ln -s <main>/AGENTS.md <worktree>/AGENTS.md`) or to run from main only.

### 4.5. Ensure AGENTS.md will be auto-read by future sessions

Claude Code doesn't auto-load `AGENTS.md` (Codex does natively). To make it reliably the first thing future sessions read, ensure a CLAUDE.md directive exists pointing at it.

Check both: `grep -l "AGENTS.md" ~/.claude/CLAUDE.md 2>/dev/null` (machine-wide) and `grep -l "AGENTS.md" <repo-root>/CLAUDE.md 2>/dev/null` (per-repo). If neither, ask which scope: global (all your projects, no team propagation), project-level (this repo only, propagates via git), or both.

Snippets to append (don't overwrite existing CLAUDE.md):

**Project-level** (`<repo-root>/CLAUDE.md`):
```
## Read AGENTS.md first
This project pins agent operating instructions in `AGENTS.md` at the repo root. Read it before doing any work — it carries the project invariants, file map, and conventions.
```

**Global** (`~/.claude/CLAUDE.md`):
```
# session start
At session start in any project, if `AGENTS.md` exists at the repo root, read it before doing other work — makes behavior symmetric with Codex (which auto-loads AGENTS.md natively).
```

If user declines all options: note in summary that AGENTS.md will need manual Read at session start (or be invoked via `/goal-flight` which always reads it).

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
