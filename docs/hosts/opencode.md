# OpenCode Notes

Run from your Goal Flight clone (default `~/.goal-flight`; see README).

One-shot install (global + project):

```bash
./install.sh opencode /path/to/project
# same as: ./setup.sh --apply --yes --opencode-install /path/to/project
```

## Resync after SKILL.md changes

When the source Goal Flight repo's `SKILL.md` or tracked files under
`commands/`, `protocols/`, `templates/`, or `adapters/` change, OpenCode's
installed copy is not auto-synced unless it is a symlink to the source repo.
Resync from the source repo with `./install.sh opencode /path/to/project`. To
check for drift, run
`python3 scripts/goalflight_doctor.py --project-root "$PWD" --json` and inspect
`installed_skill_drift`; text mode prints `installed_skill_md_hash` WARNs.

OpenCode can run Goal Flight as an orchestrator through the installed skill wrapper
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

## Remote bash-tail

Local bash-tail via the HTTP API is supported. **Remote fleet bash-tail marker
tail** (streaming `BLOCKED:` / `USER-NEED:` markers from a worker on another
machine) was introduced as a 1.0-era beta surface:

- Probe-only validation: `python3 scripts/goalflight_fleet_bash_tail_probe.py`
  and `tests/python/test_fleet_bash_tail_probe.py`
- Full remote dispatch parity with local bash-tail is not a release gate; use ACP
  workers for production remote dispatch

See [fleet.md](../fleet.md) for SSH fleet operations.
