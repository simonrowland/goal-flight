# Changelog

Notable changes to the goal-flight Claude Code skill. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are
incremented when meaningful skill behaviour changes.

## [Unreleased]

### Added
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
