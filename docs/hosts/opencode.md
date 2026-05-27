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

For bash-tail workers use `scripts/hosts/opencode/bash_tail.py`, not bare `opencode run`.

Install source for project `opencode.json`: `configs/opencode/opencode.json`.

## Remote bash-tail (beta)

Local bash-tail via the HTTP API is supported. **Remote fleet bash-tail marker
tail** (streaming `BLOCKED:` / `USER-NEED:` markers from a worker on another
machine) is **beta** in 1.0.0:

- Probe-only validation: `python3 scripts/goalflight_fleet_bash_tail_probe.py`
  and `tests/python/test_fleet_bash_tail_probe.py`
- Full remote dispatch parity with local bash-tail is not a release gate; use ACP
  workers for production remote dispatch

See [fleet.md](../fleet.md) for SSH fleet operations.
