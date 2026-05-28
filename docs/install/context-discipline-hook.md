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

The hook is a no-op outside `/Users/simonrowland/Repos/goal-flight*`. After
installing the symlink + settings.json registration, verify the scope-gate:

1. Open a Claude Code session in a NON-goal-flight directory (e.g.
   `~/Repos/<other-repo>`).
2. Attempt a `Read` on a >5KB file in that repo.
3. The hook should NOT block. If you see the goal-flight block message
   ("Read of file >5KB without GOALFLIGHT_RECON_OK=1..."), the scope-gate
   has regressed.
4. To force-enable the hook outside the goal-flight repo (test/dev only),
   set `GOALFLIGHT_HOOKS_FORCE=1` in the environment.

The current scope-gate uses a hardcoded path (`/Users/simonrowland/Repos/goal-flight`).
Portable repo detection (probe for `SKILL.md` with `name: goal-flight`) is
queued as a Wave-A follow-up.
