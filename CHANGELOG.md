# Changelog

Notable changes to the goal-flight Claude Code skill. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are
incremented when meaningful skill behaviour changes.

## [0.4.6] — 2026-05-21

**Runner liveness: terminal-state precedence fix + hermetic regression coverage.**
Closes the runner-level test-coverage debt acknowledged in the 0.4.5 SDK-migration
review, and fixes a terminal-classification edge case found while writing it.

### Fixed

- A genuine `end_turn` now beats a SILENCE-class heartbeat terminal (dead-sample
  wedge / `progress_stall` / `max_quiet_s` — all reported as `wedged`, all gated on
  `outstanding_count == 0`) that trips in the brief alive-and-silent tail after a
  turn completes. Previously the heartbeat verdict was checked unconditionally, so
  on an aggressive heartbeat cadence a completed turn could be mislabeled `wedged`.
  `tool_timeout` (a tool left outstanding past `--max-tool-s`) is exempt and still
  wins — `end_turn` does not refute a dangling-tool anomaly. Classification is now a
  pure, unit-tested helper (`decide_terminal_state`), and the success-path status
  record reconciles `killed_by_heartbeat` / `wedged_by_heartbeat` with the final
  state so a tail-race `complete` is never self-contradictory.

### Added

- Hermetic fake-agent regression tests for the runner paths that lost deterministic
  coverage in the SDK migration: idle-timeout / `IdleLivenessGate` firing,
  `spawn_and_handshake_with_retry` kill-before-respawn on a handshake wedge,
  `AcpProcessPool` exhaustion + drain, oversized-frame drop-and-continue, the
  goal-mode (`idle-timeout=0`) progress-stall and heartbeat backstops, per-tool
  `tool_timeout` reaping, and the `decide_terminal_state` precedence lattice.
- Fake-agent scenarios: `idle_silent`, `handshake_wedge`, `tool_stuck`.

## [0.4.5] — 2026-05-20

**ACP SDK migration.** Replaced the bespoke stdio JSON-RPC transport with the
official `agent-client-protocol==0.10.*` Python SDK while keeping goal-flight's
process-group isolation, pidfile ghost cleanup, StartupGate, heartbeat wedge
detector, one-shot mode, and goal mode.

### Added

- SDK wrapper with hand-spawned `start_new_session=True` workers, verified pgid
  kill/reap, guarded over-limit frame dropping, raw stream observer liveness
  counters, and typed permission auto-allow.
- Hermetic fake-ACP tests for vendor-flood wedge behavior, CPU-busy flood
  survival, over-limit drop-and-continue, permission auto-allow, tool tracking,
  and real-time marker cancellation.
- Live smoke script for maintainers:
  `~/.goal-flight/venvs/acp-0.10/bin/python test/smoke_acp_sdk_live.py`.

### Changed

- Runner now drives SDK `initialize -> new_session(cwd, mcp_servers=[]) ->
  prompt` with streamed marker detection before prompt resolution.
- Heartbeat wedge samples now key off `wedge_progress_seen`, while
  `raw_events_seen` remains the idle keepalive counter.
- Oversized ACP frames are drop-and-continue, not terminal kills: the guarded
  reader logs and counts dropped frames, oversized notifications are skipped, and
  oversized responses degrade through the existing idle/timeout failure path.
- `/goal-flight init`, doctor, `requirements.txt`, and `tests/run.sh` know about
  the isolated ACP SDK venv.

### Fixed

Found and fixed via a live mass-spawn stress harness (all four workers validated,
zero orphan leaks):

- **SDK venv re-exec** now execs the *unresolved* venv `python` symlink. Resolving
  it first collapsed the symlink to the bare interpreter outside the venv, so the
  re-exec landed somewhere without `acp` ("SDK missing"); the loop-guard still uses
  the resolved path.
- **Worker kill no longer leaks wrapper-script adapters.** The PID-reuse identity
  guard compared process start-time *and* `comm`; a launcher that `exec`s its real
  binary (`cursor-agent`, `claude-code-cli-acp`) changes `comm` at the same PID, so
  the guard skipped `SIGKILL` and the worker leaked as an orphan (pidfile
  unregistered, invisible to the audit). The guard now compares start-time only.
- **Slow-first-token workers are no longer false-wedged.** The heartbeat wedge
  requires at least one progress event before it can fire, so a worker sitting at
  ~0% CPU waiting on a slow backend for its first token is not killed mid-wait.
- **A no-progress event flood can no longer hang forever.** A progress-silence wall
  (`--progress-stall-s`, default 300s) bounds time-since-last-progress independently
  of raw-event recency and CPU, so a worker emitting only vendor/noise notifications
  with zero real progress is still terminated.
- **`cursor`/`cursor-agent` cap lowered 5 → 3.** Cursor's cloud backend is slow and
  degrades under concurrency; it completes reliably up to ~3 concurrent.

## [0.4.3] — 2026-05-20

**ACP dispatch reliability.** 0.4.2 taught the runner to *observe* worker
liveness and recover a stalled handshake; 0.4.3 closes the field failure modes
its passive heartbeat could not. A controller running 0.4.2 reported `ACP = 0%`
with both adapters hung — a worker's long-running tool call (web search) wedging
the event stream, and mass-spawned Claude-TUI adapters starving each other at
startup. Now the heartbeat *acts* on a confirmed wedge (even when the idle gate
is off), an outstanding tool call gets a grace window plus an absolute wall, an
oversized ACP frame is bounded instead of hung, and fragile-adapter startup is
serialized.

### The problem

- **The 0.4.2 heartbeat observed but never acted.** It wrote progressive
  CPU/event status, but only `session_prompt`'s idle-timeout could actually
  cancel a worker — so `--idle-timeout 0` (a supported goal-mode path) had no
  wedge backstop at all, and a wedge inside a long legitimate-silence window went
  uncaught.
- **A long-running tool call wedged the stream.** A worker that emits a
  `tool_call` (web search, a big test) then goes silent looks identical to a
  wedge under the CPU rule — I/O-wait sits at ≈0% CPU. The CPU grace alone could
  not tell a healthy in-progress tool from a genuinely stuck one.
- **An oversized result frame hung the reader.** An ACP frame larger than
  asyncio's stream limit raised `LimitOverrunError` deep in the read loop;
  pending requests never resolved and the runner hung indefinitely.
- **Mass-spawned Claude-TUI adapters starved at startup.** Four simultaneous
  `claude-code-cli-acp` dispatches each blew the adapter's hardcoded 120s
  per-turn timeout on an otherwise trivial turn (3/4 failed); the same four with
  serialized startups ran 4/4. The contention is the TUI's *startup*
  (hooks/LSP/keychain/auto-memory/MCP), not steady-state.

### Added

- **Heartbeat-driven wedge detector that acts (`goalflight_acp_run.py` +
  `goalflight_liveness.heartbeat_wedge_decision`).** The concurrent heartbeat
  now kills and finalizes a worker on a *confirmed* wedge, independent of the
  idle-timeout — so `--idle-timeout 0` is still protected. A dead sample requires
  ALL of: PID alive, pgroup-CPU ≤ epsilon, event count unchanged since the last
  beat, and zero outstanding tool calls; `--wedge-samples` consecutive dead
  samples (default 4; env `GOALFLIGHT_WEDGE_SAMPLES`) are required before the
  kill, so a single transient `ps` failure or a momentary lull cannot
  false-positive. Terminal state `wedged`.
- **Tool-call grace + an absolute per-tool wall (`ToolActivity`).** The runner
  tracks outstanding ACP tool calls. While a tool is in flight the dead-sample
  wedge rule is suppressed — a silent web search is NOT a wedge (the field case).
  But an individual tool outstanding past `--max-tool-s` (default 1800s; env
  `GOALFLIGHT_MAX_TOOL_S`) is killed *regardless of CPU* — the wall is ABSOLUTE,
  so a tool that is CPU-busy or whose CPU is unsamplable still trips it. Terminal
  state `tool_timeout`. A separate `--max-quiet-s` (default 3600s; env
  `GOALFLIGHT_MAX_QUIET_S`) bounds total event-silence for a CPU-busy quiet
  worker → `wedged`.
- **Oversized-frame guard (`acp_client.py`).** The read loop catches
  `LimitOverrunError` on an oversized newline frame, drops that frame, increments
  the dropped-frame counters, logs it, and continues. Oversized notifications are
  skipped; if an oversized response is dropped, the pending request reaches the
  existing idle/timeout failure path. No new run emits terminal state
  `result_too_large`.
