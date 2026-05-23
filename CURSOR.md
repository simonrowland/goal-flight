# Cursor Notes

Cursor can run Goal Flight as a controller through the installed skill wrapper
and can run `cursor-agent` as an ACP worker.

- Install global wrapper: `./setup.sh --apply --yes --cursor`.
- Install project-local wrapper: `./setup.sh --apply --yes --cursor-project /path/to/project`.
- Cursor MCP config lives in `~/.cursor/mcp.json` globally or
  `<project>/.cursor/mcp.json` for one project.
- Use `cursor-agent mcp list`, `cursor-agent mcp enable context-mode` when
  approval is needed, and `cursor-agent mcp list-tools context-mode` to verify
  MCP discovery after restarting Cursor.
- Keep long worker logs in files and summarize status JSON, not raw transcripts.
