# Bash-tail dispatch (legacy)

The bash-tail shape spawns a worker headlessly with stdout/stderr to a file,
then watches the file with a marker-grep loop. ACP carries turn boundaries,
tool-call locations, and stop reasons as structured events; bash-tail loses
all of that — but bash-tail remains the canonical recipe for **codex
`/goal` mode** (see `templates/codex-goal-prompt.md.tpl`).

**One-shot + bash-tail — works for ANY worker.** This is the common
bash-tail case. The worker emits the standard terminal markers
(`COMPLETE:` / `BLOCKED:` / `USER-NEED:` / `USER-CONFIRM:`) and the
watcher greps for them; `STATUS:` / `RESULT:` lines stream as progress in
between. codex, grok, claude, and cursor headless all do this fine. Nothing
about bash-tail is goal-mode-only — a one-shot task that emits a status
update or two and a final `COMPLETE:` is exactly what the watcher is built
for.

**Goal-mode + bash-tail — codex `/goal` only.** This is the constrained
case. Goal-mode runs many turns, so the watcher needs a distinct
end-of-*loop* signal — not just the end-of-*turn* terminal markers above.

- **codex `/goal`** — works. Codex emits a structured "Final response"
  block at the end of the goal, giving the watcher the end-of-loop signal
  in the flat tail. Empirically verified. (See
  `templates/codex-goal-prompt.md.tpl`.)
- **grok / claude headless in goal-mode** — does not work. No equivalent
  end-of-loop signal; the watcher can't tell when their iteration is done.
  This is a *goal-mode* limitation only — one-shot + bash-tail works fine
  for them (see above). For their iterative work, use one-shot + bash-tail
  with a coarser chunk, or switch to ACP if the adapter is available.

## Recipes