- **StartupGate (`scripts/goalflight_startup_gate.py`, new).** An async context
  manager that serializes the spawn→handshake window of fragile adapters via a
  per-agent `flock` (`/tmp/goal-flight-startup-locks/<agent>.lock`). It is
  *handshake-gated, not a hardcoded stagger* — the lock is held only across
  spawn + handshake, so the next worker starts the instant the previous one is
  ready, on any machine (no interval baselined to one laptop's speed). Default
  serializes the Claude TUI adapter only (override via env
  `GOALFLIGHT_SERIALIZE_STARTUP`); fail-open after `max_wait` (600s) so a stuck
  holder cannot deadlock the fleet. Concurrent *turns* stay parallel; only
  startup is throttled.
- **New typed terminal states wired through capacity.** `wedged` and
  `tool_timeout` join `TERMINAL_LEASE_STATES`, so a leased slot is freed and
  pruned on its terminal-at the same as `complete`/`failed`. The legacy
  `result_too_large` state is retained only for pruning old 0.4.3 lease records.
  The per-agent caps are now backed by stress-test evidence plus StartupGate:
  codex-acp / grok = 10 (verified 49/49 + 13/13
  true-simultaneous, zero wedges), claude / claude-code-cli-acp = 5 (startup
  serialized), cursor = 5.
- **Tests.** `test/test_acp_failure_modes.py`: case_n (a live tool survives a
  silent gap — the field-case grace guard), case_o (a silent tool past the wall
  → `tool_timeout`), case_p (oversized frame → drop-and-continue counted; an
  oversized response reaches the idle/timeout failure path), case_q (a CPU-*busy*
  tool past `--max-tool-s` still hits
  `tool_timeout` with `elapsed < 2.0` — the discriminator vs the old CPU-gated
  wall). `test/test_goalflight_liveness.py`: the heartbeat dead-sample decision
  table. `test/test_startup_gate.py` (new): serialization, no-op, fail-open,
  exception-release, env-override. `test/test_wedge_detector.py` (new): the
  corpus-verified tool-grace rule across codex/grok/claude event shapes.
  `test/test_goalflight_procedural.py`: terminal-prune coverage for the new
  states.

### Notes

The wedge detector and the idle-timeout are deliberately separate: the
idle-timeout bounds the gap between events (and rides the CPU grace), while the
heartbeat wedge detector is the absolute backstop that fires even when the idle
gate is disabled. The root causes — the intermittent codex-acp handshake stall
and the Claude-TUI startup contention — live in those adapters; goal-flight's job
is to detect, bound, and recover, which it now does for the handshake (0.4.2) and
for the wedge / stuck-tool / oversized-frame / startup-storm failures (0.4.3).
The broader typed timeout-state taxonomy and the detached supervisor remain
deferred to a later phase.

## [0.4.2] — 2026-05-20

**ACP dispatch liveness & reliability.** A coherent pass at the failure mode
behind the codex-acp "wedge" reports and the idle-timeout retry storm: the
runner now *observes* worker liveness (process-group CPU + a progressive
heartbeat status file) instead of trusting event/tail silence, fails fast on a
stalled handshake and retries it once, and never false-positive-cancels a
healthy-but-quiet worker. Lands Phase 1 of the converged liveness design.

### The problem

- **A stalled handshake hung the runner forever.** `initialize()` /
  `session_new()` blocked on an unbounded `await fut` when a worker spawned but
  the handshake stalled — the codex-acp wedge: adapter idle at 0% CPU, empty
  acp-run.log, no status JSON. The stall is *intermittent* — the same handshake
  answers in isolation — so it is not a client bug to patch but a condition to
  detect and recover from. Because the handshake precedes `session_prompt`, the
  execution idle-timeout never applied; the runner hung until something external
  killed it. **0.4.1 made this strictly worse** — raising the goal-mode
  execution idle-timeout to 36000s meant a wedged goal-mode handshake could hang
  ~10h before any bound fired.
- **Event-silence was the only liveness signal.** A healthy worker grinding a
  long test/compile emits no ACP events for minutes; the idle-timeout killed it
  → the retry storm. Status JSON was written final-only, so a watcher couldn't
  see progress.

### Added

- **CPU-aware liveness (`scripts/goalflight_liveness.py`, new).** Process-group
  CPU is the false-positive killer: a silent worker with CPU > epsilon is
  `running_quiet` (left alone); a silent worker with CPU ≈ 0 is `wedged`
  (cancelled fast). `classify_liveness()` maps (pid-alive, pgroup-CPU,
  seconds-since-event) → `running` / `running_quiet` / `wedged` / `worker_dead`.
  CPU is summed across the process GROUP — the codex-acp `node` wrapper can idle
  at 0% while the child binary grinds.
- **Progressive heartbeat status (`scripts/goalflight_acp_run.py`).** Status
  JSON now advances `starting → handshaking → running`, and a concurrent task
  samples pgroup-CPU every `--heartbeat-interval` seconds (default 15s; env
  `GOALFLIGHT_HEARTBEAT_INTERVAL`), recording `worker_pid`, `pgid`,
  `worker_alive`, `pgroup_cpu_pct`, `events_seen`, `last_event_at`,
  `heartbeat_at`. Heartbeats are **files, never task-notifications** — the
  controller is woken only on an actionable transition, never per beat (a
  per-beat wake would re-process its whole cached session — ruinous).
- **CPU liveness wired into the runner's own idle path.** `session_prompt()`
  (`scripts/acp_client.py`) gained a generic `on_idle` hook: on idle expiry it
  asks "keep waiting?" before cancelling. The runner supplies a pgroup-CPU
  probe, so a healthy-but-quiet ACP worker is `running_quiet`, not
  `agent_timeout (idle)`. The CPU policy lives in the runner, not the vendored
  client (the hook stays generic). Re-sampling rides out a transient `ps`
  failure instead of reading it as 0-CPU.
- **Handshake retry-once.** On a stalled handshake the runner kills + respawns
  the worker and retries the handshake once before falling back (the stall is
  intermittent, so one respawn usually clears it). The wedged worker is always
  reaped first — never retry while an identity-matched PID is still alive.
- **CPU-aware watchers.** `scripts/goalflight_watch.py` and
  `scripts/watch-dispatch-tail.sh` add `running_quiet` (PID alive + pgroup-CPU >
  epsilon ⇒ keep watching, no idle-timeout exit), with a consecutive-sample
  confirm so a transient `ps` failure can't false-positive a wedge. The bash
  watcher stays bash-3.2 portable.
- **60s handshake timeout** on `initialize()` / `session_new()` via an optional
  `timeout` on `_send_request`. A healthy adapter answers in well under a
  second; 60s tolerates a slow cold-start while catching the stall two orders of
  magnitude faster than the execution idle-timeout. On timeout it raises a clean
  `AcpError` the runner converts to `state=failed` — so the controller observes
  the failure (and now retries once) instead of hanging.
- Tests: `test/test_goalflight_liveness.py` (classify boundaries incl.
  None+idle, the CPU-sample grace, the parser, live `running_quiet`);
  `test/test_acp_failure_modes.py` case_g (handshake-timeout), case_h (on_idle
  keeps a busy worker alive — the P1 regression guard), case_i (on_idle False
  still cancels), case_j (handshake retry kills + respawns); plus
  `tests/test-watch-dispatch-tail.sh` running_quiet.

### Notes

The handshake timeout is deliberately separate from the execution idle-timeout:
handshake = short (catch the stall), execution = long (tolerate legitimate
goal-mode silence). This is Phase 1 of the liveness design; the typed
timeout-state taxonomy, semantic dispatch `--kind`, and the detached supervisor
are deferred to later phases. The root cause inside codex-acp (an intermittent
adapter handshake stall) is worth a separate upstream look; goal-flight's job is
to detect and recover, which it now does.

## [0.4.1] — 2026-05-20

Patch: goal-mode ACP dispatches no longer die at a 5-minute idle ceiling.

### Fixed

- **`scripts/goalflight_acp_run.py` idle-timeout was 300s (5min) for all
  dispatches** — fine for one-shot, fatal for goal-mode-over-ACP. A
  goal-mode worker churning through a long test/compile can emit zero
  agent events for tens of minutes; the 5-minute ceiling would kill a
  healthy multi-hour run mid-loop. (Idle-timeout is the gap between
  events, not total runtime — it resets on every event — but long silent
  stretches in goal-mode are legitimate.)

### Added

- **`--mode {one-shot,goal}` flag** on `goalflight_acp_run.py`. Derives
  the idle-timeout default: `one-shot`=300s (tight; a short dispatch
  silent that long is wedged), `goal`=36000s (10h; a safe wedge-detector
  ceiling — 10h of *total* silence means genuinely stuck). Explicit
  `--idle-timeout <secs>` still overrides; `--idle-timeout 0` disables the
  idle gate entirely (rely on PID liveness + the worker's terminal
  marker).
- Lease-TTL derivation is now mode-aware so a no-timeout (`--idle-timeout 0`)
  goal run holds a 40h lease instead of collapsing to the 1h floor and
  freeing its capacity slot mid-run.
- `protocols/dispatch-routing.md` documents the `--mode` flag in the ACP
  dispatch recipe with the idle-vs-total-runtime distinction.

Note: the codex `/goal` bash-tail path (per `templates/codex-goal-prompt.md.tpl`)
was already safe — it uses manual tail monitoring with no fixed timeout.
This fix is specifically for goal-mode dispatched over ACP, a supported
path per the dispatch-routing composition table.

## [0.4.0] — 2026-05-19

Dispatch-routing rewrite + adaptive rate-pressure walkback + worker
currency probes. Substantial shift in how goal-flight thinks about
worker routing and the controller's own rate-limit budget.

Note: the original 0.4.0 plan was a permission-gate. The 0.4.0-prep
refactor (commit `d372f47`, included here) cleaned up enough of the
substrate that the permission-gate is now smaller scope; this release
ships the routing/walkback/currency layer it sits on top of. The
permission-gate becomes a 0.5.0 candidate.

### Added — dispatch routing + legacy/

- **Two-axis dispatch routing taxonomy** in `protocols/dispatch-routing.md`.
  Iteration pattern (one-shot vs goal-mode) × comms shape
  (controller-direct / acp / bash-tail). Composition rules table
  documents `goal-mode + bash-tail` as **codex-`/goal`-only** — codex
  self-terminates with a "Final response" block giving the watcher a
  turn-boundary signal; grok / claude headless lack the equivalent.
- **`protocols/legacy/`** subdirectory — cold-storage recipes for
  bash-tail and `tail -f` paths that pre-date ACP. Loaded only when
  the primary ACP path is unavailable. Includes accurate CLI
  invocations (codex stdin redirect, grok `--permission-mode
  acceptEdits`, claude subshell pattern with no `--cwd` flag),
  watcher exit-code table, bypass-flag safety story.
- **`/goal-flight update`** — single command refreshes goal-flight
  itself (git pull on the source repo, with dirty-tree refusal +
  post-pull test gate) AND worker CLIs (each runs its own self-update).
- **Per-task routing table** in SKILL.md with explicit defaults +
  fallbacks per task category. Worker-bias note: prefer non-Claude
  CLI workers for code-writing dispatches when the controller is a
  Claude session (Claude Agent subagents share the controller's
  rate-limit budget; codex / grok / cursor do not).

### Added — rate-pressure walkback

- **`scripts/goalflight_rate_pressure.py`** (~225 LoC + 23 test
  assertions) — read-only probe that scans the dispatch ledger,
  classifies failures by **provider** (anthropic-session,
  anthropic-api, anthropic-cli-acp, openai, xai, cursor), and emits
  a JSON recommendation when 3+ rate-limit signatures hit the same
  provider in 10 minutes. Resolves the cursor/codex-acp aliasing wart
  at the provider level (per-label caps stay as process-count caps;
  rate-limit accounting collapses to one entry per vendor budget).
- **Walkback wired into pre-flight** — `commands/execute.md` step 1
  consults the probe before every dispatch (silent on clean, single
  STATUS line + fallback-provider hint on pressure).
  `commands/decompose-plan.md` step 0.4 consults anticipatorily
  before generating a multi-chunk plan.
- **Detection signatures**: case-insensitive substring match on
  "rate_limit", "429", "you've hit your limit", "usage limit",
  `anthropic.RateLimitError`, `openai.RateLimitError`, goal-flight's
  own `blocked_session_limit` state. `blocked_auth` explicitly
  carved out (auth needs credential repair, not cap-halving).

### Added — model + CLI currency

- **Cursor model discovery** — `cursor_models_probe()` in
  `goalflight_doctor.py` runs `cursor-agent --list-models`, picks
  highest-versioned `composer-X.Y` (non-`-fast`) as the leading
  internal model, reads `~/.cursor/cli-config.json` for the user's
  current model, flags `user_behind` when on an older internal or a
  paid-passthrough model. Avoids hardcoding model names that age.
- **Worker CLI currency probe** — `worker_currency_probe()` in
  `goalflight_doctor.py`. Grok via native `grok update --check
  --json`; codex / claude / claude-code-cli-acp via
  `npm view <pkg> version` registry compare against local
  `<cli> --version`. CLI-version-currency is the closest universal
  proxy for "model is current" since new models ship with new CLI
  releases.
- **Doctor integration** — payload now includes `rate_pressure` and
  `worker_currency`; human-readable output shows currency + pressure
  prominently when actionable, silent on clean.

### Changed

- **Generous static caps** in `goalflight_capacity.DEFAULT_AGENT_CAPS`:
  claude / claude-code-cli-acp / cursor / cursor-agent = 5; codex /
  codex-acp / grok = 10. Operating-cap tier 8 (>64GB RAM) bumped
  8 → 16 to give multi-session parallel work headroom.
- **Caps are placeholders, not laws.** Static numbers are best-guesses
  calibrated against the maintainer's vendor plans + 2026-05-19
  service health. Learned per-provider thresholds are future work.
- **Controller's own provider is asymmetric.** When goal-flight is
  hosted by a Claude Code session, `anthropic-session` is the
  controller's life-support. Bias conservative; don't probe upward
  on the controller's provider. The cost asymmetry (workday-ending
  vs re-routable) justifies the caution asymmetry.
