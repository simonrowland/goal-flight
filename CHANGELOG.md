# Changelog

Notable changes to the goal-flight Claude Code skill. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are
incremented when meaningful skill behaviour changes.

## [Unreleased]

### Added
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