> ⚠️ **Leak warning (tty/process).** These raw `... &` recipes background the
> worker with a bare shell `&` and track only `$!`. They do NOT register a
> pidfile or place the worker in its own reapable process group, so
> `cleanup_ghosts` cannot find them and any helper processes the worker leaves
> (e.g. codex's tty-using helpers, which reparent to launchd) leak until reboot.
> **Prefer the tracked path** — `scripts/goalflight_dispatch.py` (crash-safe:
> `start_new_session` group + pidfile + decoupled watcher + dead-worker group
> reaping). Use these raw recipes only for throwaway manual runs you reap
> yourself.

Worker invocations. All three drop a tail file the watcher polls. Replace
`<slug>` and `<workdir>` per chunk.

### codex (headless one-shot or `/goal` mode)

```bash
codex exec \
  --skip-git-repo-check \
  --sandbox workspace-write \
  -c approval_policy=never \
  -C "<workdir>" \
  - < <prompt.md> \
  > /tmp/codex-<slug>.txt 2>&1 &
WORKER_PID=$!
```

Codex reads the prompt from **stdin** (`- < <prompt.md>`); there is no
`--prompt-file` flag. The `templates/codex-goal-prompt.md.tpl` template
documents the canonical `/goal` mode invocation including the
`features.goals = true` config prerequisite.

- `--skip-git-repo-check` — codex refuses non-interactive runs in non-git
  directories by default. Worktrees under `.claude/worktrees/` are git
  repos and don't need this; chunks dispatched into `/tmp/` or other
  non-git workspaces do.
- `--sandbox workspace-write -c approval_policy=never` — required for non-
  interactive operation. Codex's permission UI is a stdin TTY and would
  block forever on `codex exec` without `approval_policy=never`;
  `workspace-write` grants in-workspace edit authority. Do NOT use
  `--dangerously-bypass-approvals-and-sandbox`: it drops the sandbox
  entirely and is rejected by some orchestrators' auto-mode safety
  classifiers. See the safety story below.

### grok (headless one-shot)

```bash
grok -p "<prompt>" \
  --cwd "<workdir>" \
  > /tmp/grok-<slug>.txt 2>&1 &
WORKER_PID=$!
```

Pass **no** `--permission-mode` flag. grok 0.2.39 (verified 2026-06-10 with both
`-p` and `--prompt-file`) regressed so that in single-turn mode **every**
`--permission-mode` value stops the file-write tool from writing — none produce
the file; only omitting the flag does. The empty-no-op values (`default`,
`acceptEdits`, `auto`) leave the worker exiting 0 with an empty tail, which the
watcher records as `worker_dead_no_terminal_marker`; `dontAsk` is worse — under
`--prompt-file` it printed a normal completion marker yet still skipped the write.
Historically `--permission-mode acceptEdits` was used here as grok's equivalent of
the codex bypass; that flag now breaks edits rather than enabling them. See the
regression note in `scripts/goalflight_dispatch.py` (`build_worker`, grok preset).

### claude (headless one-shot)

`claude -p` has no `--cwd` flag; it inherits the caller's working
directory. Wrap in a subshell to set the worker's cwd:

```bash
(cd "<workdir>" && claude -p "<prompt>" \
  --output-format stream-json \
  > /tmp/claude-<slug>.txt 2>&1) &
WORKER_PID=$!
```

If the worker needs access to additional directories outside `<workdir>`,
use `--add-dir <path>` (each path becomes accessible to the session).

**Billing**: `claude -p` produces **API billing**, not session billing.
Prefer the native subagent path for sub-billed dispatches — native subagents
share the orchestrator's session budget but make their own LLM calls, so they
also share the orchestrator's rate-limit budget. Reserve `claude -p` for cases
where you need a clearly delimited headless run outside the parent
session's context window AND you are willing to pay API rates.

### opencode (HTTP bash-tail one-shot)

Bare `opencode run` can hang in headless environments (snapshot cleanup,
DB contention with `opencode serve`). Use the Goal Flight HTTP helper
instead — it writes Goal Flight markers directly to the tail file:

```bash
python3 <skill-root>/scripts/hosts/opencode/bash_tail.py \
  --directory "<workdir>" \
  --tail /tmp/opencode-<slug>.txt \
  --prompt-file <prompt.md> \
  --model litellm/frontier-coder \
  > /tmp/opencode-worker-meta-<slug>.txt 2>&1 &
WORKER_PID=$!
```

The script starts `opencode serve` if needed, sends the prompt via
`POST /session/{id}/message`, streams `STATUS:` lines and the assistant
reply into `--tail`, then writes `COMPLETE: true` or `BLOCKED: ...`.

Copy project `opencode.json` into `<workdir>` when testing outside the
repo (set `"snapshot": false` on large trees). Requires LiteLLM env
(`LITELLM_API_KEY` or `source ~/.config/rpp/litellm.env`).

For structured turn boundaries and tool events, prefer `opencode acp`
(see `adapters/opencode.json` and `docs/hosts/opencode.md`).

## Watcher

Once spawned, watch the tail file with the dispatch watcher:

```bash
bash <skill-root>/scripts/watch-dispatch-tail.sh \
  --pid "$WORKER_PID" \
  --tail /tmp/<agent>-<slug>.txt \
  --controller-pid $$ \
  --agent <codex-bash-tail|grok-bash-tail|claude-bash-tail|opencode-bash-tail> \
  --session-id <slug> \
  > /tmp/watcher-<slug>.txt 2>&1 &
WATCHER_PID=$!
```

Exit codes:

| Code | Meaning |
|---|---|
| 0 | terminal marker (`COMPLETE` / `BLOCKED` / `USER-NEED` / `USER-CONFIRM`) |
| 1 | worker PID died without a terminal marker |
| 2 | tail file idle past `--max-idle-secs` (direct watcher default 180s; dispatch wrapper default 600s for write-capable code workers) — worker likely wedged |
| 3 | orchestrator PID died — watcher self-detected orphan |

The watcher registers a pidfile under `/tmp/goal-flight-acp-pids.d/` so
`cleanup_ghosts()` (defined in `scripts/acp_client.py`; grep for the
function name — line drifts over time) reaps orphaned workers uniformly
across ACP and bash-tail paths. Filename pattern:
`<controller-pid>.bashtail.<worker-pid>.jsonl`.

When launching multiple bash-tail dispatches, do not use a sequential
`for c in A B C; do goalflight_dispatch.py ... --foreground; done` loop with
reused ids. Each parallel chunk must get its own `--dispatch-id`; otherwise B/C
collide with A's status/tail/ledger record. Direct dispatch returns after launch
by default, so use status tooling for joins instead of blocking the launcher.
The dispatch wrapper refuses reused ids whose prior record is still
non-terminal.

## Do NOT route the dispatch or the tail through context-mode

It is tempting to run the `codex exec ... &` spawn or a `tail -f <tail>`
inside `ctx_execute` / `ctx_batch_execute` to keep output out of the
orchestrator's context. **Don't.** context-mode is a *bounded-command*
tool — its timeout (default ~120s) exists to hand control back to the
orchestrator when a command hangs. A worker spawn is intentionally
long-running (minutes–hours); a `tail -f` is intentionally infinite.
Both trip the timeout and get killed mid-run — the worker orphaned, the
tail truncated. The timeout is not a misconfiguration to lengthen; it is
correct for context-mode's purpose, which is exactly why long/infinite
processes don't belong inside it.

Correct split:

- **Dispatch**: native Bash with `&` (as in the recipes above). Never
  wrapped in an MCP tool call.
- **Follow progress**: `scripts/watch-dispatch-tail.sh` or
  `scripts/goalflight_watch.py` — they poll and return, never block.
- **context-mode's job here**: the *bounded* analysis commands that run
  AROUND the dispatch — verify the diff, run the test gate, grep the
  result. Those finish and produce output worth indexing; the worker
  spawn and the tail-follow do not.

## Bypass-flag safety story

The bypass / accept-edits flags grant the worker process full local
authority for the lifetime of the dispatch. Two operating modes apply:

- **Sequential mode** (no `--parallel`): the worker runs in the orchestrator's
  repository root. Trust boundary is the user's machine. The bypass flag is
  acceptable because the orchestrator is already operating with full local
  authority on the user's behalf.
- **Parallel mode** (`execute --parallel <N>`): each worker runs in its own
  git worktree at a path inside `.claude/worktrees/`. Pass `-C "<workdir>"`
  (codex) / `--cwd "<workdir>"` (grok) explicitly, or wrap in
  `(cd "<workdir>" && ...)` (claude — no `--cwd` flag). Without one of
  these, the worker inherits the orchestrator's cwd and can edit files
  outside its worktree, defeating the worktree-as-sandbox boundary.

In both modes the bypass flag is **not** a security boundary. A
compromised or buggy worker can still run anything the user can. The
worktree boundary is a code-organization sandbox, not a security
container. Treat each worker as you would treat a shell you typed the
same commands into yourself — same blast radius, same audit posture.
Goal-flight's 0.4.0+ permission router intercepts
`session/request_permission` events for routing and logging; it never
denies a call the worker has already been granted by its
bypass/acceptEdits flag.

## Why this is legacy

ACP gives the orchestrator:

- discrete `tool_call` and `tool_call_update` events with `locations` arrays
  (used by `_scan_out_of_scope_paths` for scope-leak audit),
- explicit `stopReason` per turn,
- `agent_thought_chunk` and `plan` events that don't pollute the prose tail,
- `session/request_permission` interception (the 0.4.0 permission router
  hook).

Bash-tail loses all four. Migrate workers to ACP as adapters become
available; revisit this file only for parity recipes.
