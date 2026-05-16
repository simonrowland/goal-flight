# Changelog

Notable changes to the goal-flight Claude Code skill. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are
incremented when meaningful skill behaviour changes.

## [0.3.0] — 2026-05-16

Post-convergence UX-friction batch + lessons from a parallel session using
the skill. Three substantive commits on top of the 0.2.0 convergence stack
at `1ade7fd`, plus a grok-sweep fix-up that dropped an ungrounded review-
channel claim and tightened prose. Three parallel grok-build review sweeps
(broad correctness / adversarial / prose) drove the fix-up.

### Added
- **Init env summary surfaces Claude Code + context-mode versions** plus
  the primary self-delegation slash form (`/fork` vs `/branch`, derived
  from `claude --version` against the 2.1.77 rename pin). Helps first-time
  users see which CLI version + slash form their session runs against, and
  lets RESUME-NOTES forensics pin behaviour to a CLI version (Claude Code
  does not version-stamp session JSONLs). `commands/init.md` step 1
  (probes) + step 6 (summary bullets). Source: round-4 grok forward-
  looking items A + E. Commit `f54772f`.
- **Layer 0 capture-timing rule** in `prompts/dispatch-wrapper.md`:
  capture expected base SHA AFTER any pre-dispatch admin commits (goal-
  queue Progress-table updates, RESUME-NOTES rev bumps, .gitignore
  additions) and BEFORE composing the dispatch prompt. Pre-admin-commit
  capture lets Layer 0 correctly reject; the fix is capture order, not
  Layer 0 lenience. Codex correctly refused such drift in the field — the
  gate worked as designed. Commit `f6bd2c5`, prose-tightened in `0e94432`.
- **Codex `/goal` mode pre-install dependencies** bullet in SKILL.md
  §Codex reliability. Multi-hour `/goal` loops + mid-iteration
  `pip install` / `npm install` / `uv sync` is a real friction class:
  surface-installs wedge on network or leave half-installed venvs the
  next iteration trips over. Resolve the dependency surface up-front.
  Commit `f6bd2c5`.

### Changed
- **README Quickstart now flags the DRAFT-goal gate** so first-time users
  aren't blindsided when `decompose-plan` refuses on a fuzzy goal. The
  refusal in `commands/decompose-plan.md` step 0 cites the resolved
  absolute path of the goal-statement file and the exact `Status:` line
  to flip. Source: UX-review Friction #2. Commit `7c03d35`.
- **SKILL.md Dispatch model section** restructured: the prior single
  "Token bias is a dial" bullet (which had become a multi-paragraph
  decay of stale future-work claims) is now two focused bullets — token
  bias (defaults UP, override per-chunk) and channel routing (user
  override on top of the controller's per-chunk three-paths default,
  reserving Claude for orchestration and milestone reviews via gstack
  `/review`, codex for coding when Claude session-limits bite, grok as
  an executor). Drops the dead `docs-private/<topic>-tuning.md` reader
  claim that no code path consumed, AND drops a transient claim about
  `grok -p` as a parallel-review channel — grok stays as an executor;
  wiring grok-p as a review-channel target is forward work. Commits
  `f6bd2c5` (initial collapse) and `0e94432` (split + grok-channel
  correction).

### Internal
- **Grok sweep validates the pattern.** Three parallel `grok-build`
  reviews against a small post-convergence diff converged on
  CONVERGED / HOLD-one-issue / PROSE-DRIFT respectively; the adversarial
  lens caught the grok-p review-channel claim that the broad and
  consolidated reviews missed. Working invocation pattern documented in
  `docs-private/grok-shell-pattern.md` (skill-private — not on origin):
  `grok --prompt-file <path> --output-format plain` with the diff
  embedded in the prompt; drop `--max-turns` (per-message cap surfaces
  faster than reasoning) and `--effort` (grok-build rejects the
  `reasoningEffort` parameter).

### Sources
- `docs-private/review-r4-grok-thorough-2026-05-15.txt` (round-4
  forward-looking items A + E)
