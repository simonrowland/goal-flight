# Cursor Notes

Run from your Goal Flight clone (default `~/.goal-flight`; see README).

One-shot install (global + project):

```bash
./install.sh cursor /path/to/project
# same as: ./setup.sh --apply --yes --cursor-install /path/to/project
```

Cursor can run Goal Flight as a controller through the installed skill wrapper
and can run `cursor-agent` as an ACP worker.

- Cursor MCP config lives in `~/.cursor/mcp.json` globally or
  `<project>/.cursor/mcp.json` for one project.
- Use `cursor-agent mcp list`, `cursor-agent mcp enable context-mode` when
  approval is needed, and `cursor-agent mcp list-tools context-mode` to verify
  MCP discovery after restarting Cursor.
- Keep long worker logs in files and summarize status JSON, not raw transcripts.

Advanced setup (dry-run first, omit `--apply --yes`):

```bash
./setup.sh --cursor                              # plan global install
./setup.sh --cursor-project /path/to/project     # plan project install only
./setup.sh --apply --yes --cursor-agents-standard
./setup.sh --apply --yes --cursor-link-claude --addons ''
```
