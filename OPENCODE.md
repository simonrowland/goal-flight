# OpenCode Notes

OpenCode can run Goal Flight as a controller through the installed skill wrapper
and can run `opencode acp` as an ACP worker.

- Install global wrapper: `./setup.sh --apply --yes --opencode`.
- Install project-local wrapper: `./setup.sh --apply --yes --opencode-project /path/to/project`.
- OpenCode config lives in `~/.config/opencode/opencode.json` globally or
  `<project>/opencode.json` for one project.
- Skills live in `~/.config/opencode/skills/<name>/SKILL.md` globally or
  `<project>/.opencode/skills/<name>/SKILL.md` project-local.
- Use `opencode mcp list` and `opencode mcp auth list` to verify MCP discovery
  after setup.
- Keep long worker logs in files and summarize status JSON, not raw transcripts.
- OpenCode reads Claude-compatible paths (`.claude/skills`, `CLAUDE.md`) unless
  disabled via `OPENCODE_DISABLE_CLAUDE_CODE*`. Goal Flight setup prefers native
  OpenCode paths first.
