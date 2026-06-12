# Dispatch Routing Protocol

Choose the smallest execution shape that can finish safely. Routing has two
orthogonal axes: **iteration pattern** (how many turns) and **comms shape**
(how the orchestrator observes the worker). Pick one value from each.

## Axis 1 — Iteration pattern

- `one-shot`: send a single prompt, worker completes the chunk in one turn.
  Default. Use when the chunk has a clear definition of done and fits one
  worker context.
- `goal-mode loop`: worker iterates against a goal across multiple turns,
  either by self-loop (codex `/goal`, Grok Build headless) or by orchestrator
  re-dispatch through the same session. Use when the chunk needs
  review-revise cycles, exceeds one turn, or the worker should keep refining
  until a marker fires.

## Axis 2 — Comms shape

- `controller-direct`: no worker spawned. The orchestrator does the edit itself.
  Use only for tiny local work expected to finish in seconds. If the task
  grows, stop and dispatch.
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

  Cursor tool-use or file-writing chunks need `--permission-mode inline`
  (or `--interactive`, which expands to ACP inline mode). Plain `auto`
  permission mode can surface shell/tool escalation as `USER-CONFIRM` and block
  the worker even though the same chunk completes when inline permission routing
  is active.

  **`--max-idle-secs` gates quiet workers.** Default: 600s for write-capable
  code workers and 180s for read-only, research, or custom workers. The idle
  timeout is the gap between events, NOT total runtime — it resets on every
  event, so a healthy worker emitting periodic STATUS markers never trips it.
  Override with `--max-idle-secs <secs>` (or `--max-idle-secs 0` for no idle
  gate, relying on PID liveness + the worker's terminal marker).
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

## Worker/controller candidates

Treat routing candidates as first-class only after their readiness gate passes:

| Candidate | Orchestrator use | Worker use | Readiness gate |
|---|---|---|---|
| Codex | yes | yes | Desktop/CLI available when needed, context-mode registered for large-output work, ACP handshake passes for structured dispatch. |
| Cursor | yes | yes | Cursor Desktop or CLI path present for orchestrator use; `cursor-agent` present and ACP handshake passes for worker use; model-currency probe is current or explicitly accepted as stale. |
| Grok | yes | read-only analysis/research only until write probe passes | Grok Build/headless flags present; structured ACP path passes before ACP dispatch; bash-tail is fallback-only and must obey the marker limits in Composition rules. File-writing is not routable unless `goalflight_doctor.py --worker-write-probe --write-probe-agent grok-code` passes in the current environment. |
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
for that launcher to finish, then reuses the same id for B/C. Background each
dispatch command independently through the host's background-task mechanism and
assign stable ids per chunk (`chunk-a`, `chunk-b`, `chunk-c`). The dispatcher
refuses a reused id while the prior ledger record is non-terminal; a duplicate id
means status, tail, and lease ownership would collide.

Shared-tree code writers that run the full suite (`pytest tests/` or equivalent)
are serialized. Concurrent code-writing is only for file-disjoint chunks whose
focused tests do not mutate or sweep the whole shared tree. If two chunks both
need full-suite verification, run them one after the other or isolate them in
separate worktrees and merge through the normal review gate.

## Liveness — a quiet worker is not a dead worker

Event/tail silence alone is NOT a wedge signal. A healthy worker grinding a long
test or compile can emit zero ACP events (or zero tail bytes) for tens of
minutes; treating that as a timeout false-positives it into a retry storm. The
runner and watchers use **process-group CPU** as the false-positive killer:

- The ACP runner (`goalflight_acp_run.py`) writes a *progressive* status JSON
  (`starting → handshaking → running`) and runs a concurrent heartbeat task that
  samples pgroup-CPU every `--heartbeat-interval` seconds (default 15s; env
  `GOALFLIGHT_HEARTBEAT_INTERVAL`). When the ACP stream goes silent past the
  idle window, the runner checks pgroup-CPU *before* cancelling: **CPU > epsilon
  ⇒ `running_quiet`, keep waiting; CPU ≈ 0 ⇒ wedged, cancel.** A busy-but-quiet
  worker is never killed; a genuinely stuck one still is.
- The watchers (`goalflight_watch.py`, `watch-dispatch-tail.sh`) apply the same
  rule to bash-tail dispatches: PID alive + pgroup-CPU > epsilon ⇒ `running_quiet`
  (no idle-timeout exit). CPU is summed across the worker's current process
  group, so a quiet parent with a CPU-active test/compile child still counts as
  active. A single failed CPU sample is never read as a wedge — the runner
  re-samples and the watchers require consecutive samples, riding out a transient
  `ps` failure before declaring a wedge.
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
| one-shot | controller-direct | yes | tiny edits, no spawn |
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
permission decisions live (`--interactive` = `--permission-mode inline`) and
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
grok lane is the high-capacity second executor lane (pool cap 14 vs
cursor's 3 in `DEFAULT_AGENT_CAPS`).

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
