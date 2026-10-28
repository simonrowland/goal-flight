# Agent Notes

> If `GOALFLIGHT_DISPATCH_ID` is set you are a dispatched worker: do your
> brief only; do not run `/goal-flight resume`, claim or renew controller
> leases, arm supervise/listeners, read RESUME-NOTES for direction, or pick
> queue items.

> **Goal Flight is installed in this repo.** Check active state FIRST with
> `python3 scripts/goalflight_session_status.py --text` — definitive verdict.
> If **active**, follow the Goal Flight load order below. If **no active
> session**, do NOT auto-load the skill end-to-end — only when the user
> invokes `/goal-flight <command>`. Reading repository `SKILL.md`
> end-to-end is the load-order step that follows; do not skip it.

## Companion tools (defined for non-Claude orchestrators loading this file)

- **gstack** — Garry Tan's skill pack (`/review`, `/challenge`, `/office-hours`,
  `/plan-eng-review`, `/cso`, `/investigate`, etc.). Installs at
  `~/.gstack/repos/gstack/.agents/skills/` and is registered per-host
  (Claude Code: `~/.claude/skills/`; Codex: `~/.codex/skills/`; Cursor:
  `~/.cursor/skills/`). Goal Flight invokes `gstack /review` as the canonical
  chunk-level pre-commit reviewer and `gstack /challenge` for adversarial
  framing. When gstack is absent, fall back to the bundled prompt skeletons at
  `prompts/gstack-claude-review.md` and `prompts/gstack-codex-challenge.md` —
  do **not** hand-roll a custom review prompt.
- **context-mode** — MCP plugin that offloads large command outputs
  (diffs, integration test runs, codex tail files, large greps) to an FTS5
  sandbox queried by pattern. Lets the orchestrator analyze big artifacts
  without consuming its own context window. Installs per-host (Claude Code:
  `~/.claude/plugins/cache/context-mode/...`; Codex: registered via
  `scripts/register-context-mode-codex.py`).

## Goal Flight Routing

- When a user invokes `goal-flight` or asks for durable planning, dispatch,
  review flights, worker orchestration, recovery, resume notes, or long-running
  repository work, load the Goal Flight skill wrapper first.
- Codex plugin skill path:
  `~/.codex/plugins/cache/goal-flight/goal-flight/<version>/skills/goal-flight/SKILL.md`.
- Repository canonical workflow path: `SKILL.md`.
- Load order: this agent instruction file, then the installed host wrapper when
  available, then repository `SKILL.md`, then only the `commands/*.md` and
  `protocols/*.md` files referenced by the invoked command.
- **Remote / multi-node workers (fleet).** To dispatch a worker on another machine
  over SSH — including a remote **Claude** worker via `claude-acp` — see
  `docs/fleet.md`: node registration, the per-worker install/auth recipe, and
  `goalflight_fleet.py dispatch`. Remote Claude needs a headless subscription token
  on the node (`claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN` in the node env,
  ferried to the detached worker); full recipe: docs/fleet.md "Remote Claude worker
  (claude-acp) end-to-end". Local Claude worker surfaces (loop / one-shot):
  `protocols/dispatch-routing.md` "Claude worker surfaces".
- For tests of orchestrator generalization, use a nondestructive task: run doctor,
  make a compact plan, check capacity, launch one read-only worker, and summarize
  status/ledger evidence without writing to the repository.
- During an active goal-flight run, keep advancing the queue and accumulating
  commits per chunk until decomposition/execute is done; do not stall on
  engagement prompts. See repository root `SKILL.md` §Autonomous throughput.
- **Controller session — active run + compaction:** if goal-flight was already in play (verdict
  active per `goalflight_session_status.py --text`, OR open queue/ledger,
  OR `docs-private/RESUME-NOTES-<YYYY-MM-DD>.md`), run `/goal-flight resume`
  (load order above; fresh disk read of `SKILL.md`, not compacted snapshot),
  then continue **in-skill**: dispatch workers, review before commit, one
  explicit-pathspec commit per chunk. Do **not** fall back to default assistant
  mode: inline edits, abandoning current task on new ask, or hand-rolled review.
  Canonical sequence: `SKILL.md` §State → `commands/resume.md` →
  `protocols/state-handoff.md`.