- **--parallel monitoring threshold** is provider-specific, not flat
  N. Only re-probe between dispatches when 3+ workers map to the
  same anthropic-* provider. Empirical observation: codex / grok /
  cursor scale cleanly through N=10.
- **Queue-tag validation** in `commands/validate-queue.md` —
  `[goal-mode] + [bash-tail]` co-occurrence with non-codex worker
  promoted to P0 conflict (was previously undetected).
- **Cursor co-default for code-writing** alongside codex (cursor's
  2026-05-19 model update brought coding benchmarks on par with
  Opus). Prefer cursor's leading internal model (`composer-2.5` as
  of release) over its paid-passthrough variants which burn the
  Cursor subscription's paid budget.
- **gstack `/review`** named as the primary reviewer in
  `protocols/milestone-review.md`, with grok / cursor as
  concern-diverse sweep partners. Claude Agent reviewer is the third
  option, used only when codex AND the sweep tool are unreachable.

### Folded from `d372f47` (0.4.0-prep refactor; was already on main)

- **SKILL.md decomposition** — large prose split into
  `protocols/*.md` files (load-on-demand). Skill body shrunk from
  430+ lines to ~200 with the routing table.
- **Procedural runtime helpers** — 7 new `scripts/goalflight_*.py`
  emit compact JSON for the controller to read summaries
  (doctor / capacity / ledger / status / watcher / acp-runner /
  review-job).
- **ARCHITECTURE.md** — top-level orientation document.

### Deferred (separate future commits)

- Permission gate (originally scoped for 0.4.0; restructured around
  the refactor + routing work landing first — see
  `docs-private/plans/0.4.0-permission-gate-2026-05-18.md`).
- Learned per-provider rate-pressure thresholds with asymmetric
  controller-provider treatment (see `docs-private/BACKLOG.md`).
- Per-worker model-level currency for codex / grok / claude (CLI
  currency is the current proxy).
- Timezone-aware ledger windowing.
- `runs.d/` retention policy.
- Doctor exit-code escalation on severe rate-pressure.

### Tests

- 9 test suites pass (8 pre-existing + new
  `test_goalflight_rate_pressure.py` with 23 assertions).
- 4 adversarial review rounds (codex-acp + grok in parallel) folded
  before tagging — all blocking + recommended findings addressed;
  forward-looking items captured.

## [0.3.4] — 2026-05-18

Combined patch — folds two parallel-session work streams:

### Added — plugin manifest + `doctor` sub-command (commit 850b907 from sibling session)

- **`.claude-plugin/plugin.json`** — declarative plugin manifest making the
  skill discoverable through Claude Code's plugin form (alongside the
  existing clone-form install at `~/.claude/skills/goal-flight/`).
- **`commands/doctor.md`** — new read-only health-check sub-command
  (`/goal-flight doctor`): validates plugin package, companion tools, codex
  trust, context-mode, gstack, ACP availability. First-time-user diagnostic
  + ongoing skill-update sanity check. Surfaced in `README.md` Quickstart
  and the sub-commands table.
- **`tests/test-plugin-manifest.sh`** — validates the plugin manifest's
  schema + structural invariants. Hooked into `tests/run.sh`.
- **`scripts/acp_client.py` ACP dispatch hardening**:
  - `_discard_pending(req_id)` helper centralizes future-cancellation;
    used by both successful resolve and exception paths so pending
    request futures never leak.
  - `session_prompt` idle-timeout handling: emits `session/cancel` before
    yielding the timeout error so codex-acp / cursor-agent doesn't continue
    a dispatch the controller has abandoned. `idle_timeout=None` or `<=0`
    disables the gate (for long-running goal-mode loops where multi-minute
    gaps between agent_message_chunks are normal).
  - `CancelledError` propagation cleaned up — caller-cancelled requests
    discard the pending future and re-raise (no leak).
- **`scripts/acp_runner.py`**: minor doc / signature touches consistent
  with the dispatch hardening.
- **`test/test_acp_failure_modes.py`**: expanded with cancel-during-prompt
  scenarios that exercise the new `_discard_pending` + `session_cancel`
  paths.

### Fixed — `_save_pids` dedupe (this commit)

Per the latent issue flagged in the 0.3.3 CHANGELOG: `AcpProcessPool._save_pids()`
and Design 2's `_write_through_pidfile_locked()` previously both wrote to
`<controller-pid>.jsonl`. In a mixed-mode scenario (bare `AcpConnection` +
pool-managed connections in the same process), the pool's narrower view
would clobber the registry's superset — bare-conn orphan-defense entries
would silently disappear from the pidfile.