- `docs-private/ux-review-grok-build-2026-05-15.txt` (Friction #2 + part
  of Friction #4)
- Lessons captured from a parallel session using the skill (Layer 0
  timing, /goal pre-install, token-bias gist)
- Three parallel grok-build review sweeps (broad / adversarial / prose) —
  outputs at `/tmp/sweep-out-{A,B,C}.txt` at tag time

### Tests
3 suites / 46 assertions remain green throughout.

## [Unreleased]

### **STRIP REFACTOR — skill collapsed from ~230 KB to ~30 KB**

Three-commit aggressive cull (`d67c80c` + `afcff37` + this one) following parallel claude + codex reviews of the prior state. Reviewers surfaced cross-file drift (P0), validate-dispatch shallow heuristics + verification-first conflict (P1/P2), and an install-script path-trust vulnerability (P0). Plus a user-level realization that frontier models don't need per-slice templates or pre-pasted wrapper examples to do good work; the templates were calcifying around one project's idioms and over-prescribing for others.

**Deleted (~2000 lines stripped across the strip):**
- 6 rag-slice templates (`templates/rag-slice-*.md.tpl`).
- 4 init-time templates (`AGENTS.md.tpl`, `RESUME-NOTES.tpl`, `goal-statement.md.tpl`, `worker-context.md.tpl`) — inlined as 5–15 line shapes in `commands/init.md`.
- `templates/goal-queue.tpl` — inlined as compact shape in `commands/decompose-plan.md` step 3.
- 4 RAG-pipeline prompt files (`rag-slice-builder.md`, `rag-slice-review.md`, `rag-cross-slice-consolidation.md`, `rag-final-assessment.md`) — collapsed into 4 short pass briefs in `commands/build-corpus.md`.
- `reference/pattern.md` — folded into `SKILL.md` (now the canonical gist; `/goal-flight` no-args prints it).
- `prompts/dispatch-wrapper.md` — stripped from 15 KB of per-layer worked examples to ~5 KB of verification-first principle + Layer 0 spec + principle table for layers 1–5. Examples calcified; the principle generalizes.

**Rewrites:**
- `SKILL.md` — now beefier (folded in `pattern.md`'s Codex reliability, /goal mode, Handoff before compact, state-three-layers, three-dispatch-paths, three-subagent-types, Don'ts).
- `commands/validate-dispatch.md` — aligned with verification-first wrapper (was telling controllers to "paste these slices" while wrapper said "point at them"). Heuristics tightened: catches `:line` anchors without verification framing in same paragraph, catches stale-`git fetch` as P0 blocker, catches Layer 5 specialization in prompt (was inverted before).
- `commands/build-corpus.md` and `commands/init.md` step 3.5 — RAG pipeline expressed as 4 short pass briefs instead of per-pass prompt-file references.
- `README.md` — stripped from 16 KB to ~6 KB. Cut the 12-knob parameter-space table and 5 example tunings; both were one-project-specific calcification. Kept the Quickstart, sub-command table, Why-the-pattern-works gist, Adapting-via-agent-edit paragraph, When-NOT-to-use list.

**Codex reviewer P0 fix (`scripts/install-codex-overrides.sh`):**
- Added path-guard rejecting `/`, `$HOME` exactly, and single-segment paths under root (`/usr`, `/tmp`, `/etc`, etc.). Prior version accepted `/` and wrote `[projects."/"] trust_level = "trusted"` — effectively trusting every cwd via prefix-match. Verified guards reject all four cases and pass a legitimate deep path through.
- Bonus: warns (but doesn't block) if the target isn't a git repo. Most legitimate codex-trusted projects are git repos; a missing `.git/` is usually a sign of a mistake but legitimate cases exist (research dirs).

**Codex reviewer P1/P2 fixes:**
- `prompts/dispatch-wrapper.md`: controller-side worktree-base verify now documented as a belt-and-braces alongside prompt-side Layer 0 (`git -C <worktree> rev-parse HEAD == expected` before dispatch). Honor-system Layer 0 alone is too weak.
- `commands/validate-dispatch.md` + `prompts/dispatch-wrapper.md` Layer 0: expected SHA captured via `git fetch origin && git rev-parse origin/main` from the MAIN worktree, not local `main` alone. Local can be stale.

**Remaining files** (load-bearing, kept):
- `templates/codex-goal-prompt.md.tpl` — /goal mode prompt skeleton (Objective / Workspace / Rules / Acceptance / Test gates / Blocker protocol / Edit policy / Final response schema). Non-prescriptive shape that activates codex /goal mode non-interactively + serves as the goal-prompt for Opus/Grok iteration loops.
- `templates/rag-corpus-schema.md.tpl` — corpus directory shape + per-slice word budgets + verified-at frontmatter convention.
- 8 prompts (`ask-anticipatory.md`, `decomposition-review.md`, `dispatch-wrapper.md`, `dual-plan-adversarial.md`, `executor-self-review.md`, `gstack-claude-review.md`, `gstack-codex-challenge.md`, `repo-audit.md`).
- 8 commands.
- `scripts/install-codex-overrides.sh` (hardened).
- `tests/` (1 test file, 8 assertions, still green).

Frontier-model composition guarantee: the skill no longer carries worked examples of dispatch prompts, per-slice content shapes, or template scaffolding the agent could compose itself from a brief description. What remains is principle + load-bearing shapes + executable scripts.

### Added
- **`scripts/self-fork-detect.sh` + self-delegation-via-fork pattern.**
  `/fork` (Claude Code slash command, also `--fork-session` CLI flag)
  creates a new session with a fresh `CLAUDE_CODE_SESSION_ID`. The
  helper script lets the controller write a contract (controller's
  session ID + task description + completion/abort signals) before
  forking; the new session's `detect` mode prints `ORIGINAL | FORK |
  SUBAGENT | NO_CONTRACT` by comparing env var to contract. On FORK,
  the task + signals are printed for the fork to act on.

  Empirically verified (May 2026):
  - `claude --resume <sid> --fork-session` creates a new top-level
    JSONL with a new session ID (`4be591f6-…` from parent `05752a67-…`
    in the verification probe).
  - Agent-tool subagents INHERIT the parent's
    `CLAUDE_CODE_SESSION_ID` (their JSONL lives at `<proj>/<sid>/
    subagents/agent-<hash>.jsonl`, nested under the parent). The
    `detect` script's heuristic (recent activity under any `subagents/`
    subdir + env-matches-marker) reports SUBAGENT, not ORIGINAL,
    so a subagent that incidentally reads the contract doesn't
    misfire as the controller.

  `SKILL.md` gains a §"Self-delegation via /fork" subsection with
  the identity-surface table + decision guide (controller-direct vs
  Agent-tool subagent vs /fork — different trade-offs).
  `tests/test-self-fork-detect.sh` covers the marker roundtrip and
  the synthetic-mismatch FORK case (the actual /fork path requires
  user interaction; the test exercises everything that can be
  exercised non-interactively).
- **Codex `/goal` mode integrated as a peer dispatch shape.** Codex CLI's
  experimental `/goal` slash command (gated behind `features.goals = true`
  in `~/.codex/config.toml`, requires codex ≥ 0.128.0) runs a non-
  interactive plan/act/test/iterate loop when fed a goal-shaped prompt via
  stdin. Activation: `codex exec -C <workdir> - < prompt.md`. New
  `templates/codex-goal-prompt.md.tpl` ships the canonical prompt
  skeleton (Objective / Workspace / Rules / Acceptance criteria / Test
  gates / Blocker protocol / Edit policy / Final response schema).
  `reference/pattern.md` §Codex `/goal` mode dispatch shape documents
  the full pattern including: why no `timeout 300` wrapper (`/goal` is
  multi-hour by design), monitoring via tail-polling for the Final
  response schema rather than activity-based stall watchdog, and a
  decision table for when to use `/goal` mode (chunk execution with
  loop primitive) vs the short-prompt codex shape (bounded review
  tasks).
- **Opus iteration loop as a no-codex fallback for `/goal`-mode chunks.**
  Same goal-prompt template; the controller becomes the loop primitive
  externally. Each Agent dispatch is one iteration; the controller
  parses the Final response block, captures git-diff state +
  Agent-reported blockers + tests pass/fail, and either commits
  (Goal complete: true) or re-dispatches with the unchanged goal-
  prompt + an updated "Iteration N of MAX, Prior progress: ..."
  preamble. Iteration cap defaults to 5–8 (configurable via
  `[max-iterations:<N>]` chunk tag). Documented as a §subsection
  inside Codex `/goal` mode dispatch shape; reuses the same
  `templates/codex-goal-prompt.md.tpl`. Strictly slower than codex
  `/goal` per-iteration but zero-setup; useful when codex isn't
  installed or `features.goals` isn't enabled, AND when the chunk
  typically completes in 1–2 iterations (overhead difference is
  negligible at that scale). Each iteration's transcript is
  readable via the task-notification's JSONL path; controller
  parses the last assistant message before the `done` event for the
  Final response block.
- **Grok iteration loop as a peer fallback to Opus iteration.** Same
  controller-as-loop pattern but dispatch surface is `grok -p
  --output-format json --model grok-build --disable-slash-commands
  < prompt.md > response.json 2> stderr.log &` — shell tool,
  file-backed, structured JSON output, tail-friendly. Pre-requirement
  detected in `commands/init.md` step 1 (`command -v grok`). Reuses
  the same `templates/codex-goal-prompt.md.tpl`. Useful when you
  want model diversity in iteration (Grok's blind spots differ from
  Opus's), when Grok-account billing is cheaper than Claude session
  billing for the workload, or when codex isn't set up but Grok is.
  `reference/pattern.md` adds a decision matrix for Opus vs Grok
  iteration covering dispatch surface, observability, model
  blind-spots, setup cost, and compaction risk. Mixed-executor
  iterations across a single chunk (e.g. iter 1 Opus, iter 2 Grok)
  are valid for stuck-loop recovery; tag the chunk
  `[mixed-executor]` in the goal-queue for RESUME-NOTES
  forensics.
- **Init step 1 now gates codex on `/goal` mode minimum (0.128.0) and
  `features.goals` enable-state.** Recommends `codex update` if older;
  recommends `codex features enable goals` if disabled. Both are
  opt-in prompts — user's environment, user's call.
- `[controller-direct]` chunk tag — `commands/decompose-plan.md` step 2 now
  tags trivial single-file chunks (< ~30 LoC, no cross-module coupling) so
  the controller can handle them inline with Read + Edit + commit instead
  of dispatching a subagent. `commands/execute.md` step 2b branches on the
  tag. Closes the dispatch-overpresribe gap for tiny chunks where subagent
  dispatch costs more than the work itself.
- **`[controller-direct]` criterion expanded with "too much context to
  explain" trigger.** Two distinct cases now justify inline execution:
  (A) trivially small work — the original criterion (single-file,
  <30 LoC, no cross-module coupling); (B) the controller has
  session-loaded state (mid-debug, just-consumed milestone-review
  P0 cluster, rolling decisions not yet in `docs-private/rag/
  decisions.md`) that re-explaining to a fresh subagent would cost
  more than doing the work. Heuristic for (B): a clean dispatch
  wrapper would exceed ~5 KB primarily because of session-loaded
  context. Conservative bias on both — when unsure, don't tag,
  let the default subagent path handle it. `commands/execute.md`
  step 2b also notes the codex-side analog: `codex fork --last
  <continuation>` or `codex exec resume --last '<followup>'` for
  inheriting codex's prior session state, same overhead-arbitrage
  logic on a different dispatch surface.
- `reference/pattern.md` §Handoff before compact gains a "Three layers of
  state" subsection making the RESUME-NOTES / goal-queue Progress table /
  TodoWrite split explicit. RESUME-NOTES = cross-session prose, goal-queue
  Progress = cross-session chunk state, TodoWrite = in-session tactical
  sub-steps.

### Changed
- `SKILL.md` controller-delegates-reads bullet softened: bulk reads
  (>200 lines, full READMEs, full architecture docs) still go to Explore
  subagents; short verification reads inline are fine. The ban is on bulk
  consumption, not on the controller using its eyes.
- **Handoff threshold raised 70% → 80% with explicit calibration.**
  `reference/pattern.md` §Handoff before compact now treats the percentage
  as a default rather than a hard rule. The right handoff time is a
  function of (remaining work in the queue) × (cost of waking afresh
  with summaries). Conserve harder mid-complex-chunk-debug or with
  multiple in-flight subagents whose notifications carry state; run
  hotter (90%+) when the queue is 1-3 trivial chunks from done and
  the most recent RESUME-NOTES rev already captures in-flight state.
  Explicit note that subagents + `\goal` mode are the primary leverage
  for extending session life — the controller's own context mostly
  holds metadata, not the bottleneck.
- `templates/goal-queue.tpl` independence-tags section now lists
  `[controller-direct]` alongside `[parallel-safe:<group>]` and `[milestone]`.
- **Agent roles framing made explicit in init step 1.** Codex is a
  dispatch target (executor / reviewer) — never expected to invoke
  `/goal-flight <sub>` itself. Controller is Claude Code today; Hermes
  is the future candidate. The clarification removes a footgun around
  `\goal` (in-prompt text marker, backslash) vs `/goal-flight goal
  <SLUG>` (slash command, controller-side queue helper) — there is no
  `/goal` codex command in v0.130.0 or any current marketplace.
- **`commands/init.md` step 1 now captures `codex --version` in the
  summary** and surfaces a `codex update` recommendation when an older
  version is installed than the latest published `@openai/codex`. Does
  not auto-update — user's call. Notes the minimum-tested version
  (`codex-cli 0.130.0` as of v0.2.x). RESUME-NOTES forensics benefit
  from having the version recorded since codex CLI behaviour shifts
  between versions.
- **Codex dispatch shape: pointers, not pre-pasted content.** `reference/
  pattern.md` §Codex reliability and three dispatch sites in
  `commands/{execute,decompose-plan,init}.md` rewritten to hand codex
  short prompts that point at files on disk (e.g. `Read prompts/
  gstack-codex-challenge.md in full and execute it`) rather than pasting
  the prompt file's contents into the codex exec arg. Solves three
  coupled problems at once: (1) controller burns its own tokens
  composing 6–11 KB of context per dispatch when the agent could just
  Read; (2) controller-pasted "facts" go stale on the timescale of
  minutes between composition and execution; (3) codex session
  compaction clobbers the unparaphrased original — pointer-based
  dispatch lets codex re-Read on compaction. Aligns the codex side
  with `prompts/dispatch-wrapper.md`'s verification-first principle
  for Claude Agent dispatches.

## [0.2.0] — 2026-05-15

### Added
- `scripts/install-codex-overrides.sh` — idempotent installer that registers
  a project as codex-trusted in `~/.codex/config.toml`. Bypasses the MCP
  approval-gate stall that broke ~2/5 non-interactive `codex exec` dispatches
  in the original release.
- `/goal-flight register-codex [<path>]` sub-command — thin wrapper around
  the install script for repeat invocations after the initial init.
- `/goal-flight validate-dispatch [<goal-slug>]` sub-command — renders the
  5-layer dispatch wrapper for a goal without dispatching it. Catches
  malformed layers before burning an Opus subagent dispatch.
- `/goal-flight validate-queue [<queue-file>]` sub-command — schema-checks
  a goal-queue: every chunk has SCOPE / CHECKLIST / ACCEPTANCE / FORBIDDEN;
  numbering is sequential; `[parallel-safe:<group>]` tags reference defined
  groups; no duplicate slugs.
- `commands/execute.md` parallel-mode now includes a cherry-pick conflict
  handling recipe at step 3c — re-dispatch with current main HEAD as Layer 0
  base SHA, or mark `[REBASE-NEEDED:<reason>]` and continue the batch.
- `tests/` directory with a bash test harness for `install-codex-overrides.sh`
  (sandbox-`HOME` based — never touches the real `~/.codex/config.toml`).
- `README.md` Quickstart section.
- `CHANGELOG.md` and `VERSION` files.

### Changed
- `reference/pattern.md` §Codex reliability rewritten. Primary fix is now the
  project-trust sidecar (`install-codex-overrides.sh`); `--ignore-user-config`
  demoted to documented fallback. Detection thresholds (zero-output ≥ 90 s,
  no-progress ≥ 180 s, hard-timeout 300 s) are numeric and data-derived; the
  earlier "> 2× expected window" prescription is gone.
- Every codex dispatch site in `commands/{execute,decompose-plan,init}.md`
  and `SKILL.md` now uses `timeout --kill-after=10 300 codex exec '...'`
  (no `--ignore-user-config`). Codex dispatches retain MCP tool access.
- `commands/execute.md` step 3a — explicit note that worktrees inherit codex
  trust by path prefix; no per-worktree registration needed.

### Fixed
- Codex `exec` silent-stall failure mode (zero-byte tail file, PID alive,
  ~0% CPU). Root cause: `~/.codex/config.toml` `[mcp_servers.X.tools.Y]
  approval_mode = "approve"` blocking non-interactive dispatches with no
  TTY surface for the approval prompt. Resolved by project-trust
  registration; documented in `docs-private/codex-stall-investigation-
  2026-05-15.md` (gitignored).

## [0.1.0] — 2026-05-14

Initial release. Controller pattern, dispatch wrapper layers, milestone
gstack reviews, RAG corpus pipeline, RESUME-NOTES handoff. See
`docs-private/lessons-learned-2026-05-15.md` (gitignored) for the harden
session that motivated 0.2.
