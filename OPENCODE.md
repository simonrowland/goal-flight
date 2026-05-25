# OpenCode Notes

One-shot install (global + project):

```bash
./install.sh opencode /path/to/project
# same as: ./setup.sh --apply --yes --opencode-install /path/to/project
```

OpenCode can run Goal Flight as a controller through the installed skill wrapper
and can run `opencode acp` as an ACP worker.

- OpenCode config lives in `~/.config/opencode/opencode.json` globally or
  `<project>/opencode.json` for one project.
- Skills live in `~/.config/opencode/skills/<name>/SKILL.md` globally or
  `<project>/.opencode/skills/<name>/SKILL.md` project-local.
- Use `opencode mcp list` and `opencode mcp auth list` to verify MCP discovery
  after restarting OpenCode.
- Keep long worker logs in files and summarize status JSON, not raw transcripts.
- OpenCode reads Claude-compatible paths (`.claude/skills`, `CLAUDE.md`) unless
  disabled via `OPENCODE_DISABLE_CLAUDE_CODE*`. Goal Flight setup prefers native
  OpenCode paths first.

Advanced setup (dry-run first, omit `--apply --yes`):

```bash
./setup.sh --opencode                              # plan global install
./setup.sh --opencode-project /path/to/project     # plan project install only
./setup.sh --apply --yes --opencode-agents-standard
./setup.sh --apply --yes --opencode-link-claude --addons ''
```

For bash-tail workers use `scripts/opencode_bash_tail.py`, not bare `opencode run`.