The dedupe:
- **Removed**: `AcpProcessPool._pidfile_dir` class attribute,
  `_own_pidfile()` method, `_save_pids()` method, and the two
  `self._save_pids()` call sites in `get_or_create()` + `close()`.
- **Single writer**: Design 2's module-level `_write_through_pidfile_locked()`
  is now the sole writer to the per-controller pidfile.
  `AcpConnection.__post_init__` calls `_register_connection` (on spawn)
  and `AcpConnection.kill()` calls `_unregister_connection` (on teardown)
  — automatic for both bare and pool-managed connections.
- **`cleanup_ghosts()` redirect**: reads from the module-level `_PIDFILE_DIR`
  instead of `self._pidfile_dir`. All the 0.3.2 hardening (bashtail-stem
  recognition, killpg safety, identity-verified TOCTOU defense) preserved.
- **Test override**: `pool._pidfile_dir = ...` → `acp_client._PIDFILE_DIR = ...`
  (module-level monkey-patch with restore in finally).

Documented as local change #13 in the vendored-credit header.

### Tests

- 7 passed / 0 failed (4 bash legacy + 1 plugin-manifest + 1 watcher + 2 Python).
- Test suite count expanded from 6 → 7 with the new plugin-manifest test.

## [0.3.3] — 2026-05-18

Folds two in-flight designs from the parallel ACP session into the hardening
surface so they're tested as part of the same release rather than landing as
a follow-up with unverified behavior:

### Added — Design 1: scope-leak audit (`scripts/acp_runner.py`)

- **`PromptResult.out_of_scope_writes: list[str]`** — new field on the result
  dataclass returned by `run_prompt()`. Populated post-hoc by scanning the
  ACP `tool_call` / `tool_call_update` events' `locations: [{path, line?}]`
  arrays against the connection's recorded `cwd`. Paths that resolve outside
  cwd land here as an audit signal for the controller's per-chunk diff-verify.
- **`_scan_out_of_scope_paths(tool_calls, cwd)`** — helper that does the
  resolution + classification. Key correctness properties: relative paths
  resolve against the CONNECTION's cwd (set by `session_new()`), NOT the
  caller process's cwd — avoids false positives when the controller runs
  from a different directory than the worker; dedupes; preserves source order;
  handles malformed locations (None / empty / missing path) defensively;
  empty/None cwd disables the check entirely (Path("").resolve() would
  spuriously match cwd).
- **`AcpConnection.cwd: str | None`** field added — set by `session_new(cwd)`
  so `run_prompt` can access it for the scope check.
- Also: **`extract_markers()` skips empty captures** (a bare `**STATUS:**` line
  with no content no longer creates a spurious empty-string entry).

### Added — Design 2: module-level connection registry (`scripts/acp_client.py`)

- **`_live_connections: dict[int, AcpConnection]`** + `threading.Lock` —
  registry that both bare `AcpConnection` and pool-managed connections enter
  via `AcpConnection.__post_init__` on construction. Removed via
  `AcpConnection.kill()` (also called by `close_gracefully()` and the async
  context manager exit).
- **`_write_through_pidfile_locked()`** — persists the live-connection snapshot
  to `/tmp/goal-flight-acp-pids.d/<controller-pid>.jsonl` on every register
  and unregister. Even a SIGKILL of the controller leaves the latest snapshot
  on disk for `cleanup_ghosts()` on the next controller startup to reap.
- **Closes the bare-`AcpConnection` orphan-defense gap** the prior code had:
  `AcpProcessPool._save_pids()` only registered pool-managed connections;
  scripts that used `AcpConnection` directly (small test fixtures, simple
  helpers) left no orphan record at all.

### Tests

- **`smoke_scope_leak_audit`** in `test/test_acp_pipe.py` — 5 sub-cases:
  in-scope-only returns empty; out-of-scope paths flagged + deduped +
  source-order; relative paths resolved against connection cwd; empty/None
  cwd disables checking; malformed locations don't crash.
- **`smoke_bare_connection_registry`** in `test/test_acp_pipe.py` — bare
  `AcpConnection` registers on spawn (verifies `_live_connections` entry
  + pidfile written with correct identity); kill removes from registry +
  pidfile entry gone after async-with exit. Also verifies
  `out_of_scope_writes` is empty when echo agent emits no tool_calls
  (sanity check on the Design 1 wiring through `run_prompt`).
- 6 passed / 0 failed across both suites.

### Known latent (not blocking — flagged for 0.3.4)

- `AcpProcessPool._save_pids()` and Design 2's `_write_through_pidfile_locked()`
  both write to `<controller-pid>.jsonl`. In a single-controller mixed-mode
  scenario (some bare `AcpConnection` + some pool-managed simultaneously),
  `_save_pids()`'s narrower view overwrites the registry's superset, dropping
  bare-conn entries from the pidfile. In practice goal-flight code uses
  either the pool OR bare connections, not both — so the race is theoretical.
  Slated for 0.3.4: deprecate `_save_pids()` in favor of the registry's
  `_write_through` as the single writer.

## [0.3.2] — 2026-05-18

Hardening release — pre-push audit of the 0.3.0 + 0.3.1 stack. Folds three
P0s and several P1s surfaced by a third reviewer pass (Claude + codex
hardening reviewers) before pushing to a public repo. The `review-before-commit`
rule extended to `review-before-push` because both 0.3.0 and 0.3.1 had
remaining defects that prior passes missed.

### Fixed (P0)

- **`scripts/watch-dispatch-tail.sh:79` bash 4+ `${var,,}` defect**:
  macOS default bash is 3.2 (Apple stopped updating because GPLv3). The
  watcher's missing-required-arg error path used `${required,,}` (lowercase
  substitution, bash 4+) which produced a "bad substitution" runtime error
  on bash 3.2 and fell through to exit 1 instead of EX_USAGE (64). Replaced
  with an explicit per-var `case` mapping (the var→flag table is small
  enough that this is cleaner than calling out to `tr`). Also added
  integer-validation for `--pid` and `--controller-pid` (non-integer values
  previously produced invalid JSON in the pidfile body) and an explicit
  `command -v python3` preflight (silent missing-python3 previously
  produced empty JSON-escape output and a malformed pidfile body).
- **Watcher EXIT trap unconditionally removed pidfile, orphaning live
  workers**. When the watcher exited on **idle-timeout** (code 2) or
  **controller-dead** (code 3), the worker was still alive but the EXIT
  trap removed the pidfile — leaving no record for `cleanup_ghosts()` to
  reap on the next controller startup. Refactored to
  `cleanup_pidfile_on_exit()` which preserves the pidfile when the worker
  PID is still alive at exit (any of: marker-then-wind-down, idle-wedge,
  controller-died-worker-survived, SIGTERM-of-watcher-while-worker-runs).
  cleanup_ghosts then reaps the orphan correctly on next controller start.
  Surfaced by codex hardening reviewer; missed by both prior reviewer
  passes and by my own test coverage.
- **`cleanup_ghosts()` `killpg` hazard for bash-tail entries**. Bash-tail
  workers spawned via `cmd &` in non-interactive bash INHERIT the parent
  shell's pgroup (the controller's). The prior `cleanup_ghosts()` called
  `os.killpg(pgid, SIGKILL)` unconditionally — which on a shared-pgroup
  bash-tail entry would kill the controller and every sibling worker.
  Defense added: for `agent.endswith("-bash-tail")` entries, only `killpg`
  when `pgid == pid` (worker IS its own session leader); otherwise fall
  back to single-pid kill. macOS lacks `/usr/bin/setsid` so the bash-tail
  recipe in commands/execute.md doesn't enforce isolation by default; the
  `cleanup_ghosts` defense is the safety net. Surfaced by codex hardening
  reviewer.

### Fixed (P1)

- **`SKILL.md` documentation drift**: three sites (lines 143, 348, 371-380)
  still described the old inline `while kill -0 $PID` polling pattern that
  0.3.1's `scripts/watch-dispatch-tail.sh` replaced. Refreshed to point at
  the canonical watcher recipe with all four exit-code semantics.
- **`commands/execute.md:27` `[goal-mode]` codex shape missing
  `--dangerously-bypass-approvals-and-sandbox` flag**: contradicted the
  template at `templates/codex-goal-prompt.md.tpl:6-8` (which has the flag
  and is empirically documented as required). A reader following the inline
  shape literally would dispatch without the flag and see codex emit
  `BLOCKED:` on first edit. Fixed the inline shape to mirror the template.

### Tests

- **`tests/test-watch-dispatch-tail.sh`** expanded from 10 to 19 assertions.
  New coverage:
  - Case 1b: marker received + worker also dead → pidfile REMOVED
    (verifies the trap correctly removes when worker is gone, not just
    preserves when alive).
  - Case 3 pidfile assertion: idle-timeout exit → pidfile PRESERVED
    (worker still alive, wedged; cleanup_ghosts must be able to reap it).
  - Case 4 pidfile assertion: controller-dead exit → pidfile PRESERVED
    (load-bearing path codex hardening reviewer specifically called out).
  - Case 5a/5b/5c: argument validation under explicit `/bin/bash` (macOS
    bash 3.2). Verifies exit 64 on missing args, exit 64 on non-integer
    `--pid` / `--controller-pid`, and confirms no "bad substitution"
    leak from bash-4-only parameter expansion patterns.