## Controller session — living state; read the newest docs-private/RESUME-NOTES-*.md first

Durable project state lives at fixed paths under `docs-private/` (convention over
configuration). At session start read the newest `RESUME-NOTES-*.md` (current
goals, next action, key pins), then re-ground in the docs it points to. Update the
relevant living doc when its subject changes — don't leave it only in chat.
Find newest: `ls -1 docs-private/RESUME-NOTES-*.md | sort | tail -1`.

Canonical name: `RESUME-NOTES-<YYYY-MM-DD>[-rev<N>].md` — ISO date so a
lexicographic sort is chronological, and no topic prefix (the topic goes inside
the file). Historical exceptions are left in place; new files follow the form.

`docs-private/` is gitignored, so git status and git diff are structurally blind
to it. Verify anything written there on the filesystem (`test -f`, `ls`), never
via git.

## Project commands (this repo)

- test: `./tests/run.sh`

The repo-wide gate. Run it under env isolation so a test run cannot touch live
state — point `GOALFLIGHT_JOURNAL_DIR`, `GOALFLIGHT_STATE_DIR`,
`GOALFLIGHT_WAKE_LEDGER`, `GOALFLIGHT_MESSAGES_DIR`, `GOALFLIGHT_TASK_STORE` and
`GOALFLIGHT_PIDFILE_DIR` at a temp root and set
`GOALFLIGHT_CAPACITY_CONF=/dev/null`. Tests that exercise the dispatch launch
path must also set `GOALFLIGHT_DISPATCH_DIR` themselves (via `tmp_path` /
`mktemp`); the default is still `<state_dir>/dispatch`, so a launch that
forgets both that override and `GOALFLIGHT_STATE_DIR` writes status into the
live uid-keyed directory. Do not set `GOALFLIGHT_DISPATCH_DIR` globally in the
gate — that would steal writes from tests that assert under
`$GOALFLIGHT_STATE_DIR/dispatch`. Journal/mail isolation stays on
`GOALFLIGHT_JOURNAL_DIR` / `GOALFLIGHT_MESSAGES_DIR`; the dispatch-dir
override does not cover those.

Never run the gate while a dispatched worker holds the tree: concurrent edits
produce phantom failures that belong to neither change. Workers run only their
own test files; the controller runs the full gate once after the fold.

## Dispatch/watcher family A/B deploy gate

Before deploying a dispatch or watcher change, run the same selected Python test
modules using `scripts/dispatch_watcher_gate_policy.json` and
`scripts/dispatch_watcher_gate.py`. The independent review supplies every
affected module pattern, including off-prefix groups. If a group cannot be
mapped, include `UNMAPPED` in the review output.

For a gate run, pass the complete review pattern list to one selector
invocation. Add one `--review-pattern` option per item, including
`--review-pattern UNMAPPED` in that invocation when the review lists an
unmapped group. The selector refuses to run when discovered modules differ
from the Git index plus its named local-only modules. It does not attest that
the review patterns cover every affected module; that remains the reviewer's
responsibility:

```sh
selection_json="$(python3 scripts/dispatch_watcher_gate.py --review-pattern '<pattern-1>' --review-pattern '<pattern-2>')"
```

The policy's `required_untracked_modules` are gitignored local-only tests. A
green selector requires every declared path to exist in the checkout. Prefer
copying `tests/python/ext/` from the main checkout into the dedicated gate
worktree before selecting because that keeps the module inventory with the
isolated gate environment. If that is unavailable, run the selector in the main
checkout, where the selector itself is read-only. The selector's error lists
missing paths and gives this remedy. Do not run a test suite in the main
checkout because tests can reset live worktree seats.

After the gate run, remove the copied directory; using a throwaway gate
worktree and removing it wholesale avoids leaving stale local-only files in a
reused seat.

Remote CI does not ship gitignored files. A selection with a non-null
`fallback_reason` widens to the full universe, including the local-only tests,
so run that fallback locally in the gate worktree with `tests/python/ext/`
present. A review-guided selection that includes any local-only path also needs
a local run.

Historical example: an earlier measured review included off-prefix groups
`test_drain_*`, `test_review_job.py`, `test_codex_resume_dispatch.py`,
`test_worktree_dispatch.py`, and `test_goalflight_capacity.py`.

