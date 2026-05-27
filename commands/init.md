---
description: "Initialize goal-flight state for a project."
---

# init <topic>

Initialize a project for goal-flight with compact, procedural discovery.

Read `protocols/session-preflight.md`, `protocols/tool-readiness.md`,
`protocols/premises.md`, `protocols/state-handoff.md`, and
`protocols/chunk-review.md` (review tooling).

## Steps

1. Confirm host setup/bootstrap happened. If this is a fresh host install, run
   setup from your Goal Flight clone (default `~/.goal-flight`) before project init:

```bash
./setup.sh --agent codex
./setup.sh --apply --yes --agent codex

./setup.sh --cursor
./setup.sh --apply --yes --cursor

# Optional Cursor project-local install:
./setup.sh --apply --yes --cursor-project "$PWD"
```

For Codex, setup registers the Goal Flight package for the Desktop controller
surface, cleans any duplicate legacy personal Codex skill when plugin
registration succeeds, checks the CLI worker surface, and registers
context-mode MCP when needed. `codex exec` remains the worker path. For Cursor,
setup installs global agent instructions, a personal skill, rules, and Cursor
MCP config for context-mode. Project-local setup writes the wrapper and, when
context-mode is selected, project-specific `.cursor/mcp.json` under the target
repository.

Default add-ons (same tier as setup prompts):

- **context-mode** — large-output offload
- **gstack** — default independent reviewer for both chunk-level pre-commit
  review and milestone review (`/review`, plus `/office-hours`, `/plan-eng-review`,
  `/cso`, `/investigate`). Local fallback prompts live in
  `prompts/gstack-claude-review.md` and `prompts/gstack-codex-challenge.md` when
  gstack is not installed.
- **autoreview** — complementary diff-local pre-commit pass via
  `scripts/autoreview.sh`. Runs in parallel with gstack at chunk level when the
  controller chooses; does not replace gstack as the default review path.
  Install upstream autoreview before init when doctor WARNs — typically the
  Cursor autoreview skill or `AUTOREVIEW_HELPER`.

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

- `docs-private/` runtime contents ignored (skeleton README and `.gitkeep` placeholders may be tracked in the skill repo; populated project state stays local)
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