### Tests run

- 6 passed / 0 failed (3 bash legacy + 1 bash hardening + 2 Python).
- Watcher suite now 19 assertions, up from 10.

## [0.3.1] — 2026-05-18

Patch release adding **content-aware completion watcher** for the `[bash-tail]`
dispatch path, with unified orphan-defense across both ACP and Bash-tail
paths. Folds four coordination asks from the parallel ACP session.

### Added

- **`scripts/watch-dispatch-tail.sh`** — parameterized watcher backgrounded
  alongside each `[bash-tail]` dispatch. Replaces the inline `while kill -0
  $PID; do sleep 15; done` pattern that `commands/execute.md` step 2.b used
  to recommend. Key wins:
  - **Content-aware completion**: greps the worker's tail for any terminal
    marker (`^\**(COMPLETE|BLOCKED|USER-NEED|USER-CONFIRM):\**` — emphasis-tolerant
    for grok's `**MARKER:**` form). Exits when the marker appears, BEFORE
    the worker process exits. Codex `/goal` runs can sit alive for minutes
    after meaningful work lands while wind-down completes; the PID-only
    watcher delayed the controller by that window. Content-aware exit
    surfaces completion to the controller as soon as the worker emits the
    terminal line.
  - **Idle-timeout wedge detection**: exits 2 if the tail file size doesn't
    change for `--max-idle-secs` (default 180s, matching `SKILL.md`
    §Codex reliability no-progress threshold). Used to require a manual
    SIGTERM on the watcher when codex hung; now self-terminates.
  - **Controller-PID self-monitoring**: exits 3 if the controller PID dies
    (orphan watcher self-detection). No more zombie watchers polling tail
    files no one cares about after a controller crash.
  - **Pidfile registration in the shared ACP dir**: writes a per-watcher entry
    at `/tmp/goal-flight-acp-pids.d/<controller-pid>.bashtail.<worker-pid>.jsonl`
    on startup, removes via EXIT trap. Schema matches what `scripts/acp_client.py`
    `_save_pids()` writes (pid / pgid / started_at / cmd / agent / session_id) so
    `cleanup_ghosts()` reaps orphaned bash-tail workers using the same
    identity-verified PID-reuse-safe path the ACP workers use. Closes the
    pre-0.3.1 gap where `[bash-tail]` workers had no orphan-defense
    registration at all.
  - **Exit-code semantics surfaced via WATCHER-EXIT summary line**: every
    exit path prints `WATCHER-EXIT: <kind> exit_code=<N>` plus the last 30
    tail lines, so the controller's task-notification handler can branch
    on `marker` / `pid-dead` / `idle-timeout` / `controller-dead` without
    re-reading the full tail.
- **`tests/test-watch-dispatch-tail.sh`** — 10 assertions covering all four
  exit conditions plus pidfile lifecycle (created on startup, removed on
  every clean exit path).

### Changed

- **`scripts/acp_client.py` `cleanup_ghosts()`**: extracts controller-pid
  from the LEADING int prefix of the pidfile stem (`int(pf.stem.split(".", 1)[0])`)
  rather than requiring the full stem to parse as int. Supports the new
  `<controller-pid>.bashtail.<worker-pid>.jsonl` naming pattern alongside
  the existing `<controller-pid>.jsonl` ACP pattern. One ACP-side
  `cleanup_ghosts()` call now reaps orphans across both dispatch paths.
  Documented as local change #12 in the vendored-credit header.
- **`commands/execute.md` step 2.b `[bash-tail]` branch**: replaces the
  inline `while kill -0` watcher recipe with the parameterized
  `scripts/watch-dispatch-tail.sh` invocation. Documents WATCHER-EXIT
  semantics so the controller can branch on graceful-complete vs
  worker-crashed vs wedge-detected.

### Tests

- **4 bash suites** (`tests/run.sh`): unchanged 3 + `test-watch-dispatch-tail.sh`
  with 10 assertions across the 4 exit conditions + pidfile lifecycle.
  All green.
- **2 Python suites** (`test/test_acp_pipe.py` + `test/test_acp_failure_modes.py`):
  unchanged from 0.3.0. All green.

## [0.3.0] — 2026-05-18

Minor-bump release; folds two parallel adversarial reviews (Claude challenger
via Agent + codex challenger via `codex exec --dangerously-bypass-approvals-and-sandbox`;
both verdicts `block` pre-fold) PLUS the ACP transport implementation from a
sibling worktree (`scripts/acp_client.py` vendored from
aws-samples/sample-acp-bridge with goal-flight-specific corrections; pool +
runner + tests). Findings folded inline before commit, per the
review-before-commit workflow rule (we do convergence reviews on staged
changes BEFORE the public-repo commit, so history doesn't read as "ship X,
oops, X+1").

### Added — ACP transport (Agent Client Protocol)

- **`scripts/acp_client.py`** — vendored from
  [aws-samples/sample-acp-bridge](https://github.com/aws-samples/sample-acp-bridge)
  @ `2cd3c86`, MIT-0, with corrections needed for goal-flight's controller
  use-case: (1) `auto_allow_tools: bool = False` default — upstream
  auto-allowed every tool call unconditionally (fine for chat-bridge, bad for
  a controller that wants user-confirmation surface); (2) **Permission
  response schema corrected** to the ACP spec — upstream sent
  `{"optionId": "allow_always"}` which codex-acp rejects with -32700
  `missing field 'outcome'`; correct shape is
  `{"outcome": {"outcome": "selected", "optionId": "<id-from-request.options>"}}`
  with options introspection (prefer `kind="allow_always"`, fall back to
  `allow_once`, then to `options[0]`); (3) **asyncio reader limit bumped to
  8 MB** — default 65 KB chokes on goal-mode workers that stream long
  reasoning traces as single lines; (4) **`close_gracefully()`** — capability-gated
  `session/close`, then stdin close, soft-timeout wait, kill escalation;
  AcpConnection is async context manager; (5) **Ghost-cleanup pidfile
  upgraded to JSON-Lines with identity disambiguation** — upstream killed by
  PID alone, which on Mac (fast PID reuse) would SIGKILL unrelated processes
  after a controller restart; new cleanup verifies live `ps lstart+comm` against
  recorded values before killing.
- **`scripts/acp_pool.py`** — production-shaped wrapper around `AcpProcessPool`:
  `managed_pool()` async context manager wires SIGINT/SIGTERM/atexit handlers
  so controller crashes drain the pool. `compute_pool_ceiling()` reads
  `docs-private/env-caveats.md` for box RAM and computes `max_processes`
  using `(RAM_MB - 2048) // 1200` (worst-case worker RSS = cursor-agent peak,
  controller reserve 2 GB), capped at the AcpProcessPool default 20.
- **`scripts/acp_runner.py`** — ergonomic wrapper: `PromptResult` dataclass
  accumulating `agent_message_chunk` text / thoughts / tool_calls / plan /
  stop_reason / error from `session_prompt` notifications. `run_prompt()`
  with `idle_timeout` (default 300 s; raise for goal-mode dispatches that
  run multi-minute between events). `extract_markers()` pulls
  `STATUS:` / `RESULT:` / `USER-NEED:` / `USER-CONFIRM:` / `BLOCKED:` /
  `COMPLETE:` lines from accumulated text, tolerating optional markdown
  emphasis around marker tags (`**STATUS:** ...` style as emitted by grok;
  unwrapped `STATUS: ...` as emitted by codex) — matches the
  `^\**(MARKER):\**` regex from SKILL.md §Worker message passing.
- **`scripts/probe-box-capacity.sh`** — captures Mac/Linux box RAM + CPU +
  presence of `codex-acp`, `grok agent stdio`, `cursor-agent`,
  `claude-code-cli-acp`. Writes `docs-private/env-caveats.md` for the
  dispatch wrapper's Layer 4 to reference. Idempotent.
- **`test/test_acp_pipe.py`** — smoke test: vendored ACP client + ergonomic
  runner against an in-process `test/fixtures/acp_echo_agent.py`. Proves
  end-to-end JSON-RPC over stdio works without external worker CLIs / auth /
  network. Covers: ACP pipe roundtrip, runner accumulator, marker extractor
  (both codex-style unwrapped `STATUS:` and grok-style `**STATUS:**` markdown
  emphasis), pidfile identity safety against PID reuse, `compute_pool_ceiling`
  formula + fallback cases, `managed_pool()` async context manager teardown.
- **`test/test_acp_failure_modes.py`** — failure-mode tests: (a) worker
  process killed mid-prompt → connection-closed sentinel; (b) controller
  crash / cleanup-under-load via `pool.shutdown()`; (c) broken stdio pipe
  (worker writes garbage between valid frames).
- **`test/dispatch_acp_chunk.py`** — live end-to-end test against real
  `codex-acp` (requires the adapter on PATH + auth). Not in `tests/run.sh`
  because it's not hermetic; documents the chunk-dispatch loop via
  `managed_pool` → `get_or_create` → `run_prompt` → `extract_markers`.
- **`test/probe_real_worker.py`** + **`test/probe_worker_memory.py`** —
  resource probes for re-measuring worker RSS / ACP capabilities against a
  different box class. Output feeds the worst-case RSS budget used by
  `compute_pool_ceiling()`.

### Added — dispatch model integration

- **`SKILL.md` §Transport choice — ACP-first when available**: ACP composes
  with the single-shot and goal-mode workflow shapes as a structured transport.
  Untagged-ACP-capable chunks default to ACP; force with `[acp]` or back-off
  to `[bash-tail]`. Pool capacity auto-derived from `env-caveats.md`.
  Workers that don't speak ACP (or where the adapter is missing) fall through
  to Bash-`&`-tail-file automatically.
- **`commands/execute.md` step 2.b**: new `[acp]` dispatch branch driving
  `AcpProcessPool` + `acp_runner.run_prompt()`; new `[bash-tail]` branch
  holding the legacy Bash-`&`-tail-file shape; Untagged now picks
  transport-by-availability rather than hard-coding shell-out.
- **`commands/init.md` step 1.5**: new step running
  `scripts/probe-box-capacity.sh` to capture box RAM + ACP-worker availability
  to `docs-private/env-caveats.md`. Idempotent re-run on box change.

### Added — controller stewardship surface

- **§Inline office-hours — premise re-validation against drift** (`SKILL.md`).
  Largest conceptual addition. Backlog of premise-checks ride alongside
  dispatch turns: inferred premises (controller filling absences), gap-fills
  (the absence itself signals a missing-spec), and forward considerations
  (thinking-partner observations). Cherry-pick logic; non-blocking by default;
  validated answers land in `docs-private/premises-<topic>-<date>.md` so they
  survive compaction and feed executor dispatches via Layer 4. The mechanism
  is opportunistic — frontier-model judgment over when/what/how, not
  rigid automation.
- **Worker message-passing marker vocabulary** (`SKILL.md` §Worker message
  passing). Six worker→controller markers: `STATUS:`, `RESULT:`, `USER-NEED:`,
  `USER-CONFIRM:`, `BLOCKED:`, `COMPLETE:`. One controller→worker marker:
  `USER-CLARIFICATION:` (prepended on re-dispatch after a `USER-NEED:` is
  answered). Polling shapes for Bash `&` / Agent / ACP transports; pattern
  is markdown-emphasis-tolerant (`^\**(STATUS|...|COMPLETE):\**`) so codex
  and grok formatting both parse. Added as Layer 6 of `prompts/dispatch-wrapper.md`;
  validate-dispatch warns when missing.
- **Polish-skill class** (`commands/init.md` step 2.5, `commands/decompose-plan.md`
  step 0.5). Two sub-classes: interrogative skills (`/office-hours`, `/grill-me`)
  that return validated user answers, and reviewer skills (`/plan-eng-review`,
  `/eng-design-review`) that return findings. Interrogative skills run on the
  orchestrator (Claude-side `Skill(...)` or orchestrator-embodied gist) because
  workers have no user-facing channel; reviewer skills can dispatch as workers.
- **Memory companions** (`SKILL.md` §Memory companions). CASS + Hindsight as
  opt-in markdown-augmenters. Plain dated markdown remains the default.
- **Skill-loaded fingerprint header** for cross-session drift detection. Init
  step 1 / dispatch-wrapper / RESUME-NOTES carry `Skill-loaded: <version>@<sha> fprint:<8hex>`;
  pre-flight probe 4 catches version drift between session start and dispatch.

### Changed — naming convention

- **Goal-statement, goal-queue, and premises files use clustered prefix**:
  `goal-<topic>-<date>.md` (was `<topic>-goal-statement-<date>.md`),
  `goal-queue-<topic>-<date>.md` (was `<topic>-goal-queue-<date>.md`),
  `premises-<topic>-<date>.md` (new). The new prefix lets the three peer
  artifacts cluster when scrolling `docs-private/`. Legacy file naming is
  still accepted on read — `init` writes new naming, downstream commands
  prefer new and fall back to legacy. No migration required for existing
  projects.

### Verified empirically (2026-05-17)

- **Codex `/goal` non-interactive dispatch** accepts prompts up to 4407 chars
  cleanly on codex 0.130.0 + gpt-5.5 (probe: `/tmp/codex-goal-size-probe.md` →
  `/tmp/codex-goal-probe.out`). The 4 KB limit prior versions cited applies
  only to the interactive `/goal` slash command, not the `codex exec - <
  prompt.md` path goal-flight uses. SKILL.md / prompts/dispatch-wrapper.md /
  commands/execute.md prose updated to distinguish the two entry paths.
- **Codex autonomous edit requires `--dangerously-bypass-approvals-and-sandbox`**.
  Without the flag, codex correctly emits `BLOCKED: filesystem is read-only and
  approvals are disabled` after attempting the first edit. With the flag, the
  full edit → pytest → green loop completes in ~92 s. Safety story is
  load-bearing: the bypass flag trades sandboxing for autonomy; the worktree
  boundary only provides external sandboxing when `<workdir>` is a sibling
  worktree (parallel mode), not when `<workdir>` is the controller cwd
  (sequential mode). Always pass `-C <workdir>` explicitly so the safety
  story has a defined surface; in sequential mode the per-chunk diff-verify
  is the only fence. Verified at `templates/codex-goal-prompt.md.tpl:19` and
  `SKILL.md` §Codex reliability.
- **Grok `/implement` is an interactive slash command** (activates the
  `implementer` role bundled in `~/.grok/agents`), NOT a headless CLI flag.
  Headless equivalent goal-flight uses: `grok --prompt-file <f> --cwd <path>
  --permission-mode acceptEdits --output-format plain > <tail> 2>&1 &`.
  `--permission-mode acceptEdits` is required for autonomous file edits.

### Changed — controller framing

- **README opening + SKILL opening** reframe the controller as **high-level
  management, not execution**. The controller holds enough context about goal,
  scenery, and intent to exercise discretion and recommend next moves; actual
  work dispatches to workers (Claude subagents, codex, grok) that don't need
  that context. This is the frontier of lightly-supervised development:
  user ratifies suggested moves, redirects when needed, trusts the controller
  to keep the project anchored across compactions and unattended hours.
- **Dispatch-mode-by-duration rule**: any tool call expected to take more than
  ~10 s runs in background, so the user's terminal doesn't hang. Rule applies
  to the tool call's duration, not the subagent type. Replaces the prior
  "subagent foreground / codex-grok background" type-based prescription.
- **Goal-statement as working signal, not rigid gate**: `decompose-plan` proceeds
  on whatever signal exists (goal-statement when present, plus plan source,
  architecture doc, in-session conversation), surfacing inferred assumptions as
  backlog items. DRAFT state no longer blocks downstream commands.
- **Codex bypass-flag safety story is honest about sequential vs parallel mode**.
  Prior prose implied "worktree boundary provides external sandboxing" universally;
  in sequential mode the bypass dispatches against the controller cwd with no
  sandbox. Diff-verify is the only fence in that mode. Sites: SKILL.md §Codex
  reliability, commands/execute.md step 2.b, templates/codex-goal-prompt.md.tpl.

### Fixed — pre-commit reviewer sweep (this release's findings)

- **Naming-rename completed across all sites** that had stale legacy paths
  inside the same release: `commands/init.md` step 3 + RESUME-NOTES template,
  `commands/execute.md` step 1 queue lookup, `commands/validate-dispatch.md`
  step 1, `commands/validate-queue.md` no-args lookup, three prompt files
  (`ask-anticipatory.md`, `gstack-claude-review.md`, `gstack-codex-challenge.md`).
- **Layer 5 → Layer 6 cross-references** in SKILL.md and dispatch-wrapper.md
  (the new marker-vocabulary layer is Layer 6, not 5).
- **`validate-dispatch.md` heuristic for Layer 6**: warns when the
  marker-vocabulary line is missing — without it, workers can't signal back
  through the marker channel.
- **Private-domain leak in worked examples**: the inline-office-hours section's
  example was specific to plasma-physics research; replaced with a
  domain-agnostic `status='cancelled'` rows / aggregate example readable by
  any project. The codex-goal-prompt template's pre-paste-anti-example
  similarly genericized.

### Fixed — second-pass reviewer findings (post-ACP-absorb)

- **`AcpProcessPool` now plumbs `auto_allow_tools` through `_spawn()` to every
  spawned `AcpConnection`**. Pre-fix, `managed_pool()` was advertised as
  enabling controller-side auto-allow but the pool's spawn path constructed
  connections with the default `auto_allow_tools=False`, so every dispatched
  worker would hang on the first `session/request_permission` request. Empirically
  confirmed by the second-pass codex reviewer via live `test/dispatch_acp_chunk.py`
  failure (no deliverable, no markers). `managed_pool()` defaults
  `auto_allow_tools=True` because the goal-flight controller decides chunk
  acceptability before dispatching, not per-tool-call.
- **`compute_pool_ceiling()` fallback no longer fail-opens to 20**. Missing
  `env-caveats.md` (likely a fresh install where `init.md` step 1.5 hasn't run)
  → returns `CONSERVATIVE_FALLBACK_CEILING = 4` instead of `hard_cap = 20`. The
  hard-cap fallback would have happily spawned 20 cursor workers (~24 GB RSS) on
  an unknown box — unsafe default for laptops including small Macs.
- **`probe-box-capacity.sh` actually verifies `grok agent stdio` is supported**
  rather than declaring success on `command -v grok`. Older grok versions don't
  ship the `agent` subcommand; previously the probe would falsely advertise
  ACP-mode availability.
- **`probe-box-capacity.sh` capacity guidance table corrected** — the 8 GB row
  said "4 concurrent" while the formula `(8192-2048)/1200 = 5.12 → 5` yields 5.
- **`commands/decompose-plan.md` tag dictionary now includes `[acp]` and
  `[bash-tail]`**; `commands/validate-queue.md` schema validates them
  (mutually exclusive; `[acp]` warns when env-caveats shows adapter
  unavailable). Without these, the new dispatch tags were undocumented for
  the decomposer pass.
- **`commands/execute.md` parallel mode (§3.b)**: ACP-parallel called out as
  forward work — current `--parallel` dispatches Claude subagents only;
  pool-aware parallel coordinator is a future-release feature.
- **`README.md`**: "Three dispatch paths" now notes the ACP transport overlay;
  Quickstart mentions the new env-caveats artifact written by `init` step 1.5.
- **CHANGELOG accuracy**: failure-mode test scenarios corrected from the
  earlier "oversized response, permission denial" claim to the actual three
  scenarios in the file. Test coverage for the chunk-dispatch loop and resource
  probes now properly enumerated.
- **Missing `docs-private/notes/acp-pipe-validation-2026-05-17.md` citations
  softened** to point at the in-tree `test/` smoke + failure-mode tests as the
  real evidence base; the original citation was to a docs-private note that
  doesn't ship with the public repo.
- **`scripts/acp_runner.py` marker regex tested against grok-emphasis form**
  (`**STATUS:** ...`) — the regex was designed to tolerate it but the test
  fixture only exercised codex-style unwrapped markers. Tests now cover both
  branches plus single-asterisk emphasis variants.

### Tests

- **3 bash suites** (`tests/run.sh`): `test-install-codex-overrides.sh`,
  `test-register-context-mode-codex.sh`, `test-self-fork-detect.sh`. All
  green.
- **2 Python suites** (`python3 test/test_acp_pipe.py && python3 test/test_acp_failure_modes.py`):
  ACP smoke + runner + markers + pidfile safety + pool ceiling + managed pool;
  failure modes (worker-killed-mid-prompt, broken stdio pipe, cleanup under
  load). All green.
- **`test/dispatch_acp_chunk.py`** — live end-to-end against real `codex-acp`;
  not hermetic, runs manually only.

## [0.2.8] — 2026-05-16

Convergence-fix sweep against parallel adversarial reviews (codex challenge +
grok adversarial; both HOLD with high confidence). The reviews caught a real
P0 in the 0.2.5–0.2.7 dispatch-rule prose: the claim that "background dispatch
ends the turn at dispatch; the harness re-surfaces completion as a new turn via
task-notification" is true for Agent-tool `run_in_background: true` but FALSE
for Bash `&` dispatches (`codex exec ... &`, `grok -p ... &`). Bash `&`
launches the child and immediately exits the launcher Bash call — the harness
sends a task-notification for the LAUNCHER's exit, not for the child's
eventual completion. Without an explicit watcher, a codex/grok dispatch
silently runs to ground with no callback to the controller.

Outputs from the two reviews persisted at
`docs-private/codex-challenge-2026-05-16.txt` and
`docs-private/grok-adversarial-2026-05-16.txt`.

### Fixed (P0)
- **SKILL.md §Per-chunk loop dispatch rule** now splits Agent vs Bash
  background mechanics explicitly. Agent: `run_in_background: true` and the
  harness handles the completion-notification. Bash `&`: launcher exits
  immediately; the controller must wire a watcher (`while kill -0 $PID
  2>/dev/null; do sleep 15; done`) via `run_in_background: true` Bash so the
  harness fires a notification when the watcher (and therefore the child)
  exits. Documented as the canonical shape with a worked example.
- **commands/execute.md** step 2 (b/c) updated to match: untagged-default
  dispatch now explicitly says `run_in_background: true` for Agent OR `codex
  exec ... &` + PID-capture + watcher for codex. Step c renamed from "Wait
  for task-notification" to "End the dispatch turn" — the wait is structural
  (the next turn fires on notification), not a polling loop.

### Fixed (P1)
- **SKILL.md §Session pre-flight probe 1 (fingerprint compute)**: now
  surfaces multi-install ambiguity (clone-form AND plugin-form both present)
  instead of silently picking clone-form. The fprint recipe also checks each
  of the three behavior-bearing files exists before hashing; missing files
  produce `fprint:incomplete(<paths>)` instead of a plausible-but-wrong
  8-hex hash from partial content.
- **SKILL.md §Session pre-flight probe 4 (drift detection)** now distinguishes
  four outcomes: match (silent), no-header legacy (silent), malformed-header
  (surface "cannot compare" with the raw line), differs (surface compact
  forensics: source file, stored vs live, changed fields, and explicit
  guidance for the backward-fprint / rollback case so "Skill drift" reads
  correctly as "changed" not just "updated").
- **SKILL.md §Don't** "Poll a background subagent" rule renamed and
  clarified: "Poll an Agent-tool subagent's transcript or its `<output>`
  JSONL." Explicitly carves out `kill -0 $PID` watching Bash-spawned codex /
  grok children as the correct pattern (not banned by this rule).

### Deferred (P2 — queued for 0.2.9 or later)
- Conservative fallback for unknown-duration tool calls (current 10s rule
  doesn't specify how to estimate before launch).
- Soften the "cost ~50 ms" claim in §Session pre-flight intro (plugin-find
  + multi-file hash can exceed that on slow trees).
- Output-format robustness for `claude --version` and `npx context-mode
  --version` probes (init.md captures whatever string the CLI prints).

### Tests
3 suites / 46 assertions remain green throughout.

## [0.2.7] — 2026-05-16

Dispatch rule prose tightened — drop "foreground" mentions (it's the
harness default; no need to spell it out) and lead with the reason:
"so the user's terminal doesn't hang, allowing them to steer."

### Changed
- **SKILL.md §Per-chunk loop dispatch rule** — replaced "background if
  >10s; foreground for shorter calls is fine. Reason: foreground locks
  the user's terminal..." with "background if >10s — so the user's
  terminal doesn't hang, allowing them to steer." Same rule, fewer
  words, reason up front.
- **SKILL.md §Asking discipline** background-dispatch bullet — same
  prose simplification.
- **SKILL.md §Three subagent types** dispatch-mode note — dropped
  "foreground otherwise" tail.
- Feedback memory updated to match.

### Tests
3 suites / 46 assertions remain green.

## [0.2.6] — 2026-05-16

Dispatch-mode rule simplified to a duration threshold. 0.2.5 introduced a
type-based prescription (executor = background, reviewer / planner =
foreground) that was both wrong (most goal-flight reviewers / planners
take 30s–3min, well past any "inline" budget) and complicated. User
trimmed it to:

> Background if the tool call is going to be over ~10 seconds, so the
> user's terminal doesn't hang for steering.

That's the whole rule. Foreground / background isn't about agent type
or purpose — it's about whether the user can tolerate a locked terminal
for the call's duration.

### Changed
- **SKILL.md §Per-chunk loop** tightened: opens with the dispatch rule
  ("any tool call expected to take more than ~10 seconds runs in
  background"), then the steps. Dropped the two-turn-cycle exposition
  in favor of stating the rule once and letting the step list embody
  it.
- **SKILL.md §Three subagent types table** — Dispatch-mode column
  (added in 0.2.5) removed; replaced with a one-line note pointing at
  §Per-chunk loop for the duration rule. Type and mode are orthogonal;
  the table is about type only.
- **SKILL.md §Asking discipline** "dispatch executors in background —
  foreground = failure mode" bullet (0.2.5) replaced with
  "Background-dispatch anything expected to take more than ~10 seconds"
  — same rule, simpler framing.

### Why simpler
The 0.2.5 framing dragged in "Executor = background, Reviewer = fore-
ground" prescriptions that don't survive contact with how long goal-
flight's actual reviewers run. The duration threshold is the actual
predictor of whether the user's lockout cost wins. Strip the rest.

### Tests
3 suites / 46 assertions remain green.

## [0.2.5] — 2026-05-16

Executor dispatch defaults to background; foreground Agent for executors
is named explicitly as a failure mode. 0.2.4 added a "yield the turn
between chunks" rule, but treated the symptom — the root cause is that
foreground Agent dispatch keeps the controller's turn OPEN for the
entire executor run (often minutes), so queued user messages never
drain. Background dispatch (`run_in_background: true` for Agent;
`&` + tail-polling for codex / grok Bash) yields the turn at dispatch
time; the harness re-surfaces completion as a new turn via task-
notification. This makes the chunk loop a two-turn cycle instead of a
one-turn block.

### Changed
- **SKILL.md §Per-chunk loop rewritten** as a two-turn cycle: dispatch
  turn (step 1 background-dispatch + step 2 emit one-line status and
  end the turn) and completion turn (steps 3-7: verify diff, commit,
  update Progress, look-ahead, dispatch chunk N+1). The two-turn split
  is load-bearing — it's the structural mechanism that drains queued
  user input every chunk.
- **SKILL.md §Three subagent types table** gains a Dispatch-mode
  column: Executor = background (long-running, controller doesn't need
  result inline), Reviewer + Planner = foreground (short, result feeds
  the immediate next decision). Mismatching the mode is named as the
  most common pacing antipattern.
- **SKILL.md §Asking discipline** "yield the turn between chunks"
  bullet (added in 0.2.4) replaced with a more accurate "dispatch
  executors in background — foreground Agent is a failure mode"
  bullet that explains the root cause and points at the two-turn
  cycle.

### Why this matters
The 0.2.4 step 7 ("yield the turn before chunk N+1") was a workaround
that asked the controller to remember to emit one-line + STOP between
chunks. Easy to forget mid-execute. Background dispatch makes the yield
*structural* — the turn ends at dispatch time, not as a separate manual
step. Less prone to chain-the-chunks drift.

Foreground Agent stays correct for short inline reviewers (anticipatory
subagents, look-ahead Explore, decomposition reviewers) where the
controller genuinely needs the result inline. The pacing antipattern
is foreground Agent for *executors*, where the result isn't needed
inline anyway (the next decision is just "verify diff + commit", which
happens fine on the next turn).

### Tests
3 suites / 46 assertions remain green (prose-only changes; testable
scripts unchanged).

## [0.2.4] — 2026-05-16

Per-chunk turn-yielding rule made explicit. Field motivation: a user
running goal-flight against an academic-paper drafting flow watched
the controller dispatch chunks #9, #10, #11 back-to-back, each as a
proper subagent — but typed status requests and steering piled up
unprocessed at the bottom of the chat. The chunks WERE going to
subagents (correct path); the bug was that the controller was
chaining N chunks inside one assistant turn, so user-typed messages
queued at the harness level and never surfaced until the chain
broke.

0.2.3 fixed the controller-direct interactivity tradeoff but missed
this larger pattern: even when dispatching correctly, chaining inside
one turn defeats interjection. 0.2.4 closes that gap.

### Changed
- **SKILL.md §Per-chunk loop step 7 added (new step):** yield the
  turn before dispatching chunk N+1. Emit a one-line status (`Chunk
  #N landed at <sha>. Dispatching chunk #N+1.`) and STOP the current
  assistant turn. The next chunk dispatch fires on the next turn,
  triggered by user input (their queued message processes first) or
  by silent continuation (no input → next chunk proceeds). Exception
  carved for `[goal-mode]` loops where the loop primitive owns
  turn-boundaries.
- **SKILL.md §Asking discipline** gains a companion bullet:
  "Yield the turn between chunks" — clarifies this is NOT a Netflix
  check-in (no `Continue?` prompt) but a clean turn boundary the
  Claude Code harness needs to drain queued input.

### Why this matters
Without per-chunk yielding, a 14-chunk unattended run looks correct
from the controller's perspective (each chunk dispatched, verified,
committed) and broken from the user's perspective (no way to steer
mid-run despite multiple typed attempts). The skill's whole premise
is "12-hour unattended runs where you check in periodically rather
than babysit" — but "check in periodically" requires the check-ins
to actually work.

### Tests
3 suites / 46 assertions remain green (prose-only changes).

## [0.2.3] — 2026-05-16

Interactivity tradeoff for `[controller-direct]` dispatch path made
explicit. Field motivation: a user observed that a running controller
session was inlining work via `[controller-direct]`, blocking their
ability to comment / question / redirect mid-flight — the session
appeared "hung between agents" but was actually busy executing tool
calls. SKILL.md didn't call this tradeoff out, so the controller
defaulted to inline when subagent dispatch would have served the user
better.

### Changed
- **SKILL.md §Dispatch model `[controller-direct]` bullet** — added
  the interactivity tradeoff: while the controller inlines, the parent
  session is unresponsive to user input. Subagent dispatch (path 2)
  frees the parent so the user can interject. Heuristic added: prefer
  subagent dispatch when the user is at the keyboard, when the work
  will take more than ~1 minute even if the LoC delta is small, or
  when the chunk is parallel-safe so look-ahead can run alongside.
  Inline only when session-loaded state is genuinely load-bearing AND
  the work is short. ESC interrupts the current tool call (including
  a subagent dispatch) but doesn't roll back disk changes.
- **SKILL.md §Asking discipline** — companion rule added between the
  "no Netflix check-ins" and "prepare the question with subagents"
  bullets: don't monopolize the parent thread with long inline work.
  Same heuristic as the dispatch-model bullet, framed from the asking-
  discipline north star (user retains ability to interject = real value
  the controller protects).

### Tests
3 suites / 46 assertions remain green (prose-only changes; testable
scripts unchanged).

## [0.2.2] — 2026-05-16

Skill-update drift detection. Long-running controller sessions could load
SKILL.md, run for hours, and never notice when `git pull` refreshed the
skill on disk — they kept using the stale content. Failure surface for
this in 0.2.1: a session that loaded before the MCP-wrap rule shipped
would keep wrapping `codex exec` in `ctx_execute` and burning time on
mysterious hangs.

Grok design exploration (`/tmp/grok-autoupdate-out.txt`) proposed four
options ranked across lightness × unobtrusiveness × coverage; Option 2
shipped here. (Option 1, a dedicated `scripts/check-skill-update.sh`,
queued for 0.2.3 once this pattern proves out.)

### Added
- **`Skill-loaded:` header line** with `version@git-sha fprint:<8 hex>`
  in three places: emitted in session pre-flight's opening parenthetical
  (`SKILL.md` §Session pre-flight probe 1); written into new goal-queue
  files (`commands/decompose-plan.md` step 3); written into new
  RESUME-NOTES files (`commands/init.md` step 3). The fprint is
  `sha256(SKILL.md + commands/execute.md + prompts/dispatch-wrapper.md)`
  truncated to 8 hex chars — catches behaviour-affecting edits while
  ignoring isolated prompt-tweaks elsewhere.
- **Probe 4 in §Session pre-flight: skill-update drift.** When an
  in-flight goal-queue or RESUME-NOTES carries a `Skill-loaded:` header
  that differs from the live LOADED_LINE, surface one line:
  `"Skill updated since this session loaded: <old> -> <new>. Re-invoke
  /goal-flight to refresh SKILL.md, or read the section you need."`
  Silent when lines match exactly or when the file carries no header
  (legacy file from < 0.2.2 — treat as no-data, not as drift).
- **Read-and-compare sites** added in `SKILL.md` §resume steps 1-2 and
  `commands/execute.md` §Pre-flight — every controller entrypoint that
  reads a state file now does the comparison so the drift catches
  early, before the dispatched executors operate on stale conventions.

### Sources
- `/tmp/grok-autoupdate-out.txt` — grok-build design exploration that
  proposed Options 1-4 and recommended 2.
- Field motivation: see 0.2.1 §"Never wrap headless dispatches in an
  MCP tool call" — the rule users would miss until the next session
  restart absent this detection mechanism.

### Tests
3 suites / 46 assertions remain green (no test changes — convention
addition for the controller to follow; testable scripts unchanged).

## [0.2.1] — 2026-05-16

Post-convergence UX-friction batch + lessons from parallel sessions using
the skill. Three substantive commits on top of the 0.2.0 convergence stack
at `1ade7fd`, plus a grok-sweep fix-up that dropped an ungrounded review-
channel claim and tightened prose, plus a follow-on lesson capture for the
codex/context-mode timeout pattern surfaced in the field. Three parallel
grok-build review sweeps (broad correctness / adversarial / prose) drove
the fix-up.

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
- **"Never wrap headless dispatches in an MCP tool call"** bullet in
  SKILL.md §Codex reliability. Wrapping `codex exec`, `grok -p`, or
  `claude -p` inside `ctx_execute` (or any MCP tool call) hits the
  MCP/context timeout — the controller sees a hang even though the
  underlying process ran fine and exited; the output is stuck in the
  OS-captured stdout the MCP wrapper never returned. Pattern: Bash +
  `>` redirect to a file, poll via `while kill -0 $PID 2>/dev/null;
  do sleep 15; done`, then `ctx_search` the captured output AFTER
  exit. Reminder: for Claude code-writing chunks, prefer the Agent
  tool with `model: "opus"` over `claude -p` — Agent subagents are
  session-billed; `claude -p` is API-billed.

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
