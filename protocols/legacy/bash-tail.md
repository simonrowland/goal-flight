# Bash-tail dispatch (legacy)

The bash-tail shape spawns a worker headlessly with stdout/stderr to a file,
then watches the file with a marker-grep loop. ACP carries turn boundaries,
tool-call locations, and stop reasons as structured events; bash-tail loses
all of that — but bash-tail remains the canonical recipe for **codex
`/goal` mode** (see `templates/codex-goal-prompt.md.tpl`).

**Goal-mode + bash-tail compatibility:**

- **codex `/goal`** — works. Codex emits a structured "Final response" block
  at the end of the goal, giving the watcher a turn-boundary signal in the
  flat tail. Empirically verified.
- **grok / claude headless** — does not work. No equivalent end-of-goal
  signal in the flat tail; the watcher has no way to know when iteration
  is complete. Use one-shot + bash-tail with a coarser chunk instead, or
  switch to ACP if the adapter is available.

## Recipes

Worker invocations. All three drop a tail file the watcher polls. Replace
`<slug>` and `<workdir>` per chunk.

### codex (headless one-shot or `/goal` mode)

```bash
codex exec \
  --skip-git-repo-check \
  --dangerously-bypass-approvals-and-sandbox \
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
- `--dangerously-bypass-approvals-and-sandbox` — required for non-
  interactive operation. Codex's permission UI is a stdin TTY and would
  block forever on `codex exec`. The bypass flag is binary: it grants
  the process full local authority. See the safety story below.

### grok (headless one-shot)

```bash
grok -p "<prompt>" \
  --permission-mode acceptEdits \
  --cwd "<workdir>" \
  > /tmp/grok-<slug>.txt 2>&1 &
WORKER_PID=$!
```

`--permission-mode acceptEdits` is grok's equivalent of the codex bypass —
auto-accepts file edits without interactive prompts.

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
Prefer the Agent tool for sub-billed dispatches — Agent-tool subagents
share the parent session's billing but make their own LLM calls, so they
also share the parent's rate-limit budget. Reserve `claude -p` for cases
where you need a clearly delimited headless run outside the parent
session's context window AND you are willing to pay API rates.

## Watcher

Once spawned, watch the tail file with the dispatch watcher:

```bash
bash <skill-root>/scripts/watch-dispatch-tail.sh \
  --pid "$WORKER_PID" \
  --tail /tmp/<agent>-<slug>.txt \
  --controller-pid $$ \
  --agent <codex-bash-tail|grok-bash-tail|claude-bash-tail> \
  --session-id <slug> \
  > /tmp/watcher-<slug>.txt 2>&1 &
WATCHER_PID=$!
```

Exit codes:

| Code | Meaning |
|---|---|
| 0 | terminal marker (`COMPLETE` / `BLOCKED` / `USER-NEED` / `USER-CONFIRM`) |
| 1 | worker PID died without a terminal marker |
| 2 | tail file idle past `--max-idle-secs` (default 180s) — worker likely wedged |
| 3 | controller PID died — watcher self-detected orphan |

The watcher registers a pidfile under `/tmp/goal-flight-acp-pids.d/` so
`cleanup_ghosts()` (defined in `scripts/acp_client.py`; grep for the
function name — line drifts over time) reaps orphaned workers uniformly
across ACP and bash-tail paths. Filename pattern:
`<controller-pid>.bashtail.<worker-pid>.jsonl`.

## Bypass-flag safety story

The bypass / accept-edits flags grant the worker process full local
authority for the lifetime of the dispatch. Two operating modes apply:

- **Sequential mode** (no `--parallel`): the worker runs in the controller's
  repository root. Trust boundary is the user's machine. The bypass flag is
  acceptable because the controller is already operating with full local
  authority on the user's behalf.
- **Parallel mode** (`execute --parallel <N>`): each worker runs in its own
  git worktree at a path inside `.claude/worktrees/`. Pass `-C "<workdir>"`
  (codex) / `--cwd "<workdir>"` (grok) explicitly, or wrap in
  `(cd "<workdir>" && ...)` (claude — no `--cwd` flag). Without one of
  these, the worker inherits the controller's cwd and can edit files
  outside its worktree, defeating the worktree-as-sandbox boundary.

In both modes the bypass flag is **not** a security boundary. A
compromised or buggy worker can still run anything the user can. The
worktree boundary is a code-organization sandbox, not a security
container. Treat each worker as you would treat a shell you typed the
same commands into yourself — same blast radius, same audit posture.
Goal-flight's 0.4.0+ permission router is UX-level interception (catches
`session/request_permission` events for routing and logging), not a
sandbox.

## Why this is legacy

ACP gives the controller:

- discrete `tool_call` and `tool_call_update` events with `locations` arrays
  (used by `_scan_out_of_scope_paths` for scope-leak audit),
- explicit `stopReason` per turn,
- `agent_thought_chunk` and `plan` events that don't pollute the prose tail,
- `session/request_permission` interception (the 0.4.0 permission router
  hook).

Bash-tail loses all four. Migrate workers to ACP as adapters become
available; revisit this file only for parity recipes.
