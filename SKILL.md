---
name: goal-flight
version: 0.3.2
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

# /goal-flight

Turn a fresh Claude Code session into a **controller** for long-running, decomposed code work — refactors, multi-turn implement-from-architecture-doc, porting, recursive end-to-end testing, finite Ralph/Karpathy loops, scientific convergence against ground truth or first principles.

**The controller's role is high-level management, not execution.** Its value-add is holding enough context about the project's goal, scenery (constraints, architecture, prior decisions, failure modes), and user intent to exercise discretion and recommend the next move — then dispatching actual work to workers (Claude subagents, codex, grok) that don't need that context. The dispatch / review / handoff machinery below exists to free the controller to do the management. This is the frontier of lightly-supervised development once the underlying coding harness (goal-mode, executor self-review, milestone reviewer sweeps) is running smoothly: you check in, ratify suggested moves, redirect when needed, and trust the controller to keep the project anchored to intent across compactions and unattended hours.

Operationally: the controller dispatches `/goal` chunks to executor subagents, embeds adversarial self-review in every dispatch, runs parallel codex+claude review sweeps at milestones (via [gstack](https://github.com/garrytan/gstack)'s `/review` skill when installed), and writes dated handoff notes before context fills. Designed for ~12-hour unattended runs where you check in periodically rather than babysit.

## Session pre-flight (silent — surface only what fires)

On `/goal-flight` invocation, the controller orients itself with four fast probes before responding to any sub-command. Silent on a fresh project; surfaces drift before it bites.

1. **Skill version + load-time fingerprint** — resolve the skill root. Enumerate **both** locations and surface ambiguity when both exist:
   ```bash
   CLONE_FORM=$( [ -d ~/.claude/skills/goal-flight ] && echo ~/.claude/skills/goal-flight )
   PLUGIN_FORM=$(find ~/.claude/plugins -maxdepth 4 -type d -name goal-flight 2>/dev/null | head -1)
   ```
   If both resolve and differ: surface "Multiple goal-flight installs detected: <clone> AND <plugin>. The harness loaded one of them; this probe can't tell which. fprint may match the wrong tree." Then pick clone-form by convention (consistent baseline). If only one resolves, use it. If neither resolves, skip the probe silently.

   Compute the load-time fingerprint via this recipe (canonical home; other sites cite "per pre-flight recipe"):
   ```bash
   SKILL_ROOT=<resolved-above>
   VERSION=$(head -1 "$SKILL_ROOT/VERSION" 2>/dev/null || echo missing)
   GIT_SHA=$(git -C "$SKILL_ROOT" rev-parse --short HEAD 2>/dev/null || echo no-git)
   # Fail loud if any of the 3 behavior-bearing files is missing — partial installs
   # would otherwise hash whatever cat succeeded in reading and emit a plausible-but-wrong fprint.
   MISSING=""
   for f in "$SKILL_ROOT/SKILL.md" "$SKILL_ROOT/commands/execute.md" "$SKILL_ROOT/prompts/dispatch-wrapper.md"; do
     [ -f "$f" ] || MISSING="$MISSING ${f#$SKILL_ROOT/}"
   done
   if [ -n "$MISSING" ]; then
     FPRINT="incomplete($MISSING)"
   else
     FPRINT=$(cat "$SKILL_ROOT/SKILL.md" "$SKILL_ROOT/commands/execute.md" "$SKILL_ROOT/prompts/dispatch-wrapper.md" | shasum -a 256 | awk '{print $1}' | head -c 8)
   fi
   LOADED_LINE="Skill-loaded: ${VERSION}@${GIT_SHA} fprint:${FPRINT}"
   ```
   Quote `LOADED_LINE` as a parenthetical in the session's first non-trivial response (e.g. `(Skill-loaded: 0.2.8@<sha> fprint:a1b2c3d4)`). The same line is written into goal-queue and RESUME-NOTES headers when those files are created (see `commands/decompose-plan.md` step 3 / `commands/init.md` step 3) and read back by probe 4 to detect updates mid-run. When `GIT_SHA=no-git` (plugin-form install with no `.git`) or `FPRINT=incomplete(...)`, that's information — record it as-is; downstream comparison should treat any non-no-git-non-incomplete change as drift.
2. **In-flight state** — `find docs-private -maxdepth 1 -name 'RESUME-NOTES-*.md' -mtime -7 2>/dev/null | sort | tail -3` (mtime-filtered: only recent files trip the nudge; year-old RESUME-NOTES from a prior topic don't). If any match and the user invoked something other than `resume` / `goal` / `init`, surface a one-line nudge: "RESUME-NOTES from <date> exists — run `/goal-flight resume` first?" User redirects if they meant to start fresh.
3. **Corpus drift** — if `docs-private/rag/` exists, read the oldest `verified-at:` SHA from slice frontmatter (format pinned in `templates/rag-corpus-schema.md.tpl` §Frontmatter — YAML frontmatter, lowercase `verified-at`, full 40-char SHA). If `git rev-list --count <SHA>..HEAD` exceeds 20, surface "corpus is N commits stale — executors will verify aggressively or run `/goal-flight build-corpus --next-wave`."
4. **Skill-update drift** — when an in-flight goal-queue (`docs-private/goal-queue-*.md`, or legacy `docs-private/*-goal-queue-*.md`) or RESUME-NOTES contains a `Skill-loaded:` header, compare it to the live `LOADED_LINE` from probe 1. Three outcomes:
   - **Match** (exact string equality): silent. No nudge.
   - **No header**: legacy file written before 0.2.2 shipped this convention. Silent. Treat as no-data, not as drift.
   - **Malformed header** (parse fails, version field missing, fprint not 8-hex or `incomplete(...)`): surface "Malformed Skill-loaded header in `<file>`: `<raw line>`. Cannot compare." Continue without further drift checks against this file.
   - **Differs**: surface forensics, not just a vague "updated" message:
     ```
     Skill drift in <file>:
       stored: Skill-loaded: 0.2.7@e3a7726 fprint:abcd1234
       live  : Skill-loaded: 0.2.8@9f12ee01 fprint:ef567890
       changed fields: version (0.2.7 -> 0.2.8), git_sha, fprint
     Likely cause: skill repo advanced (git pull, edit, or new commit). Re-invoke /goal-flight to refresh SKILL.md, or read the section that changed. If versions match but fprint differs, the change was uncommitted or sub-version (in-repo edit between fprint capture and now). If versions and git_sha rolled BACKWARD relative to stored, the skill repo checked out a prior commit during this session — likely a deliberate rollback; "Skill drift" is the correct framing (read as "changed", not "updated"), the new line IS the current state, no action needed unless you want to fast-forward.
     ```
     One block per affected file (goal-queue and RESUME-NOTES are checked separately; both may fire). Format compactly when in pre-flight to avoid noise.

A fresh project trips none of these. They cost ~50 ms (~200 ms on slow-startup shells) and prevent silent staleness compounding across a 12-hour run.

## Sub-commands

```
/goal-flight                              # print this file
/goal-flight init <topic>                 # check tooling, audit repo, scaffold AGENTS/docs-private/
/goal-flight decompose-plan [<plan-file>] # break a plan into /goal chunks; review the decomposition
/goal-flight ask-questions [<scope>]      # spawn anticipatory subagents; surface clarifying questions
/goal-flight execute [--parallel <N>]     # run the per-chunk loop
/goal-flight build-corpus [<flags>]       # extend / rebuild the docs-private/rag/ corpus
/goal-flight register-codex [<path>]      # register a project as codex-trusted (re-run safely)
/goal-flight validate-dispatch [<slug>]   # dry-run a dispatch wrapper; catch malformed layers
/goal-flight validate-queue [<path>]      # schema-check the goal-queue
/goal-flight resume                       # rebuild RESUME-NOTES from current git state
/goal-flight goal <SLUG>                  # append one goal to the queue
```

Dispatch on the first token by reading the matching `commands/<token>.md`; `resume` and `goal` are handled inline (see bottom of this file).

If no args: print this file.

## Hard conventions

### Dispatch model

- **Default to Claude Agent tool** (`model: "opus"`, highest reasoning) for code-writing chunks. Claude session billing, not API billing. **Never `claude -p` for worker tasks unless the user is known to be on enterprise / API billing already** (where the session-vs-API tradeoff doesn't apply because they're paying that way regardless). Default assumption: the user is on session billing and `claude -p` would double-charge for work the Agent tool covers free.
- **Codex `exec` is a peer dispatch target**, not just a reviewer. Use `Bash timeout 300 codex exec '<short pointer-shaped prompt>'` — keep the prompt short, pass file pointers (the agent reads what it needs at the time it needs it). The MCP approval-gate stall that otherwise breaks ~2/5 codex dispatches is solved at init by `scripts/install-codex-overrides.sh` (registers the project + its worktrees by path prefix as codex-trusted).
- **In-session agent-loop primitives exist in both codex and grok.** Both run a multi-hour autonomous plan→act→test→iterate loop when given a goal-shaped prompt (Objective + Workspace + Rules + Acceptance + Test gates + Final response schema — see `templates/codex-goal-prompt.md.tpl`; same shape applies to grok). Empirically verified end-to-end 2026-05-17 on a synthetic failing-test scenario: both ran the full edit → pytest → green loop.
  - **Codex `/goal`** (interactive slash command) — requires codex ≥ 0.128.0 and `features.goals = true` in `~/.codex/config.toml`. **Headless dispatch** (what goal-flight uses): `codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox -C <workdir> - < prompt.md` — the bypass flag is required for autonomous edits (without it codex correctly emits `BLOCKED: filesystem is read-only and approvals are disabled`).
  - **Grok `/implement`** (interactive slash command, activates the `implementer` role per `~/.grok/bundled/roles/implementer.toml`) — full-access implementer mode. **Headless dispatch** (what goal-flight uses): `grok --prompt-file prompt.md --cwd <workdir> --permission-mode acceptEdits --output-format plain` — the `acceptEdits` permission mode is required for autonomous edits. Grok's default agent loop activates when given a goal-shaped prompt; the interactive `/implement` slash command and the headless `--prompt-file + acceptEdits` invocation produce equivalent loop behaviour.
  - **`/goal` is the unified chunk marker** in the goal-queue (a structural / human-readable label for the file, not a CLI invocation). At dispatch time the controller composes the right headless invocation for the target executor (codex `exec ...` vs grok `--prompt-file ...`) — the interactive `/goal` and `/implement` slash commands are user-facing surfaces, not dispatcher entry points. To Claude Opus executors there is no in-session loop primitive — Opus iteration runs as an external loop driven by the controller (one Agent dispatch per iteration; controller parses Final response block, re-dispatches with updated `Iteration N of MAX, Prior progress: ...` preamble).
- **Three dispatch paths** (frontier models pick; the skill doesn't prescribe between Opus / codex / Grok):
  1. **`[controller-direct]`** — controller does the work inline. Two triggers: (a) trivially small (single-file, < ~30 LoC, no cross-module coupling); (b) controller already has the session-loaded state a fresh subagent would have to re-discover (mid-debug, just-consumed milestone-review P0, rolling decisions not yet in `decisions.md`). Heuristic for (b): a clean dispatch wrapper would exceed ~5 KB primarily because of context the controller already holds. **Interactivity tradeoff** (real, often-overlooked): while the controller inlines, the parent session is tied up executing tool calls and unresponsive to user comments / questions / redirects. Subagent dispatch frees the parent session so the user can interject mid-flight. Prefer subagent dispatch (path 2) when (i) the user is actively at the keyboard and might want to comment, (ii) the work will take more than ~1 minute even if the LoC delta is small, (iii) parallel-safe with the next chunk so a look-ahead subagent could run alongside. Inline only when the controller's session-loaded state is genuinely load-bearing AND the work is short enough that brief unresponsiveness is fine. (ESC interrupts the current tool call — including a subagent dispatch — but doesn't roll back what's already on disk.)
  2. **Single-shot subagent** — Agent tool (Claude) OR codex `exec` short-prompt OR `grok -p` short-prompt. One dispatch, executor reports done. The default path for most chunks. Frontier model picks the executor target based on chunk shape.
  3. **Goal-mode loop** — codex `/goal` (in-session, codex ≥ 0.128 + features.goals) OR grok `/implement` (in-session, grok's CLI equivalent) OR external loop driven by the controller for Claude Opus (one Agent dispatch per iteration; controller parses Final response block, re-dispatches with updated `Iteration N of MAX, Prior progress: ...` preamble). Iteration cap: 5–8 default, configurable via `[max-iterations:<N>]` chunk tag.
- **Transport choice — ACP-first when available.** The three paths above describe workflow SHAPES; ACP (Agent Client Protocol over stdio JSON-RPC) is a TRANSPORT that composes with the single-shot and goal-mode shapes. When the target worker speaks ACP — empirically validated 2026-05-18 for codex-acp / grok agent stdio / cursor-agent acp / claude-code-cli-acp via the smoke and failure-mode tests under `test/` — prefer ACP over Bash-`&`-tail-file for these reasons:
  1. **Structured events** (`agent_message_chunk`, `agent_thought_chunk`, `tool_call`, `tool_call_update`, `plan`) replace tail-file marker grep. Same `STATUS:` / `RESULT:` / `USER-NEED:` / `USER-CONFIRM:` / `BLOCKED:` / `COMPLETE:` vocabulary is extracted from accumulated `agent_message_chunk` text via `scripts/acp_runner.extract_markers()`.
  2. **Persistent sessions** — `AcpConnection` per `(agent, session_id)` survives multiple chunks. `USER-NEED:` clarifications go through the existing session instead of re-spawning the whole worker (per §Worker message passing's "For ACP / persistent-session workers" clause).
  3. **Controller-side auto-allow** substitutes for plan-tier Auto Mode features (works on any subscription tier — see `scripts/acp_client.py` `auto_allow_tools` flag).
  4. **Three of four workers are sub-billed by default** through their ACP entry points (codex via OpenAI Pro device-auth; cursor via Cursor sub; claude via Claude Code session through `claude-code-cli-acp` PTY wrap). Grok is the only API-default — opt into SuperGrok device-auth to sub-bill that too.

  **Tags**: untagged-ACP-capable chunks default to ACP; force ACP explicitly with `[acp]` (e.g., to override an otherwise-Bash-tail dispatch); force legacy Bash-`&`-tail with `[bash-tail]` (e.g., for one-shots where the ACP session overhead isn't worth it).

  **Pool**: long-running goal-flight runs MUST use `AcpProcessPool` (not bare `AcpConnection`) — only the pool writes to the disambiguated pidfile that survives controller crashes. See `scripts/acp_client.py` `cleanup_ghosts()` for the identity-verified PID-reuse-safe cleanup. Use the `managed_pool()` async context manager (in `scripts/acp_pool.py`) which wires SIGINT/SIGTERM/atexit handlers so controller crashes don't orphan workers.

  **Capacity**: pool sizing reads `docs-private/env-caveats.md` (populated by `commands/init.md` step 1.5 via `scripts/probe-box-capacity.sh`). Worst-case worker RSS is ~1.2 GB (cursor-agent peak); pool ceiling = `(RAM_MB - 2048) // 1200`, capped at 20. On RAM-tight boxes this matters; on big boxes server-side rate limits bind first.

  **Fallback**: workers that don't speak ACP (or where `docs-private/env-caveats.md` shows the ACP adapter is missing) fall through to Bash-`&`-tail-file dispatch automatically.

- **Token bias defaults UP** — largest model + highest reasoning + parallel reviewers + corpus eagerly built — because subagent / codex / grok tokens are cheap relative to engineering quality. Override DOWN per-chunk via `[controller-direct]` or a smaller `model:` field for trivially-small work.
- **Channel routing.** The three-paths section above doesn't prescribe between Opus / codex / grok within a path. When the user states a routing preference at session start, apply it consistently through the run. Typical override: route coding-heavy chunks to codex when Claude session-limits bite from concurrent runs; reserve Claude for orchestration and milestone reviews (gstack `/review` Claude-side); use codex `/review` (via gstack) for the parallel second-opinion review channel. Grok remains an executor (single-shot `grok -p` or `/goal`-loop iteration, or `[acp]` via `grok agent stdio` when structured events are wanted) — wiring grok-p as a review-channel target is forward work.

### Verification-first dispatch wrapper

The wrapper **scaffolds what the executor should investigate**; it does NOT substitute for investigation. Controller-pasted "facts" (file:line refs, function signatures, invariant restatements) go stale on the timescale of minutes; frontier models trust pasted text because the controller is upstream in the trust hierarchy. Pointers force the agent to re-verify against live disk and surface drift.

Full spec: `prompts/dispatch-wrapper.md`. **Prompt size has two paths into codex goal-mode**: interactive `/goal` slash command (human typing in a codex session) caps the goal text at ~4000 chars — codex bounces longer; non-interactive `codex exec -C <workdir> - < prompt.md` (the path goal-flight uses for `[goal-mode]` chunks) has no such cap, empirically verified to 4407 chars on codex 0.130.0 + gpt-5.5 (probe 2026-05-17). Since the controller dispatches via the non-interactive path, the 4k limit doesn't bind on automated chunks. The remaining discipline across all dispatch shapes (codex / Agent / grok / ACP) is points-not-pre-paste — large prompts are a smell test for the controller over-asserting facts the executor could discover. Layer 0 (base-verification pre-flight) is mandatory for worktree-isolated dispatches; Layer 6 (marker vocabulary, one line) is universal so the worker can signal `USER-NEED:` / `BLOCKED:` back through the marker channel rather than guessing past ambiguity.

### Codex reliability

- **Register every project as codex-trusted** at init time via `scripts/install-codex-overrides.sh`. Without it, non-interactive `codex exec` blocks indefinitely on the first MCP tool call when context-mode (or any MCP server with `approval_mode = "approve"`) is registered — codex surfaces this as `request_user_input is not supported in exec mode for thread <id>` followed by silent retry loops. Trust registration auto-approves the call, dissolving the loop. Trust is prefix-based; worktrees inherit automatically. See `commands/register-codex.md`.
- **Register context-mode on codex side** at init time via `scripts/register-context-mode-codex.py`. The script handles the plugin-form vs explicit-form detection on Claude side and writes the canonical `npx -y context-mode@latest` codex registration. Without it, `codex exec` works but loses the context-mode multiplier for that dispatch.
- **Codex CLI ≥ 0.128.0** for `/goal` mode (older versions still work for reviews / consolidation / short-prompt dispatches — they just don't have the in-session loop primitive). Init step 1 checks the version and recommends `codex update` if older.
- **Pre-install external dependencies before `/goal` mode dispatches.** Multi-hour autonomous loops + mid-iteration `pip install` (or `npm install`, `uv sync`, `cargo build`) introduce a real friction class: codex tries to surface-install dependencies on demand, which can wedge on network or leave half-installed venvs that the next iteration trips over. Resolve the dependency surface in the workspace BEFORE composing the dispatch (e.g., `pip install -r requirements.txt`, `uv sync`, `npm install`). Surface known-missing packages in the dispatch Rules so codex doesn't try to surface-install. Same principle for short-prompt codex dispatches when the chunk imports something not in the base env.
- **Never wrap `codex exec`, `grok -p`, or `claude -p` inside an MCP tool call** like `ctx_execute` or `ctx_batch_execute`. Context-mode wraps shell calls in an FTS5 sandbox subject to MCP timeouts; long headless dispatches (≥ tens of seconds, which is most useful codex / grok / claude review or /goal-mode invocations) hit the MCP/context timeout before the dispatch completes — the controller sees a hang, the underlying process ran fine and exited normally, but the output is stuck in the OS-captured stdout the MCP wrapper never returned. Run headless dispatches via Bash with `>` redirection to a file, background `scripts/watch-dispatch-tail.sh` for content-aware completion + orphan-defense pidfile registration (or, for one-off dispatches where the watcher is overkill, `while kill -0 $PID 2>/dev/null; do sleep 15; done`), then `ctx_search` / `ctx_execute` over the captured output **AFTER** the dispatch exits. Reminder: for Claude code-writing chunks, prefer the Agent tool with `model: "opus"` over `claude -p` — Agent subagents are session-billed; `claude -p` is API-billed. See Dispatch model section above. Failure-mode symptom: the controller appears to stall on a single MCP tool call with no streaming output for minutes; if you see this, kill the wrapping MCP call and re-dispatch the underlying command via Bash + redirection.
- **Autonomous goal-mode dispatch needs `--dangerously-bypass-approvals-and-sandbox`** plus `--skip-git-repo-check` when the workspace isn't a git repo. Without the bypass flag, codex correctly emits `BLOCKED: filesystem is read-only and approvals are disabled` after attempting the first edit and the chunk fails. With the flag, codex runs unsandboxed and can edit anywhere in the workspace. **Always combine with `-C <workdir>`** — without an explicit working-directory pin, codex edits the controller's cwd, which collapses any worktree-isolation safety story. The bypass flags trade sandboxing for autonomy; the worktree boundary provides external sandboxing **only when `<workdir>` is a sibling worktree** (`--parallel` mode, where each chunk runs in its own `.claude/worktrees/<slug>/` tree). In **sequential mode** where `<workdir>` is the controller cwd (or any non-isolated path), there is no sandbox — the per-chunk verify-diff step in `commands/execute.md` step d is the only scope check. Don't dispatch sequential bypass-mode chunks against repos with uncommitted unrelated work; either parallel-isolate or accept that the diff-verify is your only fence. Empirically verified 2026-05-17: same `[goal-mode]` synthetic-failing-test scenario completes the full edit → pytest → green loop in ~92 s with the flag; emits `BLOCKED:` correctly without it. For non-`[goal-mode]` short-prompt codex dispatches that don't need to edit, the bypass flag is unnecessary.
- **Wrap `codex exec` in `timeout --kill-after=10 300`** for non-`/goal` dispatches. Codex v0.130.0 has no `--timeout` flag. 300 s is 10× observed p95 (~25 s healthy). **Do NOT** wrap `/goal` mode dispatches in `timeout 300` — they're multi-hour by design; the controller monitors the tail file for the Final response block to detect completion.
- **Backstop watchdog** for residual stalls (network wedge, codex-internal hang): zero-output ≥ 90 s OR no-progress ≥ 180 s ⇒ kill + retry as Claude general-purpose subagent. Applies to short-prompt dispatches only; for `/goal` mode, long pauses during plan/act/test/iterate cycles are expected and the watchdog would false-positive.
- **Fallback when trust can't be registered** (`--ignore-user-config`): shared machine, one-off invocation, or `~/.codex/config.toml` shouldn't be mutated. Run `codex exec --ignore-user-config '...'`. Loses MCP tool access in the dispatched session; trust-registration is strictly preferable when feasible.

### Asking discipline — north star

The controller's value is forward motion until a real blocker. **Interrupt the user only when a decision genuinely affects the project's north star** (default: code quality + first-principles scientific integrity; configurable per project).

- **Don't ask trivia.** Worktree labels, file naming nits, paint colors — never ask. Just decide.
- **Don't do Netflix "are-you-still-watching" check-ins.** "Step 1 done. Continue?" when nothing is blocked is the antipattern. Running commentary as work progresses IS welcome — short status lines giving the user a window into the work. The thing to cut is the implicit-stop pattern ("Hold or go?", "Proceed?") on routine forward motion.
- **Don't monopolize the parent thread with long inline work.** While the controller inlines (`[controller-direct]` path), the user cannot comment, question, or redirect — the session is busy executing tool calls. Dispatch to a subagent (path 2) for anything that won't finish in ~1 minute, even if the LoC delta is small, so the user retains the ability to interject. ESC will interrupt the current tool call (including a subagent dispatch) but won't roll back what's already on disk. See §Dispatch model `[controller-direct]` for the full interactivity tradeoff.
- **Background-dispatch anything expected to take more than ~10 seconds** — so the user's terminal doesn't hang, allowing them to steer. `run_in_background: true` for Agent; `&` + redirection-to-file for Bash codex / grok. The harness re-surfaces completion as a new turn via task-notification. See §Per-chunk loop for the canonical shape.
- **Prepare the question with subagents first.** While waiting on a long-running subagent or codex review, dispatch anticipatory reviewer-loop subagents to pre-resolve choices. A well-prepared ask with subagent-vetted options is worth roughly 5 raw asks.
- **Do ask when** a chemistry/physics/security/correctness assumption needs user values, when a destructive operation is about to fire, or when a decision would lock in a wrong invariant.

### Inline office-hours — premise re-validation against drift

On long unattended runs, the dominant failure mode isn't bad code — it's **silent gap-fill or downstream propagation of errors**. The controller (or an executor) fills an absence of explicit context with a default-of-its-own, OR an early chunk makes a wrong call that subsequent chunks inherit as if it were ground truth. Either way, the misread lands in a load-bearing artifact (goal-statement, plan-of-record, premises file, code comment, commit message) and downstream chunks operate on it as if it were settled fact. By the time the user catches it ("`status='cancelled'` isn't normal state to aggregate — those are checkout-failure rows, not historical inventory"), three chunks have been built on the misread and the rollback cost is multiplied across them. Figuratively, this class of issue accounts for an outsized share of avoidable rework on unattended runs — the protocol below exists so the controller can dispatch without anxiety about silent drift or compounded errors.

**Deeper function**: a long unattended run's single most important controller-side job is remembering **what is the point of what we're doing** — the answer must survive compaction, session boundaries, and the drift of dozens of executor dispatches whose JSONL transcripts the controller will never re-read. The premises file (peer to the goal-statement) IS the compaction-surviving anchor; inline office-hours keeps it sharp.

The pattern: keep an **inline office-hours backlog** of clarification and consideration items, **cherry-pick** them when the user is present and a long-running dispatch is in flight. Three sources feed the backlog:

- **Inferred premises** the controller is operating on that weren't explicitly stated by the user. Heuristic: "I'm enacting premise X. Was X stated in the goal-statement / plan / prior turn? If not, X is inferred — backlog candidate."
- **Gap-fills** — places the controller filled a documentation absence with a default-of-its-own. Heuristic: "I had no input on Y; my fill was Z because that's the obvious default. Was Z actually right, or is the absence-of-input itself a signal that Y is a real missing-system / missing-spec / missing-assumption that should be documented upstream?" Gap-fills are higher-leverage than inferences: the controller will never spontaneously hedge on a fill it doesn't realize it's making, so making the gap visible IS the value.
- **Forward considerations** — observations, connections, and open design questions the user hasn't yet thought of explicitly. The controller isn't just a sentinel for drift; it's a thinking partner. While holding context across compactions and chunks, the model may notice an edge case the current approach doesn't handle, an asymmetry between two parts of the design worth aligning, a pattern related to a broader concept worth generalizing, or a decision worth raising before the next milestone forces it implicitly. These belong in the backlog too. **Advancing and perfecting the concept in partnership** with the user is part of the controller's value-add, not just catch-up and maintenance.

At dispatch-turn-end (the natural pause while a background executor runs), the controller may append **one** premise-check question to the one-line status — the most topically-relevant entry from the backlog, framed concretely:

```
Dispatching chunk #N (`<slug>`). Background task <id>.
[Premise check] I'm filling the inclusion-criteria gap by treating `status='cancelled'` rows as a separate category in the aggregate, because the goal-statement doesn't specify how to handle them. Confirm: is `cancelled` a normal historical state worth aggregating, or does it indicate checkout failure (and should be filtered out)?
```

The user answers when convenient (next turn or several turns later, possibly after several chunks have landed). The answer goes into the persistent premises artifact (see below); chunks that touch the same surface read the artifact and respect the now-validated premise.

**Cadence**: 0–1 premise-check per dispatch turn. **Non-blocking by default** — the controller proceeds with chunk N+1 on the next turn regardless of whether the user has answered. **Blocks** only when the controller is already mid-untangle of detected drift from a prior unvalidated premise (downstream is already paused; the answer is what unblocks the next move).

**Backlog hygiene**: the controller adds to the backlog as it works. Recompute is a judgment call by the frontier model running as controller — a quiet stretch needs no recompute, a startling clarification (the user just said something that invalidates a whole branch of the queue) warrants immediate refactor of the remaining questions, and the in-between is opportunistic, e.g. while waiting on a long compile or codex run with nothing immediately useful to do. No fixed cadence; not algorithmic; not heavily prescriptive. The model can also tell when a later question has been answered indirectly by an earlier reply and silently drop it. The backlog can be empty for whole chunks if nothing material is open.

**Strategic-frame questions**: not every backlog item is a tactical "is X correct?". The same surface is right for strategic questions too — "what's the actual measurement we're trying to make?", "what would success look like in 6 months?", "is the chunked approach actually fitting the shape of the problem?". The cherry-pick logic ranks topical-to-current-chunk first but a long-running session is also the right time to polish the upstream framing.

**Artifact**: `docs-private/premises-<topic>-<date>.md`. Shape:

```
# <TOPIC> — Premise Distillation
Goal-statement: docs-private/goal-<topic>-<date>.md  (strategic anchor)
Last updated: <date>

## Validated premises (anchor against these)
- `status='cancelled'` rows are checkout failures, not historical inventory; exclude from aggregates.
  [Validated <date>. Source: user reply in chat, turn <marker>.]
- Downstream analytics must remain agnostic to specific tenant_id distributions.
  [Validated <date>.]

## Open premises (inferred or gap-filled; not yet validated)
- I'm assuming `status='cancelled'` is a normal historical state to preserve in the aggregate. Source: my own reading of <file:line>.
  Flag if next chunk depends on this.
- ...

## Corrected premises (back-audit log)
- <date>: I had operated as if `cancelled` rows were a normal historical category.
  Corrected by user: `cancelled` = checkout failure, not recoverable state. Action: rolled back chunk #7's
  aggregate; re-ran with `status != 'cancelled'` filter. Commit <sha>.
```

The premises file is a peer of the goal-statement (`docs-private/goal-<topic>-<date>.md`) and the goal-queue (`docs-private/goal-queue-<topic>-<date>.md`) — same naming convention so all three sort together when scrolling `docs-private/`. Read by:

- **Executor dispatches** via the verification-first wrapper Layer 4 (env caveats) — the Validated section becomes part of the executor's briefing so it doesn't re-derive (and potentially re-drift) the same interpretations.
- **`/goal-flight resume`** — reads the file into the rebuilt RESUME-NOTES so the next session inherits validated premises without re-asking.
- **Milestone reviewers** — cite Corrected premises as the back-audit log; if a regression looks like a re-emergence of a prior corrected drift, that's a P0.

**Architectural invariant — interrogation runs on the orchestrator, never on a worker.** Claude Code (this skill's session) is the orchestrator: it has the conversational surface to the user. `codex exec`, Agent-tool subagents (foreground or background), and `grok -p` are invisible workers — they receive a prompt and return a result; they have no channel to ask the user anything. Therefore: any polish-skill that interrogates the user (`/office-hours`, `/grill-me`, the inline backlog questions described above) must run as a Claude-side `Skill(skill: "<name>")` invocation OR be embodied by the orchestrator directly in its own assistant text. The codex-side install of gstack is for codex-as-reviewer (e.g., `/plan-eng-review` reading a draft, `/review` over a diff) — work that returns findings, not questions. Never `codex exec '/office-hours ...'` — codex has no one to ask.

**Front-end integration with polish-skills**: gstack ships several skills in this role — `/office-hours` (YC-style forcing questions), `/grill-me` (adversarial interrogation), `/eng-design-review` and `/plan-eng-review` (engineering critique), `/design-review` (visual / UX). The interrogative ones (office-hours, grill-me) run Claude-side per the invariant above; the review ones (plan-eng-review, design-review) can dispatch as workers because their job is to return findings rather than ask questions. At every plan-feeding entry point (`commands/init.md` step 2.5, `commands/decompose-plan.md` step 0.5), the controller **offers** the user a polish-skill pass — most often `/office-hours`, but the frontier model can pick a different one (or embody the gist directly in its own assistant text) based on what the planning artifact most needs. Accept → run the interrogation Claude-side, seed the premises file with the validated entries, then proceed on a polished plan. Decline → proceed; the backlog mechanism still operates inline once execution starts. The init / decompose-plan offering is "polish the upstream artifacts before decomposing"; the per-chunk-loop hook is "pepper for drift during execution." Same intent, two cadences.

**Incremental, opportunistic, user-steerable.** The mechanism earns its weight on long unattended runs where premise drift is the dominant failure mode; skip it for small projects, throwaway scripts, or chunks where the user is actively engaged anyway. The frontier model has judgment over when to ask, what to ask, when to recompute, and which polish-skill (if any) fits the current artifact — **user-steering beats rigid automation** (e.g., a user directive like "switch the next chunk to grok; Claude is rate-limited right now" should redirect the controller mid-run without code changes). The skill provides the surface; the model uses it.

### State — three layers, different scopes

- **`docs-private/RESUME-NOTES-<date>-(rev N).md`** — cross-session prose handoff. Survives compaction, session boundaries, machine reboots. Bump rev at: end of init, after decompose-plan, after each milestone review, before any anticipated compaction, on queue completion. Append-only within a day — never overwrite a prior rev.
- **`docs-private/goal-queue-<topic>-<date>.md` Progress table** — cross-session chunk-level state. One row per chunk; status = TODO / IN-FLIGHT / DONE / BLOCKED. Updated immediately after every commit. What `/goal-flight resume` reconstructs from. (Legacy file naming `<topic>-goal-queue-<date>.md` from < 0.3.0 is also accepted on read; new files use the new naming so `goal-*` artifacts cluster when scrolling `docs-private/`.)
- **TodoWrite (harness state)** — in-session tactical sub-step tracking. "Read the file. Edit the function. Run pytest." Survives in-session compaction but NOT cross-session. Optional. If state needs to survive `/goal-flight resume`, it goes in a file; if it's "next 3 tool calls," TodoWrite is fine.

### Handoff before compact

When context is ~80% full or compaction is imminent, write fresh `docs-private/RESUME-NOTES-<date>.md`. 80% is the default; the right threshold is a function of (remaining work in the queue) × (cost of waking afresh with summaries). Conserve harder when multiple subagent dispatches are in flight whose notifications carry mid-decision state; run hotter (90%+) when the queue is 1–3 trivial chunks from done and the most recent RESUME-NOTES rev already captures in-flight state. Subagents + `\goal` mode do most of the heavy lifting for extending session life — the controller's own context primarily holds metadata, not the bottleneck (as long as the controller doesn't pull large outputs into its own context).

### Context-mode multiplier

[context-mode](https://github.com/simonrowland/context-mode) installs MCP tools (`ctx_execute`, `ctx_batch_execute`, `ctx_search`, `ctx_fetch_and_index`) that offload large command outputs to an FTS5 sandbox and query them by pattern instead of stuffing context. Real multiplier for the controller pattern — diff verification, pytest output, goal-queue searches, codex tail monitoring all benefit. Init checks both Claude Code and codex MCP registrations and recommends install if missing.

### gstack integration

[gstack](https://github.com/garrytan/gstack) works on both Claude Code and codex. Init checks both install locations (`~/.claude/skills/gstack/`, `~/.codex/skills/gstack/`, plus project-level `.agents/skills/gstack/`) and recommends install if either side is missing. Prefer Claude-direct invocation (`Skill(skill: "review", ...)`) when available; use `timeout 300 codex exec '/review <range>'` for the parallel second-opinion perspective at milestones.

### Memory companions

goal-flight's plain-markdown approach (RESUME-NOTES + goal-statement + goal-queue + premises file) is fit-for-purpose for the compaction-survival problem and stays the default. Two optional companions worth considering when scope outgrows single-project / single-machine:

- **Cross-agent procedural memory from JSONL: CASS** ([cass_memory_system](https://github.com/Dicklesworthstone/cass_memory_system) + [coding_agent_session_search](https://github.com/Dicklesworthstone/coding_agent_session_search)). Rust CLI + Node MCP server. Eats Claude Code, codex, grok, cursor session JSONLs natively — distills procedural rules from session history with no LLM bill (lexical-only by default; semantic optional via local embedder install). Strong fit for cross-project *"did we validate this before on another topic?"* queries. Project partitioning: native via JSONL workspace path (encoded in filename); `cass search "X" --workspace <abs>` filters; default is global. Install: `brew install dicklesworthstone/tap/cass` and `brew install dicklesworthstone/tap/cm`.

- **Reflective synthesis (deep per-project): Hindsight** ([vectorize-io/hindsight](https://github.com/vectorize-io/hindsight)). Local Docker or `pip install hindsight-all` + Ollama for the local LLM backend. Four-network memory substrate (World / Experience / Observation / Belief) with `retain` / `recall` / `reflect` operations. The `reflect()` primitive is the differentiator — explicit belief synthesis with confidence scores, evidence chains, and reinforce / weaken / contradict update logic. Strong fit for high-level goal preservation across compactions and wrong-inference correction *within a single project's bank*. Project partitioning: `bank_id="<topic>"` per project — full isolation between banks (separate networks, separate beliefs). Cost: per-op LLM calls (free with local Ollama; per-token otherwise). Best when goal-flight runs span weeks / months with cross-run "what has the controller learned?" queries.

Pick by failure mode: cross-project search → CASS; deep per-project reflect synthesis → Hindsight. They're complementary — install both if both failure modes bite. Neither is required; the plain-markdown anchor is fit-for-purpose for typical single-project, single-machine runs. mem0, Letta, Cognee, Zep / Graphiti are out of scope for goal-flight's specific failure modes — see commit messages and `docs-private/codex-challenge-2026-05-16.txt` for the evaluation that ruled them out.

### Read discipline

- **Controller delegates *bulk* Read-heavy work to Explore subagents.** Don't read whole READMEs, architecture docs, or files >200 lines directly during init or execute — spawn an Explore agent, consume its summary. Short verification reads (< 200 lines, specific `file:line` ranges, targeted `grep` / `tail -n` / `sed -n`) are fine inline. The ban is on bulk context consumption, not on the controller using its eyes.
- **`AGENTS.md` is never overwritten.** If it exists, init proposes additions/edits as a diff for the user to apply.

### Self-delegation via `/fork`

Claude Code's `/fork` (renamed `/branch` in v2.1.77 but `/fork` still works) creates a new session branched off the current one with full conversation history inherited; the new session gets a new `CLAUDE_CODE_SESSION_ID`. **The forked session can self-detect via env var comparison against a marker the controller wrote before forking.**

Empirical identity surface (verified):

| Role | `CLAUDE_CODE_SESSION_ID` | JSONL path on disk |
|---|---|---|
| Controller | `X` | `<proj>/X.jsonl` (top-level) |
| Subagent (Agent-tool dispatch) | `X` (inherited) | `<proj>/X/subagents/agent-<hash>.jsonl` (nested) |
| Fork | `Y` (new) | `<proj>/Y.jsonl` (top-level sibling) |

Use `scripts/self-fork-detect.sh`:

```bash
# 1. Controller writes the contract BEFORE typing /fork:
bash scripts/self-fork-detect.sh write '<task the fork should execute>'
# Also captures a JSONL snapshot of ~/.claude/projects/ at this moment.

# 2. After /fork (or `claude --resume <sid> --fork-session`), in the new session:
bash scripts/self-fork-detect.sh detect
# Prints one of: ORIGINAL | FORK | SUBAGENT | NO_CONTRACT
# On FORK: also prints the task to execute + completion + abort signals.

# 3. (Optional) Controller locates the fork's JSONL on disk:
bash scripts/self-fork-detect.sh find-fork
# Prints any top-level JSONLs that didn't exist at write time and
# aren't the controller's own — fork candidates. Empty output =
# no fork yet (wait 1-2s after the user types /fork, then retry).

# 4. (Optional) Controller monitors the fork's progress live:
bash scripts/self-fork-detect.sh monitor <fork-jsonl-path>
# Polls the fork's JSONL (default every 5s), prints new assistant
# text as it appears, exits on "FORK-COMPLETE" marker OR if the
# JSONL stops growing for 120s (--idle-stop configurable).

# 5. After the fork's work is committed:
bash scripts/self-fork-detect.sh clear
```

Default contract path: `docs-private/.fork-contract.json` (gitignored by default per the existing `docs-private/` policy).

**Keyword marker vocabulary** (the only "return channel" — fork emits these in its assistant text; controller polls JSONL for them):

| Marker | Semantics | Monitor exit |
|---|---|---|
| `FORK-STATUS: <update>` | Intermediate progress; controller logs but keeps monitoring | (keeps polling) |
| `FORK-RESULT: <key>=<value>` | Structured output the controller should extract | (keeps polling) |
| `FORK-NEED: <question>` | Fork is blocked on a controller/user decision | **exit 2** (intervention required) |
| `FORK-COMPLETE: <summary>` | Fork is done | **exit 0** |
| `FORK-BLOCKED: <reason>` | Fork hit an unrecoverable issue, won't continue | **exit 1** |

`detect` (FORK case) prints this vocabulary to the fork so it knows what to emit; `monitor` greps for these strings and routes accordingly. The fork's work persists via git/RESUME-NOTES; the original controller also sees it on `/rewind` or `resume` if not actively monitoring.

The keyword mechanism is the workaround for forks lacking a task-notification analog. Subagents (Agent tool) get a proper callback on completion; forks don't, so the fork must DELIBERATELY emit grep-able strings the controller polls for.

**Resolving `FORK-NEED` (the fork asked a question; how does the controller reply?)** — forks similarly lack a clean inbound channel; there's no `--reply` flag that appends to a paused fork. Four options in order of cost:

1. **`/rewind` + redo in the controller** (default, recommended). Rewind to before the fork; do the work in the controller with the answer baked in. Zero extra cost. The fork's partial work, if it committed anything, persists on disk and can be cherry-picked or referenced.
2. **User manually replies in the fork window.** Switch to the fork's Claude Code window (or `claude --resume <fork-sid>` interactively) and type the reply. No cost; requires the user is at the keyboard. The fork resumes; the controller re-invokes `monitor` to keep watching.
3. **Sidecar reply file** (cooperative — needs pre-arranged contract). Controller writes `docs-private/.fork-reply.md` with the answer; the fork's contract pre-instructs it to re-read this file on its next turn. Still needs an external trigger for the fork's next turn (either option 2 or option 4) — so doesn't fully eliminate the orchestration step, but lets the controller stage the reply asynchronously.
4. **`scripts/self-fork-detect.sh reply <fork-sid-or-jsonl> '<reply prompt>'`** (API-billed; opt-in). Wraps `claude --resume <fork-sid> --print '<reply>'`. Fully automated. Anthropic's prompt caching makes this cheaper than the "claude -p antipattern" framing implies — the fork's prior conversation is cached prefix at ~10% rate, only the new reply turn + response are full new-token cost. For a short "read this file and answer" reply, typically a few cents per FORK-NEED resolution. The blanket "claude -p antipattern" rule in this skill is about WHOLE-CONVERSATION new dispatches at API rates (where session-billing would be free), not short cached-prefix continuations. Still meaningfully more expensive than option 1 over a long run; pick per context.

The right pick is usually option 1 — by the time FORK-NEED fired, the fork has already paused and the controller has the user's attention; `/rewind` + redo is faster than wrestling with the fork's reply channel.

When to use vs `[controller-direct]` vs Agent-tool subagent:

- **`[controller-direct]`** — controller does the work inline. Best for trivial chunks (single-file, <30 LoC) OR when controller-state matters but the work is short.
- **Agent-tool subagent** — fresh-context dispatch, task-notification on completion, full JSONL transcript. Best when you want the work in an isolated context (clean per-call), don't need controller's loaded state, and want the completion-notification affordance.
- **`/fork`** — branch the controller into a new session with full inherited state. Best when controller has substantial session-loaded context the work needs AND the work is risky/exploratory enough that a `/rewind`-able savepoint helps. Single-thread (no concurrent execution); reporting back is filesystem-only (commits + RESUME-NOTES; no task-notification equivalent for forks).

### Worker message passing — marker vocabulary

Workers (codex `exec` and `/goal`, Agent-tool subagents, `grok -p` and `/implement`, ACP workers when wired) have **no direct conversational surface to the user** — they receive a prompt, run, return a result. To pass messages back to the user mid-flight (or signal status, completion, or blockers), they emit structured **marker lines** in their output; the controller polls / parses these and relays via the orchestrator's conversational surface. Same idea as `/fork` markers (§Self-delegation) generalized for all worker types.

**Marker vocabulary** (one marker per line in the worker's output, so a grep at completion catches each):

| Marker | Semantics | Controller action |
|---|---|---|
| `STATUS: <update>` | Informational progress. | Log internally. Surface to user only if they asked for live commentary. |
| `RESULT: <key>=<value>` | Structured output the controller should extract. | Capture for downstream use (Progress table, premises file, RESUME-NOTES). |
| `USER-NEED: <question>` | Worker stopped because it can't decide without user input — the inline-office-hours premise-check moment surfaced from inside the worker. | Relay the question to the user via the orchestrator's conversational surface. Wait for the user's reply (next user turn, or several turns later). Re-dispatch the worker with the answer prepended as `USER-CLARIFICATION: <answer>`. |
| `USER-CONFIRM: <action> [Y/N]` | Worker requests explicit authorization for an irreversible operation (destructive file edit, mass commit, deploy). | Relay; wait for explicit yes/no; re-dispatch with the answer; if no, mark chunk BLOCKED and surface to user. |
| `BLOCKED: <reason>` | Worker hit something unrecoverable. | Surface to user via orchestrator. Don't auto-retry. Mark chunk BLOCKED in the Progress table. |
| `COMPLETE: <summary>` | Worker finished its assigned task. | Verify diff briefly (per §Per-chunk loop step 3), commit, continue. |

**Controller→worker marker** (one direction-reversed marker, prepended on re-dispatch — included here so workers reading the dispatch wrapper recognize it):

| Marker | Direction | Semantics | Worker action |
|---|---|---|---|
| `USER-CLARIFICATION: <answer>` | controller → worker | The user's reply to a prior `USER-NEED:` / `USER-CONFIRM:`. Prepended to the chunk body on re-dispatch. | Read the clarification; resume from where you stopped; do NOT re-ask the same question. |

**Polling shape — Bash `&` + tail-file workers** (codex `exec`, `grok -p` / `grok --prompt-file`, anything launched via Bash `&` with output redirected to a tail file): the canonical pattern is `scripts/watch-dispatch-tail.sh` (added 0.3.1). It backgrounds alongside the worker, polls both the PID liveness AND the tail file, and exits with structured codes (0=terminal marker / 1=pid-dead / 2=idle-timeout / 3=controller-dead) plus a `WATCHER-EXIT: <kind>` summary line. The watcher also registers a per-watcher pidfile entry at `/tmp/goal-flight-acp-pids.d/<controller-pid>.bashtail.<worker-pid>.jsonl` so `cleanup_ghosts()` reaps orphaned workers across both ACP and Bash-tail paths uniformly. The content-aware grep handles the marker vocabulary's emphasis-tolerant form (`^\**(STATUS|RESULT|USER-NEED|USER-CONFIRM|BLOCKED|COMPLETE):\**` matches both codex's plain `STATUS:` and grok's markdown-bold `**STATUS:**`). After the watcher fires its task-notification, the controller reads the tail and processes any non-terminal markers (STATUS / RESULT) for logging; the watcher's exit code already classified the terminal state.

**Polling shape — Agent `run_in_background` workers**: the Agent tool returns a final-text blob via the harness's completion notification. The controller scans the blob for marker lines the same way. Multi-step Agent work can emit several `STATUS:` lines plus a final `COMPLETE:` / `USER-NEED:` / `BLOCKED:`.

**Polling shape — ACP workers** (when wired via OpenAB or similar broker): ACP defines structured message types (`user_input_required`, `progress`, `final_response`). Markers map onto those types; the controller routes via the broker. Same vocabulary, different transport — the marker prose in the dispatch wrapper still applies because the worker's *prompt* is the same.

**Re-dispatch shape for `USER-NEED`**: when the worker emitted `USER-NEED: <question>` and the user has answered, the controller re-dispatches the same chunk's wrapper with a `USER-CLARIFICATION: <user's answer>` line prepended to the chunk body. The worker reads the clarification, resumes from where it stopped, and is instructed (per the dispatch wrapper) to not re-ask the same question. For ACP / persistent-session workers: send the answer through the existing session rather than re-dispatching.

**Worker prompt contract**: the dispatch wrapper (`prompts/dispatch-wrapper.md` Layer 6) and the goal-mode prompt template (`templates/codex-goal-prompt.md.tpl`) instruct executors about this vocabulary so they emit the right strings when they hit an ambiguous point. Without explicit instruction, executors often just guess and proceed — the markers exist so the controller can *prevent* that. The cost of the convention is one line of prose in each dispatched prompt; the win is the controller becomes a structured relay rather than a guess-the-state poller.

### Worktree convention

Controller works in the main worktree. Parallel-safe chunks each get `<repo>/.claude/worktrees/<adjective-noun>/` on a `claude/<adjective-noun>` branch. Codex trust prefix-matches the project root, so worktrees inherit automatically — no per-worktree registration. Cherry-pick onto main; do NOT use `git merge --ff-only` (sibling worktrees branched off main don't fast-forward cleanly when other worktrees committed since).

### Per-chunk loop (canonical shape)

**Dispatch rule**: any tool call expected to take more than ~10 seconds runs in background — so the user's terminal doesn't hang, allowing them to steer.

Two background mechanisms with different completion-notification properties:

- **Agent tool** with `run_in_background: true` (also Skill() invocations the harness backgrounds): the harness sends a task-notification when the subagent completes; the controller resumes as a new turn automatically. No watcher needed.
- **Bash `&` for headless dispatch** (`codex exec ... &`, `grok -p ... &`, `npx ... &`): the launcher Bash call exits **immediately after spawning the child**. The harness sends a task-notification for the launcher's exit, NOT for the child's eventual exit. To get a notification when the child actually completes, the controller must **wire a watcher**:

```bash
# 1. Launch the child, capture PID.
codex exec -C <workdir> --dangerously-bypass-approvals-and-sandbox '<prompt>' \
  > /tmp/codex-<slug>.txt 2>&1 &
PID=$!
echo "Dispatched codex PID=$PID -> /tmp/codex-<slug>.txt"

# 2. Background the watcher (content-aware completion + idle/wedge detection +
#    pidfile registration for cleanup_ghosts orphan defense). Then dispatch a
#    `wait $WATCHER_PID; echo "exit=$?"; cat /tmp/watcher-<slug>.txt` Bash call
#    with run_in_background: true to surface the watcher's exit code.
bash <skill-root>/scripts/watch-dispatch-tail.sh \
  --pid $PID --tail /tmp/codex-<slug>.txt --controller-pid $$ \
  --agent codex-bash-tail --session-id <slug> \
  > /tmp/watcher-<slug>.txt 2>&1 &
WATCHER_PID=$!
```

The watcher's Bash call is what produces the eventual task-notification (it exits when the child PID is gone). Without the watcher, the controller's turn ends after step 1 and **no callback ever arrives for the child's completion** — the dispatch silently runs to ground and the controller looks "hung" between chunks.

Per-chunk loop:

1. **Dispatch in background.** Agent: `run_in_background: true`. Bash codex/grok: `&` + redirect, capture PID, dispatch watcher with `run_in_background: true`.
2. **Emit one-line status, end the turn.** "Dispatching chunk #N (`<slug>`). Agent task `<id>` / shell PID `<pid>` -> `<output-path>`." Do not chain chunk N+1's dispatch within this turn. Optional: append one premise-check from the inline-office-hours backlog (`[Premise check] <question>`) — non-blocking; see §Inline office-hours.
3. **On completion turn** (triggered by task-notification — from the Agent's completion, or from the watcher exiting when the Bash child exited): **verify the diff briefly** — scope contained, suite green, no leaked invariants. Executor's self-review already caught issues; you sanity-check. For Bash dispatches, `cat` / `ctx_search` the output file captured in step 1.
4. **Commit** (one chunk = one commit). Parallel-mode: cherry-pick onto main.
5. **Update the Progress table** in goal-queue + your visible state.
6. **Look-ahead** — fire-and-forget Explore subagent (also background) reading the next 1–2 chunks for hidden dependencies or missing acceptance criteria.
7. **Dispatch chunk N+1 in background, end the turn** (back to step 1).

Exception: `[goal-mode]` loops own their own turn-boundaries (codex `/goal` runs multi-hour internally; external Opus/Grok loops tail-poll). In goal-mode the controller surfaces turns at iteration-cap or convergence only, not per-iteration. The watcher pattern still applies for the external-loop tail-poll case (each iteration is a separate Bash `&` + watcher pair).

### Progress table — keep this current

```
Chunk                       Status            Commit
1. <SLUG>                   ✅                <hash>
2. <SLUG>                   ✅                <hash>
3. <SLUG> (current)         🟡 in flight      —
#4 / #5 / #6                queued            —
#7                          post-<gate>       —

<branch> @ <head>, <N> green.
```

Status legend: ✅ done · 🟡 in flight · queued · blocked · post-`<gate>`.

### Three subagent types

| Type | Purpose | Source for the dispatched prompt | Reports |
|------|---------|----------------------------------|---------|
| **Executor** | Implement a `/goal` chunk; writes code, runs tests, commits | Controller composes per `prompts/dispatch-wrapper.md` (all 6 layers + optional RAG slices) | `git diff --stat`, P0/P1/P2/P3 findings, tests run, surprises |
| **Reviewer** | Read-only adversarial pass over a commit range or draft | Loaded as-is from `prompts/gstack-claude-review.md`, `prompts/gstack-codex-challenge.md`, or `prompts/decomposition-review.md` — these are reduced-layer shapes (effectively Layers 1+3, no Layer 0/2/5). Or invoked via gstack `/review` when registered (preferred). | Findings list with file:line refs, P0/P1/P2/P3, confidence |
| **Planner** | Write a plan document to a pinned path; "NO code changes" | Loaded as-is from `prompts/dual-plan-adversarial.md` (when dual-lens) or composed inline (Layers 1+3 + pinned deliverable path) | File path, word count, bottom-line recommendation, open questions |

The shapes for reviewer + planner live in their respective prompt files, NOT composed per-dispatch by the controller. The controller's job for these types is to substitute the few placeholders (`<range>`, `<paths>`, `<deliverable path>`) and dispatch; the full wrapper is in the file. Reviewer/planner dispatches that mistakenly receive the executor wrapper (all 6 layers) waste context and encourage drift into code-writing — keep them on the reduced shapes.

Dispatch mode is orthogonal to type — see §Per-chunk loop dispatch rule: background if expected to exceed ~10s. Most goal-flight reviewer / planner dispatches qualify (gstack `/review` typically takes 30s–3min).

### Don't

- Run a separate reviewer subagent per chunk — the embedded self-review is the cheaper substitute. Reserve full multi-agent review for milestones.
- Bundle multiple chunks in one commit.
- Refactor outside the chunk's SCOPE mid-execution. File a follow-up chunk.
- Skip the diff verification because self-review reported clean.
- Use `git merge --ff-only` to integrate parallel-worktree subagent commits. Cherry-pick.
- Hand-write dispatch wrappers when the RAG corpus exists. Source layers 2/3/4 from the corpus with verification framing.
- Poll an Agent-tool subagent's transcript or its `<output>` JSONL. Agent dispatches deliver a task-notification on completion; wait for it. (This is NOT a ban on `kill -0 $PID` watching a Bash-spawned child like `codex exec ... &` or `grok -p ... &` — those need an explicit watcher to produce a notification at all; see §Per-chunk loop dispatch rule.)

### Don't (cont.) — date format + notifications + worker-context

- **Date format**: `YYYY-MM-DD` from the conversation's `currentDate`. Same-day RESUME-NOTES bump `(rev N)` in H1.
- **Notifications** via `osascript -e 'display notification "X" with title "goal-flight"'` ONLY on blockers and queue completion. Session scrollback is the primary monitor. Forensics live in the harness session JSONL + per-subagent JSONL + codex tail files.
- **Worker context is optional**. Default: dispatches point executors at `AGENTS.md` directly. Create `docs-private/worker-context.md` only if AGENTS.md is huge (>1000 lines) or the project has distinct worker profiles.
- **High-level goal pinned at init** to `docs-private/goal-<topic>-<today>.md`. Controller cites it in decompose-plan, mid-execute decisions, milestone summaries. (Legacy file naming `<topic>-goal-statement-<date>.md` from < 0.3.0 is also accepted on read.)

## Resume / goal sub-commands (handled inline)

### `resume`
1. Find the most recent `docs-private/RESUME-NOTES-*.md`. If none, bail: "Run `/goal-flight init <topic>` first." If the file carries a `Skill-loaded:` header, compare it to the live `LOADED_LINE` (per §Session pre-flight probe 1 recipe) and surface the drift nudge from probe 4 if they differ.
2. Find the most recent goal-queue: `docs-private/goal-queue-*.md` (new naming) or `docs-private/*-goal-queue-*.md` (legacy < 0.3.0). Same `Skill-loaded:` comparison if the header is present. Also read `docs-private/premises-<topic>-<date>.md` if present (or legacy fallback) — propagates Validated premises into the rebuilt RESUME-NOTES.
3. Read git state via Bash: `git rev-parse HEAD`, `git rev-parse --abbrev-ref HEAD`, `git log --oneline -20`.
4. Spawn a subagent to write `docs-private/RESUME-NOTES-<today>.md`. If today's file exists, increment `(rev N)` in H1 instead of overwriting. RESUME-NOTES contains: TL;DR (one paragraph), Progress table verbatim, in-flight executor IDs/PIDs, reading order on wake (numbered file list), first-5-minutes (exact next steps). New writes include the current `Skill-loaded:` header (per `commands/init.md` step 3 shape).
5. Print: file path; placeholders the user still needs to fill in.

### `goal <SLUG>`
1. Find the most recent goal-queue.
2. Find the highest existing `## N.` goal number.
3. Append: `## <N+1>. /goal <SLUG>` with SCOPE / CHECKLIST / ACCEPTANCE / FORBIDDEN skeleton; tags `[parallel-safe:<group>]` `[milestone]` `[controller-direct]` `[goal-mode]` `[max-iterations:<N>]` `[mixed-executor]` as applicable.
4. Append a row to the Progress table: `| #<N+1> \`<SLUG>\` | TODO |`.
5. Print the appended block.