This is a manual, review-guided family, not a dependency-derived selector. A
reverse-import closure for the measured dispatch and ACP changes reached 221 of
296 Python test modules (75%), so it was too broad to serve as a useful family
gate. A green A/B result establishes only that the selected modules passed; it
does not establish coverage outside the selected modules or replace the
full-suite gate.

The selector's `modules` and `revisions` fields drive the remote command. Set
`GOALFLIGHT_REMOTE_CI_CONFIG` to its absolute path and replace the deployed SHA
placeholder below. This command chooses matched-pair mode only when the
selector requests the deployed baseline and candidate; a candidate-only policy
uses a single remote run. Unsupported target lists stop before execution.

```sh
export GOALFLIGHT_REMOTE_CI_CONFIG=$HOME/Repos/goal-flight/.goal-flight/remote-ci.json
DEPLOYED_SHA='<deployed-sha>'
python3 - "$selection_json" "$DEPLOYED_SHA" <<'PY'
import json
import os
import subprocess
import sys

selection = json.loads(sys.argv[1])
targets = selection["revisions"]
if targets == ["deployed-baseline", "candidate"]:
    revision_args = ["--pair", "--base", sys.argv[2]]
elif targets == ["candidate"]:
    revision_args = []
else:
    raise SystemExit(f"unsupported revision targets: {targets!r}")

command = [
    os.environ["GOALFLIGHT_REMOTE_RUN"],
    "--kind", "gate",
    *revision_args,
    "--cmd-id", "goalflight-full-gate",
    "--",
    "python3", "-m", "pytest", "-q",
    *selection["modules"],
]
raise SystemExit(subprocess.run(command, check=False).returncode)
PY
```

Record the receipt's hostname; a local fallback does not satisfy this gate.

## Git workflow (this repo)

- **Commit as work completes** — one logical chunk at a time after focused tests
  **and at least one independent review** (`protocols/chunk-review.md`; default
  gstack `/review`, with `./scripts/autoreview.sh` as a complementary parallel
  option). Executor self-review alone is not enough.
- Do not wait for a separate "please commit" unless the user forbade commits.
- **`git commit -m '...' -- <files>` with explicit pathspecs** while other
  goal-flight workers are in flight. Never bare `git commit` — the commit
  guard (`scripts/goalflight_commit_guard.py`) refuses to prevent bundling
  worker WIP. See its error message for the fix shape.
- A linked-worktree seat commit can succeed while Git's automatic ref packing
  reports `Operation not permitted` creating the shared
  `.git/packed-refs.lock`. On the Codex launch paths (`codex` and `codex-acp`,
  the only labels the Seatbelt builder grants these roots to) the worker
  sandbox grants the private worktree Git directory and shared `objects/`,
  `refs/heads/`, and `logs/refs/heads/`, but not the common `.git/` directory,
  so packing is denied while the commit itself is not. Other shapes write
  according to host permissions rather than that grant. A successful branch
  update is atomically written as a loose ref under `refs/heads/`; Git reads
  that ref ahead of any packed copy, so this packing failure does not lose the
  branch tip. If `git commit` exits 0 **and** `refs/heads/<branch>` resolves to
  the new commit, that permission-denied packing warning alone needs no report.
  Resolve the branch, not just `HEAD`, and capture the commit's exit status
  before running the verification. Report any commit or ref update failure.
  This covers the sandbox permission denial only — a lock error from a
  different cause, such as a pre-existing lock reporting `File exists`, is not
  covered and must be reported.
- **Do not push to public** without the relevant test sweep and explicit user
  permission.
- Amending, force-push, and destructive git operations still require explicit
  user request.

## Git-Visible Trigger Hygiene

- Never put the known billing-trigger harness codenames from this thread in
  git-visible metadata: filenames, directories, branch names, tag names, commit
  messages, generated JSON manifest names, or installer output paths.
- Do not repeat those exact codenames in agent instruction files loaded by local
  coding tools.
- Installer aliases must not become manifest filenames. If an installer command
  receives `--agent=<trigger-codename>`, map that alias to a neutral manifest
  filename before reading or writing `adapters/*.json`.
- When porting legacy packages, rename codename JSON manifests before staging,
  then run the trigger audit against status, paths, commit messages, and history.
