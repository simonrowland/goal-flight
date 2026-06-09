# Cursor Notes

Run from your Goal Flight clone (default `~/.goal-flight`; see README).

One-shot install (global + project):

```bash
./install.sh cursor /path/to/project
# same as: ./setup.sh --apply --yes --cursor-install /path/to/project
```

## Resync after SKILL.md changes

When the source Goal Flight repo's `SKILL.md` or tracked files under
`commands/`, `protocols/`, `templates/`, or `adapters/` change, Cursor's
installed copy is not auto-synced unless it is a symlink to the source repo.
Resync from the source repo with `./install.sh cursor /path/to/project`. To
check for drift, run
`python3 scripts/goalflight_doctor.py --project-root "$PWD" --json` and inspect
`installed_skill_drift`; text mode prints `installed_skill_md_hash` WARNs.

Cursor can run Goal Flight as an orchestrator through the installed skill wrapper
and can run `cursor-agent` as an ACP worker.

- Cursor MCP config lives in `~/.cursor/mcp.json` globally or
  `<project>/.cursor/mcp.json` for one project.
- Use `cursor-agent mcp list`, `cursor-agent mcp enable context-mode` when
  approval is needed, and `cursor-agent mcp list-tools context-mode` to verify
  MCP discovery after restarting Cursor.
- Keep long worker logs in files and summarize status JSON, not raw transcripts.
- Cursor ACP workers that need tool-use/file-writing chunks must use
  `--permission-mode inline` (or `--interactive`). Plain `auto` permission mode
  can escalate shell/tool calls to `USER-CONFIRM` and block the worker; inline
  mode has been verified for edit + shell chunks.

Advanced setup (dry-run first, omit `--apply --yes`):

```bash
./setup.sh --cursor                              # plan global install
./setup.sh --cursor-project /path/to/project     # plan project install only
./setup.sh --apply --yes --cursor-agents-standard
./setup.sh --apply --yes --cursor-link-claude --addons ''
```

## Orchestrator wake on worker completion (Cursor)

Cursor does **not** use Claude Code's `run_in_background: true` task-notification
harness. Worker success/fail wakes the orchestrator **only** when a **blocking**
dispatch shell exits: run `python3 <skill-root>/scripts/goalflight_dispatch.py …`
as a Cursor **background Shell** (`block_until_ms: 0`), never bare bash `&` /
`nohup` / `disown` — bare background launches the launcher and returns
immediately (CHANGELOG 0.2.8 P0), so the agent session is not woken when the
worker finishes. The dispatch script blocks on `goalflight_watch.py` until a
terminal state, prints `DISPATCH-END`, then exits; Cursor may surface a **shell
task completed** notification for that background shell, but that is **not
guaranteed to auto-resume this chat turn** — treat shell exit as a prompt to
run `goalflight_status.py --dispatch <id> --all-projects`, read the tail marker,
and continue execute step 7–8 (review, commit). Workers must emit `COMPLETE:` /
`BLOCKED:` / `READY:` as the **last non-empty line** of the tail
(`protocols/worker-markers.md`); trailing prose after the marker yields
`worker_dead_no_terminal_marker` and the dispatch shell never exits. While
workers are in flight, `protocols/user-status-cadence.md` still applies: poll
`goalflight_status.py` on a ≤15-minute cadence as the fallback plane if no shell
notification arrives. Do not hand-roll JSON status-file polls or `--done` loops
with shell errexit — use the skill status tooling for digests only.

```bash
# Canonical Cursor dispatch (one background Shell per worker):
python3 ~/.goal-flight/skill/scripts/goalflight_dispatch.py \
  --agent cursor \
  --shape acp \
  --permission-mode inline \
  --prompt-file docs-private/dispatch/<chunk>.md \
  --cwd /path/to/repo-or-worktree \
  --dispatch-id <chunk-id> \
  --max-idle-secs 1200
```
