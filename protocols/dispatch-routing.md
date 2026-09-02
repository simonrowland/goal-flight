# Dispatch Routing Protocol

Choose the smallest execution shape that can finish safely. Routing has two
orthogonal axes: **iteration pattern** (how many turns) and **comms shape**
(how the orchestrator observes the worker). Pick one value from each.

Operational routing still applies: resolve abstract roles through host maps;
type dispatches as executor, reviewer, or planner; use the five-layer wrapper;
give parallel fixes forbid lists; split broad chunks; and apply same-provider
review policy. Triggered lanes require the pinned package and execute pre-wave
check in `protocols/worker-context-package.md`.

## Axis 1 — Iteration pattern

- `one-shot`: send a single prompt, worker completes the chunk in one turn.
  Default. Use when the chunk has a clear definition of done and fits one
  worker context.
- `goal-mode loop`: worker iterates against a goal across multiple turns,
  either by self-loop (codex `/goal`, Grok Build headless) or by orchestrator
  re-dispatch through the same session. Use when the chunk needs
  review-revise cycles, exceeds one turn, or the worker should keep refining
  until a marker fires.

Right-size the shape — three loop failure modes to avoid:

- **Don't dispatch what you already know how to fix.** A serialized worker
  round-trip (spawn, orient, loop, review, report) costs minutes, and the
  wrapper cannot carry everything you currently hold; when the controller can
  already state the fix, controller-direct is correct even mid-run. The inverse
  rule (don't hand-iterate past ~3 edit/test cycles, and never explore
  in-session when a worker could) still holds — right-sizing cuts both ways.
  See Axis 2 for the full test.
- **Every loop exits through defensive failure-condition verification.** Before
  handoff, the worker states the concrete failure conditions for the patch,
  runs checks capable of exposing them, and hands off only when evidence shows
  those conditions are absent; "it should work" is not evidence.
- **In-loop review convergence is severity-gated and floor-based.** P0–P2
  findings block loop exit. Trivial/mechanical chunks self-review the seven
  categories to green; non-trivial chunks add at least two concern-diverse lenses
  as the floor, not the target; complicated optimizer/search/numeric or
  objective-bearing chunks use more than two in-context perspectives and deeper
  checks. Safe/easy in-scope P3s may be applied
  inline when mechanical (per `protocols/chunk-review.md`) — but they never drive
  another iteration; uncertain, non-mechanical, or out-of-scope P3s go into the
  worker's report for store capture. Never keep looping solely for P3 polish.
- **Loops are iteration-bounded.** No new green progress across ~3 consecutive
  iterations → stop and return `BLOCKED:` with evidence (the fix so far OR the
  next honest reason), not more laps.

## Axis 2 — Comms shape

- `controller-direct`: no worker spawned. The orchestrator does the edit itself.

  **The test is context, not size.** Write it yourself when you already hold
  what the change needs — you just diagnosed the bug, you just read the
  function, the fix follows from a review finding you have already consumed —
  and you can state the whole change before you start typing. That covers far
  more than one-liners: a bug you have just root-caused; a rename or comment fix
  confined to files whose current text and all intended call sites you already
  read this session (a search to be sure means explore — dispatch); or a review
  finding you already consumed. Spawning a worker to re-derive what you know is
  waste, and the hand-off itself loses fidelity.

  **The disqualifier is serialization, not effort.** Doing it yourself is one
  thing at a time; the fleet is many. Hand it off the moment any of these is
  true, however small the change looks:
  - you are about to explore — read files you have not read, search for the
    call sites, work out how it currently behaves;
  - you have run ~3 edit/test cycles without converging;
  - another ready unit does not depend on your held context and a slot is free —
    inline only the dependent unit; dispatch the rest;
  - trust requires implementation independence beyond the routine independent
    chunk review — dispatch implementation; never solely review your own
    non-mechanical patch.

  Before every inline edit, record in the controller trace either
  `in-flight: <dispatch ids>` or `fleet idle: <N free slots>`. With a free slot,
  dispatch every ready unit except the one uniquely dependent on unexported
  session state.
