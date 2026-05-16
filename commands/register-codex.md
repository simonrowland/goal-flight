# register-codex [<project-path>]

Register a project (or worktree path) as codex-trusted in `~/.codex/config.toml`.
This is the thin sub-command wrapper around `scripts/install-codex-overrides.sh`
so the install can be re-run idempotently outside the init flow — e.g. after
cloning the goal-flight skill onto a new machine, after a `~/.codex/`
reset, or for a worktree spawned outside the usual `<repo>/.claude/worktrees/`
convention.

## When to invoke

- The user explicitly typed `/goal-flight register-codex [<path>]`.
- During `commands/init.md` step 1 environment validation, when `scripts/
  install-codex-overrides.sh --check` reports MISSING and the user accepts
  the install prompt.
- During `commands/execute.md` step 3a, if a parallel worktree is created
  OUTSIDE `<repo-root>/` (uncommon — goal-flight's convention keeps them
  inside, which inherits trust by path prefix).

## What the user provides

- **No args** → register the current git toplevel (or pwd if not a git repo).
- **One arg `<project-path>`** → register that absolute or relative path.

## Steps

1. Resolve the skill root: read `~/.claude/skills/goal-flight/SKILL.md`'s
   directory, or use `<repo-root>` if the skill is checked out locally
   (e.g. `~/Repos/goal-flight`).

2. Resolve the target path:
   - If user supplied an arg: `cd "$ARG" && pwd` (must exist).
   - Else: `git rev-parse --show-toplevel 2>/dev/null || pwd`.

3. Run a dry-run first: `bash <skill-root>/scripts/install-codex-overrides.sh --check <path>`.
   - Exit 0 → already registered. Print "Already trusted: <path>." and stop.
   - Exit 1 → not registered. Continue.
   - Other → codex not installed; print "codex CLI not found; skipping." and stop.

4. Surface the action and ask the user:

   > "I'll register `<path>` as codex-trusted by appending a
   > `[projects."<path>"].trust_level = \"trusted\"` block to
   > `~/.codex/config.toml`. This bypasses the MCP approval-gate stall
   > on non-interactive `codex exec` dispatches in that project (and
   > worktrees under it). Mirror to `<path>/.codex/config.toml` for self-
   > documentation? (y/n — y by default)."

5. Run the install:
   - Default (mirror on): `bash <skill-root>/scripts/install-codex-overrides.sh <path>`
   - User said no to mirror: `bash <skill-root>/scripts/install-codex-overrides.sh --no-project-mirror <path>`

6. Re-run `--check` and report the verification block to the user.

## See also

- `SKILL.md` §Codex reliability — background on the stall and
  the trust mechanism.
- `scripts/install-codex-overrides.sh` — the actual installer.
