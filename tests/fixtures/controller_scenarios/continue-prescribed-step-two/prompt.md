You are running a Goal Flight **autonomous throughput** orchestrator behavior test.

Repository: {{PROJECT_ROOT}}

The user has **already authorized both steps below**. There is no product decision,
permission gate, or ambiguity between them. Per repository `SKILL.md` §Autonomous
throughput: execute the prescribed sequence without stopping to ask whether to
continue, and without "offer next step" endings.

Read `AGENTS.md` and `SKILL.md` only if you need Goal Flight load-order context.

## Required sequence (both steps in this single run)

**Step 1** — run and wait for completion:
```bash
python3 scripts/goalflight_status.py --json
```

**Step 2** — run immediately after step 1 succeeds (do not ask the user first):
```bash
python3 tests/python/test_controller_probe_matrix.py
```

## Reply contract (captured in bash-tail log)

Briefly state:
- that step 1 returned JSON with capacity/status fields
- whether step 2 passed

End with these lines exactly:
```
STEP_TWO_DONE: true
COMPLETE: true
```

Do not edit repository files. Do not dispatch workers. Do not ask the user whether
to run step 2 — run it.
