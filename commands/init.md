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

1. Run doctor:

```bash
python3 <skill-root>/scripts/goalflight_doctor.py --project-root "$PWD" --json
```

2. Run capacity profile:

```bash
python3 <skill-root>/scripts/goalflight_capacity.py profile --json
```

3. Scaffold private project state if missing:

- `docs-private/`
- `docs-private/goal-<topic>-<date>.md` from `templates/goal-statement.md`
- `docs-private/RESUME-NOTES.md` from `templates/resume-notes.md`
- `AGENTS.md` from `templates/project-agents.md` when project has no local agent instructions

4. Write only compact environment facts into `docs-private/env-caveats.md`:

- doctor summary path/result
- capacity profile
- available worker adapters
- known cooldowns
- commands the project uses for test/lint/build

Do not paste full probe output.

5. Confirm git hygiene:

- `docs-private/` ignored
- `AGENTS.md` tracked or intentionally absent
- current branch/head/dirty state recorded in resume notes

6. Optional corpus:

If the repo is large and the user wants reusable dispatch context, run
`/goal-flight build-corpus`. Do not run corpus construction by default during
init.

7. Self-review:

- Are readiness warnings actionable?
- Did init avoid reading large docs/logs into context?
- Are next steps clear from `RESUME-NOTES.md`?

## Output

Print:

- created/updated file paths
- doctor WARN/FAIL count
- capacity operating cap
- next command suggestion
