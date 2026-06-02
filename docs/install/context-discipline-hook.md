# Install the context-discipline hook

After cloning goal-flight, the operator opts in to the
context-discipline hooks by symlinking them into Claude Code's hooks
directory:

```bash
ln -sf "$(git rev-parse --show-toplevel)/scripts/hooks/goalflight-context-discipline.sh" \
    ~/.claude/hooks/goalflight-context-discipline.sh
```

Then ensure the hook is registered in `~/.claude/settings.json` under
the `hooks.PreToolUse` array. The existing entry for
`codedb-block-legacy.sh` is the reference shape.

The hook blocks the noisy patterns described in
`feedback_environmental_design_for_context_discipline.md` and surfaces
override env vars per the block messages.

## Scope verification

The hook is a no-op outside the goal-flight repo working tree (call it
`<goal-flight-repo-root>`). It derives that root from its own real location
(resolving the `~/.claude/hooks` symlink back to
`<goal-flight-repo-root>/scripts/hooks/`), so no operator-specific path is
embedded and the hook works for any checkout. After installing the symlink +
settings.json registration, verify the scope-gate:

1. Open a Claude Code session in a NON-goal-flight directory (e.g.
   `~/Repos/<other-repo>`).
2. Attempt a `Read` on a >5KB file in that repo.
3. The hook should NOT block. If you see the goal-flight block message
   ("Read of file >5KB without GOALFLIGHT_RECON_OK=1..."), the scope-gate
   has regressed.
4. To force-enable the hook outside the goal-flight repo (test/dev only),
   set `GOALFLIGHT_HOOKS_FORCE=1` in the environment.

The scope-gate currently matches a goal-flight-repo working directory. Firing
for goal-flight-on-any-project (via an active-controller session marker) is
queued as a Wave-A follow-up.

See also `docs/install/session-start-watchdog-hook.md` for the Claude Code
SessionStart hook that re-arms the in-session Goal Flight watchdog after a
fresh orchestrator session starts.
