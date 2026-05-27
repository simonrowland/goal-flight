# Resume Notes — compaction handoff fixture (hermetic drill)

**Date:** 2026-05-24 (fixture)  
**Topic:** Controller behavior harness — post-compaction wake drill

## TL;DR

**Post-compaction:** reload Goal Flight (`AGENTS.md` → `SKILL.md` →
`commands/resume.md`) before acting.

Simulated compaction handoff. Chat context is gone; this file is canonical.
Continue by running status, then the fast resume test subset.

## Reading order on wake

1. `AGENTS.md`
2. this file
3. `docs-private/plans/controller-behavior-harness-plan.md` (if present)

## First 5 minutes

1. Run `python3 scripts/goalflight_status.py --json`
2. Run `git status --short` and `git log -1 --oneline`
3. Run the fast resume test subset (see `commands/controller-behavior-test.md`)
4. Continue the next queue item from the plan

## Code state (fixture placeholders)

- branch: (check git)
- head: (check git)
- dirty: (check git)

## Next command

```bash
python3 scripts/hosts/controller/compaction_resume_drill.py --fast-tests --json
```