- `acp`: structured JSON-RPC stream over stdio. Default whenever an adapter
  exists. The orchestrator sees turn boundaries, tool calls, plan entries, and
  stop reasons as discrete events, not text.
  ```bash
  python3 <skill-root>/scripts/goalflight_acp_run.py \
    --agent <codex-acp|grok|cursor|claude> \
    --cwd "$PWD" \
    --prompt <prompt.md> \
    --mode <one-shot|goal> \
    --os-sandbox <off|read-only|workspace-write> \
    --status-json <status.json>
  ```
  The runner re-execs into `~/.goal-flight/venvs/acp-0.10/bin/python` when
  system `python3` cannot import `acp`; set `GOALFLIGHT_ACP_PYTHON` to override.
  That Python package is the controller-side client implementation. Workers do
  not need to be implemented with that SDK; they need to speak the adapter's
  declared ACP wire contract. A vendor CLI can expose its own `agent stdio`
  implementation while the manifest still owns command args, safe probes,
  liveness profile, and output contract.

  `goalflight_dispatch.py --shape acp` supports `codex-acp`, `cursor`, and
  `claude-acp` (`claude-acp` normalizes to the runner's `claude` label). It
  does not expose an `opencode` preset; route OpenCode through the host-specific
  helpers or the raw command passthrough described below.
  Cursor uses the `remote_api` liveness profile and the Cursor cap is 3 because
  the cloud turn can be slow while CPU stays idle. Claude uses the
  `claude-code-cli-acp` PTY session path; `StartupGate` serializes spawn→handshake
  and the Claude cap is 5.

  Cursor tool-use or file-writing chunks that need attended in-place approval
  use `--permission-mode inline` (alias: `--interactive`). Plain `auto` denies
  the individual boundary-crossing tool request, routes `USER-CONFIRM` to
  controller mail, and keeps the turn alive for independent safe work. A
  correlated steer answer can guide the next turn, but cannot retroactively
  authorize the already-denied tool call; use inline mode or a fresh explicitly
  authorized dispatch when that action itself must proceed.
  For permission-escalation questions, even a correlated `yes` is acknowledgment
  only: it does not authorize a retry or alternate route. It also does not join
  or poison an independent worker-marker decision cohort from the same turn.
  Any `no` is permanent and deny-biased across conflicting controller replies.
  Answer a routed question with
  `USER-CONFIRM-ANSWER: <question_id> yes|no <optional note>` in
  `goalflight_dispatch.py steer`. The `<question_id>` must match an outstanding
  mailbox question exactly; unknown, stale, and duplicate replies are delivered
  only as explicit non-authorization notices. A marker affirmative records
  `controller_decision=yes` and keeps its generation/scope audit fields, but
  marker prose has no tool-call id, kind, or canonical target and therefore
  cannot authorize a later non-read request. Status remains
  `guarded_action_authorized=false`, and worker delivery uses
  `recorded-yes-not-authorized` plus the inline-permission/new-dispatch
  instruction. Quoted copies of the
  authorize grammar in ordinary steer text or controller notes are rewritten
  to `quoted-yes-not-authorization` before worker delivery. An affirmative remains
  provisional through the question's arbitration deadline. Mailbox rows carry
  durable awake-monotonic arrival timestamps: only a `yes` proven written by
  the deadline participates, while any `no` remains deny-biased.
  After a finalized per-question answer is exposed to the worker, its audit
  decision is immutable and later replies to it are rejected as audit-only.
  A distinct later denial is tracked as a generation-wide future-action bound;
  it does not rewrite the earlier row. Unanswered questions have a
  600-awake-second deadline by default and fail closed to correlated `no`.
  If the worker keeps its current ACP turn open to wait, the harness exempts
  quiet only until that deadline. On expiry it first reconciles a correlated,
  provably timely reply already in the mailbox; without one it records
  `user_confirm_overdue=true` and denies. Either settlement re-enables ordinary
  silence/wedge detection, so a turn that never releases the session lock
  terminates detectably.
  Questions in one same-origin decision cohort fail closed together. Routing
  any marker closes non-read permissions for the remainder of that ACP
  connection. An explicit or synthesized `no` also bounds every later action in
  that generation; every recorded marker `yes` is delivered as
  `recorded-yes-not-authorized` with the inline-permission/new-dispatch
  instruction and does not reopen non-read permission routing. Historical restart tombstones stay
  audit-visible but do not poison the fresh connection or its terminal
  qualification. If another hard terminal condition ends the run first, every
  still-open question is recorded as denied while the
  harder terminal state and error remain authoritative. Safe work may be preserved in
  `result_text`, but a run with a denied requested action ends in a qualified
  blocked state, never clean `complete`/`ok=true`.

  **`--max-idle-secs` gates quiet workers.** Default: 600s for write-capable
  code workers and 180s for read-only, research, or custom workers. The idle
  timeout is the gap between events, NOT total runtime — it resets on every
  event, so a healthy worker emitting periodic STATUS markers never trips it.
  Override with `--max-idle-secs <secs>` (or `--max-idle-secs 0` for no idle
  gate, relying on PID liveness + the worker's terminal marker).
  **grok caveat:** bash-tail grok can run with an EMPTY tail until a single
  final write (empty/no-op tails observed across permission-mode variants,
  2026-06-10) — treat tail silence as normal there, never as death by itself;
  PID identity + final marker are the liveness signals. grok over ACP still
  emits turn/tool events but can be event-quiet through a long generation —
  give research one-shots a generous idle override rather than the 180s
  read-only default.
- `bash-tail`: worker writes stdout/stderr to files; the orchestrator watches
  via marker grep. Fallback only when no ACP adapter is available. See
  `protocols/legacy/bash-tail.md` for recipes and hazards (incl. the
  context-mode-dispatch caveat — never wrap a spawn or `tail -f` in
  `ctx_execute`).
  ```bash
  python3 <skill-root>/scripts/goalflight_watch.py \
    --pid "$WORKER_PID" \
    --tail <tail-file> \
    --status-json <status.json> \
    --agent <agent>
  ```

## Dispatch wrapper default

Canonical direct dispatch is detached/background:

```bash
python3 <skill-root>/scripts/goalflight_dispatch.py --agent codex --prompt-file p.md
```

The dispatcher prints `DISPATCH-LAUNCHED` with the dispatch id, status JSON,
tail path, and worker PID, then returns immediately. Controller entry auto-claims
the canonical-project lease without stealing a live different generation. When
supervisor absence is proven, arm one generation-bound listener in the background
using the returned label and nonce. Restart a live supervisor instead; on UNKNOWN,
resolve supervision before arming a direct component:

```bash
python3 <skill-root>/scripts/goalflight_messages.py listen \
  --project-root "$PWD" --controller-label <label> --lease-nonce <nonce>
```

The listener writes an audit row and terminates body-free when mail exists after the
stored cursor. On exit 0 the controller peeks with `relay --new --json`, processes
the returned items, advances their server-known stream positions with
`advance --cursor-version <version> --position <stream>=<seq>`, then re-arms. Exit 5
is settled did-not-arm (dead or mismatched lease nonce): do not treat it as a ring
and do not re-arm that nonce. Exit 2 is retryable journal unreadability, not a dead
nonce. Peek again to derive remaining mail. Listener, drainer, mirror, and dashboard
roles never claim or renew the controller lease; a verified watchdog tick may renew
it.

### Controller correspondence addressing

Controller-to-controller envelopes carry a durable label plus canonical project
root. Producers journal the delivery assignment before projecting its JSONL carrier.
The one-shot listener and `goalflight_status.py --wait` read that same journal
authority; callers do not rebuild ownership or add a second addressee filter.

`relay --new` is a read-only peek at journal-pending events. Advancing delivery
requires the generation-stamped listener token; legacy read/ack cursor files,
`mark-read`, `triage-backlog`, and relay acknowledgment do not exist. Coverage rows
replace process-table discovery.

Use `goalflight_status.py --dispatch <id>` for a snapshot, or background
`goalflight_status.py --wait <ids>` only when an unclaimed fixed-set terminal
join and its timeout verdict are specifically needed. Exit 3 means mail, not
completion; read it, then run the printed pending-id re-arm.

Prefer `--prompt-file` over inline `--prompt` for anything beyond a short
one-shot, and always when prompt text exceeds ~2KB: workers compact too — an
inline prompt is unrecoverable after the worker's own auto-compaction, while a
file path survives summarization and supports a standing re-read instruction
(`$GOALFLIGHT_PROMPT_FILE`; `protocols/worker-context-package.md` §Pin durability). Likely-long chunks
default to the goal-loop shape so tests-green + review convergence — not the
worker's memory of the prompt — is the exit condition.
Per-project orientation rides as an auto-injected pointer when
`docs-private/rag/ORIENTATION.md` exists; see
`protocols/worker-context-package.md` §Canned orientation.

Before launch, look up any recorded blocked reason for the same work item and
re-fire only when its disposition has changed; repeated diagnosed blocks are a
dispatch-gate concern, not a scouting concern.

### Background by default; don't block the controller

The controller dispatches workers in the background and self-paces from the
ownership event listener and queue drains. Arm the wait; let it wake you. A timer
is a fallback for state no channel can report, such as external CI, a remote
queue, or a deploy. A timer that asks whether a worker finished is polling an
available channel. Background every controller tool call expected to run longer
than about 10 seconds so typed steers remain visible and ESC/Ctrl-C interrupts
only the observer surface, not the detached worker.

Blocking waits are for short scripted synchronous needs. Prefer default detached
dispatch plus the background listener; use `goalflight_status.py --dispatch <id>`
for a snapshot or background bounded `--wait <ids>` for an unclaimed fixed join.
The `--wait` default is 1800 seconds and reports still-pending ids with a nonzero
exit; `--wait-timeout 0` is explicit unbounded waiting. `goalflight_dispatch.py
--foreground` blocks until terminal state and should stay rare because it locks
the controller terminal and queues typed steers behind the wait.

Durable queue dispatch:

```bash
python3 <skill-root>/scripts/goalflight_dispatch.py --submit --drain-on-submit --agent codex --prompt-file p.md
```

Synchronous scripts/tests that need the worker exit code must opt in:

```bash
python3 <skill-root>/scripts/goalflight_dispatch.py --agent codex --prompt-file p.md --foreground
```

## Event wake arming (the supervisor owns it)

The controller's event wake is ONE `goalflight_messages.py supervise` process
armed through the HOST'S PERSISTENT MONITOR — on Claude Code, the Monitor tool
with `persistent: true`; never a bounded monitor, never shell `&`:

```bash
python3 <skill-root>/scripts/goalflight_messages.py \
  supervise --project-root "$PWD" --controller-label <label> --lease-nonce <nonce>
```

- **Arm it with NO timeout.** Do not set, tune, or reason about a timeout
  value: a bounded monitor is killed outside the supervisor, no `type=stop`
  record appears, and the controller goes deaf without a diagnostic (the
  fleet-wide one-hour coverage drop, b-248).
- **Stop any old direct listeners FIRST**, then arm supervise (b-242):
  starting it alongside running listeners permanently stops its slots.
- `GOALFLIGHT_PERSISTENT_BACKUP_SLOTS` is optional at the shipped default of 2;
  set it only to override doorbell depth.
- If you filter the supervise stream, the allowlist must pass `kind=next` and
  the `stop`/`exit`/`restart` records carrying `rearm` — a narrow filter
  re-creates the silent death the supervisor exists to prevent.
- Arm a bare component (`follow`/`listen`) only on a host with no persistent
  monitor, or after supervisor absence is proven — never against a live
  supervisor. Full arming order and JSON-line contracts:
  `protocols/controller-mail.md`.

## Durable dispatch queue

- `goalflight_dispatch.py --submit` enqueues without blocking. `dispatch_id` is
  the idempotency key: same id plus same replay args is a no-op with rc=0; same
  id plus different args is a collision with rc=64.
- `goalflight_dispatch.py drain` is short-lived. It launches only work that fits
  current capacity caps, refreshes stale capacity first, and exits after one
  pass (normally about a second), so cron or another supervisor may re-run it.
- Queue rows start as `queued` in status JSON and the dispatch ledger. During a
  drain claim the queue file carries `claimed` plus recovery metadata until the
  worker reaches `starting` / `running` or stale-claim recovery resolves it.
- `goalflight_status.py --done <dispatch-id>` treats `queued` /
  `waiting_capacity` as still in flight, terminal states as done, and ambiguous
  stale states as inconclusive until liveness or terminal output resolves them.
- Claim recovery is launch-token based. A claimed row gets a launch token before
  spawn intent. Stale token-only claims may be restored as `queued`; once launch
  or spawn metadata exists, the claim is not restored. If matching worker
  tracking is lost, recovery records `worker_dead`; legacy no-token claims may
  still be restored.
- Worker delivery stays pointer-only: workers write findings, reviews, and long
  evidence to files, then return paths plus compact status markers.

## Claude worker surfaces (loop / one-shot / remote)

When a chunk is routed to a **Claude** worker, the surface follows the iteration
pattern and local-vs-remote. This is orthogonal to *whether* it should be Claude
at all: default code-writing to sub-billed CLI workers to preserve the
orchestrator's own provider budget (controller-provider asymmetry), and reach for
Claude when it is the right tool or the Claude budget is abundant.

- **Long-running, autonomous, looping** local Claude work -> a **`claude agents`
  background session**: it can run `/loop` (self-paced), its transcript is
  readable so the operator can watch live, it authenticates in-process, and it
  needs no pty.
- **One-shot, or controller-driven recursion** (the orchestrator re-invokes it
  each iteration) local Claude work -> a **host subagent**: it cannot self-`/loop`
  (the orchestrator drives the iterations), but its transcript is readable.
- **Remote** Claude work -> **`claude-acp` on a non-sandboxed host**: a full
  remote Claude session over ACP that **supports loop**. It needs a free pty on
  the node plus a headless subscription credential: run `claude setup-token` on
  the node and export it there as `CLAUDE_CODE_OAUTH_TOKEN` (e.g. `~/.zshenv`) so
  non-interactive ssh and the ferried detached worker both see it (`claude auth
  status` -> `oauth_token`/`firstParty`). Full recipe: `docs/fleet.md` "Remote
  Claude worker (claude-acp) end-to-end". The local / sandboxed
  `claude-acp` shim is unsupported — no pty under the host sandbox, and the macOS
  Keychain credential is unreachable over a non-interactive ssh — and is
  intentionally not used; the two local surfaces above cover local Claude work.

These Claude surfaces take no capacity lease and leave no dispatch-ledger entry,
and they run on the orchestrator's own provider, so do not route heavy code
fan-out through them (the bypass regression in the skill's Hard Invariants).
Code-writing still defaults to the sub-billed CLI workers.

## Worker/controller candidates

You, the reader, are already the orchestrator — there is no orchestrator to pick
at dispatch time. The per-chunk routing decision is the **Worker** column; the
**Controller-host** column is an install/handoff fact (which hosts CAN run this
skill as the controller, e.g. on a remote node or after a session move).

Treat routing candidates as first-class only after their readiness gate passes:

| Candidate | Controller-host capable (install fact) | Worker routing (per-chunk) | Readiness gate |
|---|---|---|---|
| Codex | yes | yes | Desktop/CLI available when needed, context-mode registered for large-output work, ACP handshake passes for structured dispatch. |
| Cursor | yes | yes | Cursor Desktop or CLI path present for the controller-host role; `cursor-agent` present and ACP handshake passes for worker use; model-currency probe is current or explicitly accepted as stale. |
| Grok | yes | read-only analysis/research only until write probe passes | Grok Build/headless flags present; structured ACP path passes before ACP dispatch; bash-tail is fallback-only and must obey the marker limits in Composition rules. File-writing is not routable unless `goalflight_doctor.py --worker-write-probe --write-probe-agent grok-code` passes in the current environment. |
| Moonshot (Kimi CLI) | worker only | yes, bash-tail coding | `kimi` is on PATH or executable at `~/.kimi-code/bin/kimi`; OAuth is ready; bare `-p` write probe passes. The `moonshot` preset resolves the off-PATH fallback, changes to the requested cwd, and emits text markers without `--auto`/`--yolo` (both conflict with print mode). |
| OpenCode | yes | helper/raw passthrough only | `opencode` on PATH; host-specific helpers under `scripts/hosts/opencode/` and live smokes in `tests/bash/test-opencode-*`; raw `goalflight_dispatch.py -- <cmd>` passthrough is allowed when the caller owns the command contract. Not a `goalflight_dispatch.py --agent opencode` preset. |
| Claude compatibility path | yes | yes | Adapter-owned CLI/plugin probes pass; startup gate applies where the adapter requires serialized initialization. |

If a candidate has static adapter capability but fails local readiness, do not
route work to it. Pick another ready candidate with equivalent concern coverage
or fall back to the legacy watcher when no ACP path is locally ready.

Grok-specific write guard: treat `grok-code`/`grok-research` as inline
review, analysis, and research workers unless the current machine has passed the
doctor write-file e2e probe. A grok worker that exits cleanly after writing a
target file but emits no final terminal marker is still not a valid file-writing
worker for Goal Flight; route write chunks to codex or another marker-reliable
worker.

Unknown ACP commands are denied by default. Add a checked-in adapter manifest or
point `GOALFLIGHT_ADAPTERS_DIR` at a machine-local manifest directory for
experiments; do not silently dispatch an unmanifested binary.

## Launch discipline

Each parallel chunk gets exactly one launcher process and one unique
`--dispatch-id`. Do not run a sequential shell loop that starts dispatch A, waits
for a synchronous `--foreground` launcher to finish, then reuses the same id for
B/C. Launch each dispatch independently and assign stable ids per chunk
(`chunk-a`, `chunk-b`, `chunk-c`). Direct dispatch already returns after launch
by default. The dispatcher refuses a reused id while the prior ledger record is
non-terminal; a duplicate id means status, tail, and lease ownership would
collide.

Shared-tree code writers that run the full suite (`pytest tests/` or equivalent)
are serialized. Concurrent code-writing is only for file-disjoint chunks whose
focused tests do not mutate or sweep the whole shared tree. If two chunks both
need full-suite verification, run them one after the other or isolate them in
separate worktrees and merge through the normal review gate.

## Liveness — a quiet worker is not a dead worker

Event/tail silence alone is NOT a wedge signal. A healthy worker grinding a long
test or compile can emit zero ACP events (or zero tail bytes) for tens of
minutes; treating that as a timeout false-positives it into a retry storm. Tail
bytes are a proxy, and the capture redaction filter buffers until a newline (it
now also flushes on a size or time bound), so a busy worker can look idle
exactly while it is working. After the idle window the runner and watchers
measure whether the worker is actually working:

- The ACP runner (`goalflight_acp_run.py`) writes a *progressive* status JSON
  (`starting → handshaking → running`) and runs a concurrent heartbeat task that
  samples pgroup-CPU every `--heartbeat-interval` seconds (default 15s; env
  `GOALFLIGHT_HEARTBEAT_INTERVAL`). When the ACP stream goes silent past the
  idle window, the runner checks pgroup-CPU *before* cancelling: **CPU > epsilon
  ⇒ `running_quiet`, keep waiting; CPU ≈ 0 ⇒ wedged, cancel.** A busy-but-quiet
  worker is never killed; a genuinely stuck one still is.
- The watchers (`goalflight_watch.py`, `watch-dispatch-tail.sh`) apply the same
  CPU rule to bash-tail dispatches, **and they also refuse to idle-timeout a
  worker that still has live descendants**. A quiet parent whose pytest/compile
  child is sleeping or I/O-waiting is 0% CPU and tail-silent; killing it is the
  wrong default. `goalflight_watch.py` additionally treats a distinct
  `--worker-cwd` whose files were written inside the idle window as work.
  Unavailable CPU still fails open; a single failed sample is never a wedge —
  the runner re-samples and the watchers require consecutive samples.
- Heartbeats are **runner-written FILES, never task-notifications.** The
  orchestrator is woken only on an actionable transition (completion / wedge /
  blocked), never per beat — a per-beat wake would re-process the orchestrator's
  whole cached session (ruinous).
- `goalflight_status.py` is authoritative for liveness. Raw `*.status.json`
  files are the watcher heartbeat and terminal-write surface, but controller
  decisions that ask "is this dispatch alive?" must use the aggregate status
  command because it cross-checks PID plus process identity and catches stale
  false-alive JSON.
- Bash-tail dispatch holds a macOS-scoped power assertion with
  `caffeinate -dimsu -w <worker-pid>` when `caffeinate` is available. This
  reduces App Nap/display-idle suspension while the worker exists. It is not a
  correctness oracle: user sleep, forced termination, resource pressure, and
  external process kills can still stop work, so status and controller
  re-verification remain required.
- Failure mode: a worker may complete code edits, emit its terminal marker, then
  lose a long low-output verify run. Treat that as idempotent. Worker prompts
  should make code completion independent of verify survival: if verify is
  killed, return the marker with enough detail for the controller to re-run the
  focused or full verify itself.
- **Handshake retry-once**: if the handshake (`initialize`/`session_new`) stalls
  — the intermittent codex-acp wedge, where the worker spawns but never answers
  even though the handshake works in isolation — the runner kills + respawns the
  worker and retries the handshake once before falling back. The wedged worker is
  always reaped first (never retry while an identity-matched PID is still alive).
- **The heartbeat *acts* (the active backstop, not just a status file).** Beyond
  the idle-path CPU check above, the concurrent heartbeat kills + finalizes a
  worker on a *confirmed* wedge even when `--idle-timeout 0` disables the idle
  gate. A "dead sample" requires ALL of: PID alive, pgroup-CPU ≤ epsilon, event
  count unchanged since the last beat, and zero outstanding tool calls;
  `--wedge-samples` consecutive dead samples (default 4) are required before the
  kill, so a transient `ps` failure or a momentary lull cannot false-positive.
  Terminal state `wedged`. `--max-quiet-s` (default 3600s) is a second wall for a
  CPU-busy worker that emits no events at all.
- **Tool-call grace + stall detection + a coarse per-tool wall.** A worker that
  emits a `tool_call` (web search, a long test) then goes silent is I/O-bound at
  ≈0% CPU — indistinguishable from a wedge by CPU alone. While a tool is
  outstanding the dead-sample rule is suppressed (it is legitimate work).
  **`--progress-stall-s` (default 300s) is the operative stuck signal** — it
  kills when standard progress events go quiet, even if raw vendor noise
  continues. Tune it for the worker's expected quiet pattern.
  **`--max-tool-s` (default 3600s, the harness clamp) is a coarse safety net**
  for one outstanding tool: activity-naive wall-clock. Lower it only for
  known-fast tasks; do not use it as the primary stall detector. Terminal state
  `tool_timeout` when the wall fires.
- **Oversized ACP frame.** An ACP frame larger than the asyncio stream limit no
  longer hangs the reader: the guarded reader drops the over-limit newline frame,
  increments the ACP dropped-frame counter, logs it, and continues. Oversized
  notifications are skipped. If an oversized response is dropped, the pending
  request falls through the existing idle/timeout failure path; no
  `result_too_large` terminal state is emitted for new runs.
- **StartupGate for fragile adapters** (`scripts/goalflight_startup_gate.py`).
  Some adapters starve each other during startup, not steady-state — the Claude
  TUI adapter blows its hardcoded 120s per-turn timeout on a trivial turn when
  several spawn at once (TUI init: hooks/LSP/keychain/auto-memory/MCP). The gate
  serializes the spawn→handshake window per agent via an `flock`. It is
  *handshake-gated, not a fixed stagger* — the next worker starts the instant the
  previous one finishes its handshake, on any machine (no interval baselined to
  one laptop). Default serializes the Claude TUI adapter only (env
  `GOALFLIGHT_SERIALIZE_STARTUP`); fail-open after 600s so a stuck holder cannot
  deadlock the fleet; concurrent *turns* stay parallel.

`wedged` and `tool_timeout` are active ACP terminal lease states — the capacity
gate below frees and prunes the slot the same as `complete`/`failed`.
`result_too_large` is retained only as a legacy pruning state for old 0.4.3
records.

## Worker permissions and context-mode over ACP

A spawned worker's permissions resolve **inside the runner subprocess**, not at
the orchestrator. `goalflight_acp_run.py` answers every `session/request_permission`
itself via `auto_allow_tools=True` (default). The orchestrator is never in the
per-tool permission loop and **cannot be asked to approve a tool call in real
time**. The only worker→orchestrator escalation channel is the text markers
`USER-NEED:` / `USER-CONFIRM:` (`worker-markers.md`): a worker that needs a human
decision stops and emits one; the orchestrator relays it.

Three separate layers can affect a spawned worker. Do not conflate them:

1. **Goal Flight OS sandbox** — `goalflight_acp_run.py --os-sandbox read-only`
   or `--os-sandbox workspace-write` wraps the ACP worker subprocess in the host
   OS sandbox where available. On macOS this is `sandbox-exec`; unsupported
   hosts fail closed with `blocked_os_sandbox` before capacity is acquired.
   `read-only` permits file reads, temp writes, and the worker CLI's own
   host-state directory (for auth/session/cache); `workspace-write` also permits
   writes under `--cwd`. This is the real process/file fence for ACP workers;
   adapter CLI flags remain adapter-specific policy knobs.
2. **codex sandbox + approval policy** — useful for the codex exec/bash-tail
   path and shell approvals. Open it with `--sandbox workspace-write -c
   approval_policy=never` (the classifier-safe form of "full permissions").
   `--dangerously-bypass-approvals-and-sandbox` is rejected by some orchestrators'
   auto-mode safety classifiers and is unnecessary when the worker's edit scope
   is its workspace.
3. **MCP elicitation** — raised by tool-level user-input request handlers such as
   context-mode's `ctx_index`. NOT a filesystem sandbox or approval-policy matter, so the first
   two layers do nothing for it. Left unhandled, codex-acp neither forwards nor
   rejects the elicitation over ACP and the tool call wedges at ~0% CPU until the
   per-tool wall.

**Controller's own auto-mode classifier busy (transient — a 4th, controller-side condition).**
Distinct from the three worker layers above: when the orchestrator's *own* Bash returns
*"… temporarily unavailable, so auto mode cannot determine the safety of Bash"*, the Anthropic
safety classifier that auto-approves the **controller's** tool calls is briefly down — not the
user's limits, not a worker problem, and invisible to every goal-flight script (it never reaches a
worker tail). **First, just retry — the normal controller reflex of re-issuing the call usually
works**, because the classifier typically recovers within seconds; a plain retry-in-place succeeds
most of the time, so don't escalate prematurely. Only if it persists across a couple of retries,
keep moving instead of stalling: (1) use **read-only** tools (Read/Grep/Glob/codedb) — they bypass
the classifier; (2) route any write/exec work to a **sandboxed worker dispatch with auto-approval** —
`--os-sandbox workspace-write` or codex `--sandbox workspace-write -c approval_policy=never` (the
classifier-safe form above) — the worker's OS sandbox is the safety boundary, so the controller's
classifier isn't in the path. Never globally disable safety or reach for
`--dangerously-bypass-approvals-and-sandbox`.

**A codex worker can use context-mode over ACP in auto-mode.** The runner
auto-injects `-c features.tool_call_mcp_elicitation=true` for codex-acp at the
single spawn boundary (`ensure_codex_acp_elicitation`); the elicitation then
arrives as a `request_permission` that `auto_allow_tools` grants, and the tool
completes. So a worker may index/search/execute via context-mode in a normal
auto-mode ACP dispatch — **no `tail -f`, no "disable context-mode for ACP."**
Proven by hermetic tests (`test_acp_pipe.py::case_permission_elicitation_unblocks`,
`::case_codex_acp_elicitation_injection_unit`) and a live codex-acp + context-mode
end-to-end run (index + search, completed clean).

Distinct, and still true: do **not** wrap the *dispatch* or a `tail -f` in
`ctx_execute` / `ctx_batch_execute` (the controller-side caveat in Axis 2 and
`legacy/bash-tail.md`). That is the orchestrator offloading a long-running spawn
into context-mode's bounded-command timeout — unrelated to a worker calling
context-mode tools.

## Composition rules

| Iteration | Comms | Supported | Notes |
|---|---|---|---|
| one-shot | controller-direct | yes | context-held edits, no spawn |
| one-shot | acp | yes | default for any spawned worker |
| one-shot | bash-tail | yes | only when no ACP adapter |
| goal-mode | acp | yes | preferred for main-tree-write loops and non-codex loops; for read-only or worktree-isolated **codex** loops, bash-tail is equivalent + leaner — see below |
| goal-mode | bash-tail | depends on worker | Requires the worker to emit a detectable end-of-goal marker in the flat tail (so the watcher knows the loop is complete). **As of 2026-05-19, codex `/goal` is the only worker known to qualify** — its structured "Final response" block is the marker; see `templates/codex-goal-prompt.md.tpl`. Grok and claude headless do not qualify today; a future worker that grows an equivalent marker contract would join this cell. When the worker doesn't qualify, use one-shot + bash-tail with a coarser chunk instead. |
| goal-mode | controller-direct | n/a | controller-direct is single-turn by definition |

### bash-tail vs ACP for a codex goal-loop

Both transports run codex's **native** `/goal` loop unchanged: the prompt and
`features.goals` config are identical, so codex — not goal-flight's wrapper — drives the
iteration either way (it is NOT a simulated/partial goal-mode on bash-tail).
The transports therefore differ for codex only in that ACP can relay per-tool
permission decisions live (`--interactive`) and
reads terminal state from structured events instead of a tail marker. Default:

| The codex goal-loop is… | Transport | Why |
|---|---|---|
| read-only (review) **or** worktree-isolated | **bash-tail** | no writes → no permission requests → ACP's relay is moot; bash-tail is leaner (no ACP-SDK venv) and `codex exec` is verified not to leak ptys/helpers |
| writing the **main tree**, wanting live per-write gates | **ACP `--interactive`** | inline permission relay where a bad write to the real tree matters |
| a **non-codex** agent (cursor / claude; grok only after write probe passes) | **ACP** | bash-tail goal-mode needs codex's end-of-goal tail marker, which they otherwise lack |

So the "ACP preferred for loops" row above holds for main-tree-write and
non-codex loops; for read-only or worktree-isolated **codex** loops, prefer
bash-tail. (Verified 2026-06-01: `codex exec` headless leaves zero leaked
processes and zero tty delta; `codex-acp` has not been separately confirmed
leak-free, so it carries unknown helper-leak risk the bash-tail path avoids.)

### Worker model selection (`--model`)

`goalflight_dispatch.py --model <id>` (and `goalflight_acp_run.py --model <id>`)
selects the worker model on both transports — bash via `build_worker`, ACP via
`agent_command`. With `--model` omitted, each agent keeps its own default — except
**claude**, which defaults to `opus` (its clear strongest — quality-by-default for
workers; pass `--model haiku` for speed). codex already defaults strong; cursor keeps its own default (strongest is
ambiguous).

**Grok is the exception — do NOT pass `--model` for grok.** The harness selects
grok's model automatically from the agent id, matched to the task, so dispatch
instructions never name it: choose `grok-code` for coding or `grok-research` for
web search/fetch and the correct model is wired in by `build_worker` /
`agent_command`. (Which model maps to which agent is an implementation detail in
`goalflight_dispatch.py`, not an agent-facing knob.)

For the agents whose model you DO choose, the selector is inserted PER-AGENT (the
flag and its position differ — a blind append breaks codex/grok ACP), so pass the
**agent's own id format**:

| Agent | Example | ACP form |
|---|---|---|
| grok-code | `--agent grok-code` (no `--model` — harness picks) | `grok agent stdio` (harness inserts the model) |
| grok-research | `--agent grok-research` (no `--model` — harness picks) | `grok agent stdio` (harness inserts the model) |
| moonshot | `--agent moonshot` (default `kimi-code/k3`) | bash-tail only; explicit `--model <id>` passes through |
| claude (speed) | `--agent claude --model haiku` | `claude-code-cli-acp --model <id>` |
| codex | `--agent codex --model o3` | bash `codex exec --model <id>`; ACP `-c model=<id>` |
| cursor | `--agent cursor --model sonnet-4` | `cursor-agent --model <id> acp` (best-effort) |

grok/codex/claude placements are verified; cursor is best-effort (its ACP arg
position is not separately confirmed). OpenCode model selection belongs to the
host-specific helper or raw passthrough command, not `goalflight_dispatch.py
--agent opencode`. Bare `--agent grok` is retired — use `grok-code` or
`grok-research` and let the harness pick the model. Web-research-looking prompts
on `grok-code` are bounced with a hint (composer can't drive web tools — use
`grok-research`, or `--web-research-ok` to override a false positive).

### Tier routing — scout-carried, class-based

Frontier effort is the default for architecture-class work and a waste on
mechanical work. Route by chunk class, and let the scout verdict carry the
recommendation — the scout has just read the surface and is the best-positioned
judge of whether a chunk is genuinely mechanical:

- **CP/H class** (architecture, cross-cutting invariants, non-obvious
  algorithms, anything whose failure is silent): codex at its strong default.
- **Stitch class** (pin bumps, fixture moves, mechanical repairs, rename
  sweeps, well-specified single-surface fixes): route to `grok-code`. A tightly
  specified brief is the load-bearing input; grok's weaker self-review is
  covered by the landing review chain regardless.
- When in doubt, the scout says so and the chunk gets the strong tier —
  misrouting a load-bearing chunk down costs more than misrouting a stitch up.

Tier the BRIEF with the worker: a strong-tier brief explains judgment calls; a
stitch brief is a spec with acceptance checks. Safety and process invariants
(read-only defaults, `BLOCKED:` escalation, marker vocabulary) are never
tier-gated.

## Checkpointed dispatches — steer, don't re-dispatch

Every fresh dispatch re-pays orientation (tens of thousands of tokens of
AGENTS/plan/contract reads) before it writes a line. Where the transport keeps
a session alive (ACP steers; any host with a working steer channel), prefer
**one worker, checkpointed**, over a chain of fresh workers:

1. **Design checkpoint** (CP/H-class chunks): the worker's first deliverable is
   the design + contract — interfaces, invariants, acceptance checks, test
   names — written to the lane's research directory. It emits
   `USER-NEED: design ready for approval at <path>` and pauses. The controller
   reviews (optionally with a read-only review flight), then steers back
   approval or corrections. The worker builds only after approval, so a wrong
   design costs a steer, not a build.
2. **Landing checkpoint**: when the worker believes the chunk is done, it
   reports `RESULT: gate=deferred-to-controller` and its focused-suite results,
   then **exits** on a non-success terminal that carries the resume handle:

   ```text
   USER-NEED: landing checkpoint — focused suites green; full gate +
   independent review deferred to controller; session <resume-id>; log <path>
   ```

   The gate-deferred line is a status record, never a terminal, and never a
   success claim; the terminal marker stays within the worker-markers
   vocabulary, and a `COMPLETE:` here would read as landable before the gate
   ran. The controller then runs the full gate and the landing review chain
   (the chunk-review floor: two or more concern-diverse legs, to a clean
   round), and delivers findings as a resume-prompt revision. Revisions land
   in a context that already holds the surface — no re-orientation, no fresh
   ramp.

This is also the correct home for the review work that used to run as in-loop
independent fleets: the worker's own self-review stays; the independent floor
runs at the checkpoint, controller-side. **Landing still requires both** the
controller full gate green and the converged review floor — the checkpoint
relocates the reviews, it does not discount them.

Three delivery forms, in order of preference by situation:

- **Session resume — the default for revisions where the transport has a
  durable, addressable session.** The worker EXITS at the checkpoint; nothing
  is parked, no lease is held, no liveness machinery sees an idle process.
  When the controller has revisions, it resumes the recorded session with the
  revision list as the new prompt — the context comes back from disk with the
  surface already loaded. Verified form today (codex ≥0.144.5), two field-
  learned mechanics included:

  ```shell
  CODEX_HOME=~/.goal-flight/dispatch-homes/<home-owner-dispatch-id> \
    codex exec --sandbox workspace-write -c approval_policy=never \
    resume <session-id> - < revisions.md
  ```

  **Resume survives seat rotation — do not pin seats to preserve it.** A codex
  rollout is local transcript state, not server-side account-bound state, so
  ANY valid codex auth can resume ANY rollout. Verified 2026-07-28: a session
  created under one seat, resumed under a home authenticated as a different
  seat, returned its full prior context and answered a question only the
  earlier conversation could answer. The resume therefore needs the rollout
  FILE, not the original seat — rebuild the dispatch home with the rollout
  intact and whatever seat is currently healthy.

  Seat continuity is worth a little, but only a little: staying on the same
  seat may keep the vendor-side prompt cache warm (short-lived, on the order of
  minutes), while a lost session costs the entire accumulated context. So
  prefer the original seat when it is healthy and free, and never refuse,
  block, or delay a resume to obtain it. Falling back to the current seat is
  always correct.

  Three mechanics, each of which fails in a way that looks like something else.
  **Stdin must reach EOF**: `codex exec` reads stdin to EOF even with a
  positional prompt, so a dangling stdin hangs the resume and reads as a
  stalled worker. Either satisfies that — `< /dev/null` when the prompt is
  positional, or `resume <id> -` with stdin fed from the prompt FILE, which is
  what tooling should do because a long revision list (gate failures plus
  review findings) passed positionally hits argv truncation. The requirement is
  EOF, not the literal `/dev/null`. Flags go BEFORE the `resume` subcommand — appended after it they are
  parsed as part of the prompt. And the rollout lives under the PER-DISPATCH
  `CODEX_HOME` (`~/.goal-flight/dispatch-homes/<home-owner-dispatch-id>`), not the global
  `~/.codex` — a resume without that env var looks in the wrong home and
  cannot find the session. The resume handle is the session UUID codex prints
  at startup (also the rollout filename under that home's `sessions/` tree);
  harvest it into the dispatch collateral at checkpoint time — resolving it
  later via `--last` guesses, and guesses resume the wrong session. Do not assume other agents can resume until their
  form is verified; for them, use warm-steer or the fresh-dispatch fallback.
  Condition: the session must not be near its context ceiling — resuming an
  overfilled context buys back orientation but leaves no room to work, and a
  compaction-mangled session is worse than a fresh one. Long noisy runs are
  better re-dispatched with the durable artifact folded in.
- **Warm steer** (ACP and other live steer channels): keep the worker alive
  through the checkpoint only when the controller will respond within its own
  watch cadence — a paused worker holds a capacity lease and looks idle to
  liveness machinery. Never park workers overnight; exit-and-resume instead.
- **Fresh dispatch + durable artifact** (fallback, and the only form that needs
  no transport support): the design/contract artifact is written to the lane's
  research directory at checkpoint time in every form, so falling back loses
  the warm context but never the decision. The artifact, not the context, is
  the part that must survive.

**Session as lane cache.** On large domain codebases (hundreds of kLoC of
physics/chemistry/engineering), the dominant per-dispatch cost is not the
briefing reads — it is the worker loading enough of the domain surface to edit
it safely. That understanding is exactly what a resumed session preserves, so
consecutive chunks on the SAME surface may chain through one resumed session
instead of each re-orienting from zero. Bounds, all load-bearing:

- same lane and surface only — a session oriented on the RF kernel is not a
  discount on the thermal solver, and scope bleed between chunks is a real
  failure mode;
- the session's memory of the tree goes stale exactly like a prompt does:
  every resume prompt MUST open with a fresh `SCOUTED STATE` block (current
  HEAD, what landed since, dirty paths) — the worker's recollection of the
  tree is a lead to verify, not ground truth. Do not resume across a rebase,
  reset, or another lane's force-land without a re-scout, and a dirty tree
  left by a prior non-landed chunk is not a free cache — reconcile it first;
- watch the fill: retire the session and start fresh well before the context
  ceiling — a compaction-mangled session silently loses exactly the standing
  rules you were reusing it for;
- the session is a cache, never a store of record — durable artifacts and
  markers are written per chunk as if each were a fresh dispatch.
- Scout relationship: scouting verifies premises cheaply and authors the brief;
  the design checkpoint is for chunks whose *shape* is still open once the
  premises are true. A scout report whose DECISION EXTRACTION lines surface
  implicit architecture choices is the trigger to promote a chunk from
  plain-scouted to design-checkpointed.

### Cursor's agent surface: Kimi and Grok

`cursor-agent` carries several vendors' models, so choosing one here is a
routing decision rather than a strength claim. The two wanted on this lane:

```shell
--agent cursor --model kimi-k3-high            # Kimi K3
--agent cursor --model cursor-grok-4.5-high    # Grok 4.5
```

Pass the id explicitly. goal-flight deliberately pins no default model for any
agent — a pinned id goes stale and stops tracking the CLI's own default, which
is why the grok ACP pin was retired; cursor is not exempt from that.

The Kimi default is a **hosting** decision, and the reason must survive: Kimi's
own CLI sends prompts and repository contents to its vendor's service, while
cursor serves the same model family from US infrastructure under the cursor
account. Same model, different jurisdiction for the data. Anyone tempted to
"simplify" by routing Kimi work back through the direct CLI is trading that
away, not removing a redundant hop.

Model ids on this surface change without notice — confirm against
`cursor-agent --list-models` before pinning a new one.

**Untrusted-directory gotcha:** in a directory cursor has not been trusted for,
`cursor-agent -p` stops for a trust decision and **exits 0 with no result** — a
dispatcher scores that as success with an empty tail. Pass `-f` for dispatched
runs.

`--agent moonshot` is the DIRECT Kimi CLI (bash-tail transport, its own
account and sandbox rules); it is a different route to the same model family,
not an alias of the cursor lane. The handle was `kimi` before the moonshot
rename — old ledgers and status files still carry `agent: "kimi"` and are
read as the moonshot family; new dispatches under the retired handle fail the
normal unknown-agent error.

### Composer-class routing: prefer grok over cursor (operator steer 2026-06-11)

For composer-2.5-class coding work, default to the grok lane and route to
**cursor only when the chunk explicitly needs the cursor vendor harness**
(cursor-internal models/features). Transport split follows this doc's
transport rules, not co-equal choice:

- **`grok-acp`** for goal-loop / code-writing chunks (non-codex goal-mode
  requires ACP per the transport table above — bash-tail goal mode is
  codex-only).
- **`grok-code`** (bash-tail) for one-shot read-only work: reviews, hunts,
  inline-return verdicts.

Scope note: this demotes CURSOR; it does not displace codex, which remains
the overall code-writing default in the SKILL.md Worker Routing table. The
grok lane is the high-capacity second executor lane (pool cap 30 vs
cursor's 3 in `scripts/goalflight_agent_limits.py`, imported by
`scripts/goalflight_capacity.py`).

Why grok over cursor here:

- **No unattended gates.** `cursor-agent` carries editor-derived workspace
  trust (one operator-present round per NEW project root or the dispatch dies
  at a transport USER-CONFIRM in seconds) plus a global approval allowlist
  that can kill runs mid-task on any unlisted command. The grok harness has
  neither.
- **Same model class**, lighter harness, larger pool cap.

**The pairing that must not be lost in the switch:** cursor's approval system
acted as a write gate; grok does NOT gate writes in auto mode (the dispatch
warning fires). For write-capable grok dispatches, pair `--os-sandbox`
(`workspace-write` when commits are expected); reviews stay `--read-only`.
See `docs/acp-push-gate-matrix.md`.

## Capacity gate

Before spawning any worker, acquire a machine-global lease:

```bash
python3 <skill-root>/scripts/goalflight_capacity.py acquire \
  --agent <agent> \
  --project-root "$PWD" \
  --dispatch-id <id>
```

If decision is `wait`, do not spawn. Use another agent only if the concern
coverage remains valid.

Machine-local caps may override the documented defaults — the override path and
merge rule are a shared-core fact (the `SKILL.md` Hard caps section; source:
`scripts/goalflight_agent_limits.py`). Check them before reasoning from defaults.

### Priority lanes (`--priority {critical,normal,bulk}`)

Acquire is single-shot try-or-block (no queue), so a burst of batch retries can
statistically crowd out an urgent fix dispatch. Lanes reserve headroom instead
of queueing — pass `--priority` on `goalflight_dispatch.py` (threaded through
to acquire) or on `acquire` directly:

- **bulk** — review storms / batch sweeps. May not take the last 3 machine
  slots nor the last pool slot; bulk work backfills as the queue lightens.
- **normal** — default; unchanged legacy behavior.
- **critical** — fix-the-blocker dispatches. May borrow 2 slots beyond the
  operating cap and 2 beyond the pool cap (never past the RAM raw ceiling;
  pool borrow is disabled while adaptive rate-pressure is active — provider
  pushback always wins).

Convention: controllers SHOULD tag review storms `bulk` and reserve `critical`
for work that unblocks other work. `capacity.py status` shows non-normal lanes
as `prio=<lane>` on the lease line.

### Capacity queue (`--capacity-wait-s`)

`goalflight_dispatch.py` QUEUES for a slot: it re-attempts acquire every ~15s
(jittered) until a slot frees or the budget lapses — no controller re-dispatch
loop needed. Defaults by lane: bulk 900s / normal 600s / critical 120s
(critical is short because it borrows headroom — if IT blocks, the machine is
truly full). `--capacity-wait-s` overrides; `0` = legacy instant
DISPATCH-BLOCKED; `GOALFLIGHT_CAPACITY_WAIT_S` env is a test/emergency
override (an explicit CLI flag still wins). The deadline runs on the
sleep-excluding clock (a lid-close does not burn the window). While queued the
dispatch is fully visible: status `waiting_capacity`, ledger classification
`queued_capacity`, `--done` reports LIVE, `CAPACITY-WAIT` lines on the
launcher tail, and the dispatch-id is reserved (duplicate ids refused).
Killed mid-wait -> terminal `blocked_capacity (wait_interrupted)`.

Fairness honesty: this is contention polling, NOT FIFO — a newcomer can win a
freed slot ahead of a longer waiter. Lanes handle PRIORITY; the deadline
bounds the damage; ticket-FIFO is the named rung if sustained saturation ever
makes starvation real. (ACP-shape and review_job acquires are still
single-shot — parity is a known follow-up.)

### Pause a vendor until its limit resets (`cooldown`)

When a provider is at or near a budget limit (its 5-hour window, weekly cap, or
credit balance) and you want to stop feeding it doomed launches until the window
resets — rather than letting `drain` spam 429s into the abyss — set a per-agent
cooldown:

```bash
# pause new launches for a vendor (auto-expires after --seconds):
python3 <skill-root>/scripts/goalflight_capacity.py cooldown set \
  --agent <codex|claude|grok> --seconds <secs-until-reset> \
  --reason "5h limit ~X% left, resets ~HH:MM"

# resume early once the window resets (or just let it auto-expire):
python3 <skill-root>/scripts/goalflight_capacity.py cooldown clear --agent <codex|claude|grok>
```

Behaviour — non-destructive and self-healing:
- New acquisitions for that agent are refused until the cooldown expires. A
  `drain` still claims and replays each queued entry, but the capacity acquire
  **blocks on the cooldown** (`decision=wait`) and the entry is **restored to the
  queue** — so nothing new launches for that vendor. **In-flight workers are not
  killed** — they finish.
- Queued `--submit` entries are **held in the durable queue, not lost** (claim →
  acquire-blocks → restore); the instant the cooldown clears or expires, the next
  `drain` relaunches them (idempotent by dispatch-id — no double-launch, no
  re-bounce).
- Add margin past the nominal reset (e.g. +10 min) so launches don't resume
  exactly at the boundary and immediately re-exhaust.
- Cooldown is **machine-local, file-backed runtime state** (`capacity.json` in
  `GOALFLIGHT_STATE_DIR` / the default state dir, lock-guarded) — NOT repo state,
  so there is nothing to commit. It pauses that vendor for every dispatcher
  sharing that state dir — which is the point for a shared provider budget.
  Reroute vendor-agnostic work to another vendor meanwhile.

This is the **manual** limit lever. It complements the **reactive**
`rate_pressure` walk-back (auto-reduces caps AFTER clustered failures) and is the
manual precursor to a **proactive** usage gate (auto-cooldown keyed off real
provider utilization %). Use `cooldown` when you already know a window is tight;
let `rate_pressure` catch what you don't.

## Ledger

After spawn, record PID and prompt:

```bash
python3 <skill-root>/scripts/goalflight_ledger.py record \
  --dispatch-id <id> \
  --agent <agent> \
  --transport <acp|bash-tail|file-backed-review> \
  --worker-pid "$WORKER_PID" \
  --prompt-path <prompt.md> \
  --status-path <status.json>
```
