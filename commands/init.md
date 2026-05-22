---
description: "Initialize goal-flight state for a project."
---

# init <topic>

Initialize a project for goal-flight with compact, procedural discovery.

Read:

- `protocols/session-preflight.md`
- `protocols/tool-readiness.md`
- `protocols/premises.md`
- `protocols/state-handoff.md`

## Steps

1. Confirm host setup/bootstrap happened. If this is a fresh host install, run
   setup from the cloned Goal Flight repository before project init:

```bash
./setup.sh --agent <codex-or-cursor>
./setup.sh --apply --yes --agent <codex-or-cursor>
```

For Codex, setup registers the Goal Flight package for the Desktop controller
surface, cleans any duplicate legacy personal Codex skill when plugin
registration succeeds, checks the CLI worker surface, and registers
context-mode MCP when needed. `codex exec` remains the worker path. For Cursor,
setup installs global agent instructions, a personal skill, and rules;
context-mode is available for Cursor, but the Goal Flight setup installer has
not automated Cursor context-mode installation yet. If the user wants it, the
agent can install context-mode for Cursor explicitly and verify it before use.

2. Run doctor:

```bash
python3 <skill-root>/scripts/goalflight_doctor.py --project-root "$PWD" --json
```

3. Ensure the ACP SDK venv exists:

```bash
ACP_VENV="$HOME/.goal-flight/venvs/acp-0.10"
if command -v uv >/dev/null 2>&1; then
  [ -x "$ACP_VENV/bin/python" ] || uv venv "$ACP_VENV"
  uv pip install --python "$ACP_VENV/bin/python" -r <skill-root>/requirements.txt
else
  [ -x "$ACP_VENV/bin/python" ] || python3 -m venv "$ACP_VENV"
  "$ACP_VENV/bin/python" -m pip install -r <skill-root>/requirements.txt
fi
```

4. Run capacity profile:

```bash
python3 <skill-root>/scripts/goalflight_capacity.py profile --json
```

5. Scaffold private project state if missing:

- `docs-private/`
- `docs-private/goal-<topic>-<date>.md` from `templates/goal-statement.md`
- `docs-private/RESUME-NOTES.md` from `templates/resume-notes.md`
- `AGENTS.md` from `templates/project-agents.md` when project has no local agent instructions
- `SKILL.md` from `templates/project-skill.md` when project has no root skill

6. Write only compact environment facts into `docs-private/env-caveats.md`:

- doctor summary path/result
- capacity profile
- available worker adapters
- known cooldowns
- commands the project uses for test/lint/build

Do not paste full probe output.

7. Confirm git hygiene:

- `docs-private/` ignored
- `AGENTS.md` tracked or intentionally absent
- root `SKILL.md` tracked or intentionally absent
- current branch/head/dirty state recorded in resume notes

8. Optional corpus:

If the repo is large and the user wants reusable dispatch context, run
`/goal-flight build-corpus`. Do not run corpus construction by default during
init.

9. Self-review:

- Are readiness warnings actionable?
- Did init avoid reading large docs/logs into context?
- Are next steps clear from `RESUME-NOTES.md`?

## Output

Print:

- created/updated file paths
- doctor WARN/FAIL count
- capacity operating cap
- next command suggestion
