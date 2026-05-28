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
