# Agent Operating Instructions — {{PROJECT_NAME}}

Private (gitignored). Applies to every coding agent: Claude Code, Codex,
review/subagents. Read this before touching code.

## What this project is

{{PROJECT_DESCRIPTION_PARAGRAPH}}

{{PROJECT_NON_SCOPE_PARAGRAPH}}

## Hard invariants — never break

<!-- The smallest set you'd reject any PR for. The shorter, the louder. -->

1. **{{INVARIANT_1_NAME}}.** {{INVARIANT_1_DESCRIPTION}}
2. **{{INVARIANT_2_NAME}}.** {{INVARIANT_2_DESCRIPTION}}
3. **{{INVARIANT_3_NAME}}.** {{INVARIANT_3_DESCRIPTION}}
4. **{{INVARIANT_4_NAME}}.** {{INVARIANT_4_DESCRIPTION}}
5. **{{INVARIANT_5_NAME}}.** {{INVARIANT_5_DESCRIPTION}}

## {{DOMAIN}} policy (binding)

The plan of record is `docs-private/{{TOPIC}}-refactor-plan-{{PLAN_DATE}}.md`
with the audit substrate at
`docs-private/{{TOPIC}}-feature-coverage-checklist-{{PLAN_DATE}}.md`.

**Per-{{UNIT}} authority (do not silently widen):**

| {{UNIT}} | Authoritative {{PROVIDER}} | Notes |
|----------|----------------------------|-------|
| {{UNIT_EXAMPLE_1}} | {{PROVIDER_EXAMPLE_1}} | {{NOTE_EXAMPLE_1}} |
| {{UNIT_EXAMPLE_2}} | {{PROVIDER_EXAMPLE_2}} | {{NOTE_EXAMPLE_2}} |
| {{UNIT_EXAMPLE_3}} | {{PROVIDER_EXAMPLE_3}} | {{NOTE_EXAMPLE_3}} |

**{{PROVIDER}} selection default:** {{SELECTION_RULE}}

**Forbidden:**

- {{FORBIDDEN_ACTION_1}}
- {{FORBIDDEN_ACTION_2}}
- Any {{PROVIDER}} mutating `{{CANONICAL_STORE}}` directly. The
  `{{COMMIT_PATH}}` is the sole writer.
- Silently falling back to a less-trusted {{PROVIDER}} when the requested
  {{UNIT}} fails. Fail loudly, route through the planner.
- {{FORBIDDEN_ACTION_5}}

## File map (where things live)

| Area | Path |
|------|------|
| {{AREA_1}} | `{{PATH_1}}` |
| {{AREA_2}} | `{{PATH_2}}` |
| {{AREA_3}} | `{{PATH_3}}` |
| Tests | `tests/test_*.py` |
| Public docs | `docs/*.md` |
| Private plans | `docs-private/*.md` (gitignored) |

Future / planned (per refactor plan, do not create speculatively):
`{{FUTURE_DIR_1}}`, `{{FUTURE_DIR_2}}`, `{{FUTURE_DIR_3}}`.

## Run + test

```bash
{{INSTALL_CMD}}                 # one-command setup
{{RUN_CMD}}                     # launch
{{TEST_CMD}}                    # run the suite
```

## Migration rules ({{TOPIC}} refactor)

1. **Shadow mode per {{UNIT}}.** For each {{UNIT}} moved to the new path,
   run new path AND legacy path in parallel; assert
   `<new-result> == <legacy-result>` within `<tolerance>` before flipping.
   One {{UNIT}} per commit.
2. **Tests before flips.** Each {{UNIT}} flip lands with: scope-filter
   test, authority-match test, invariant-gate test, shadow-parity test
   on at least {{N}} representative inputs.
3. **No drive-by refactors.** Bug fixes do not bundle reorgs.

## Conversation style for agents

- Terse. Technical. File:line refs over prose.
- No emojis unless the user asks.
- Don't narrate the obvious. Single-sentence "what I'm about to do"
  updates before tool calls; nothing during routine work.
- End-of-turn: 1–2 sentences. What changed, what's next.
- Default to no comments in code. Inline comment only when the WHY is
  non-obvious (hidden constraint, subtle invariant, surprising behavior).

## When in doubt

- Check `docs-private/{{TOPIC}}-refactor-plan-{{PLAN_DATE}}.md` for
  {{AUTHORITY_BOUNDARIES}}.
- Check `docs/{{PROCESS_DOC}}.md` for what the system tracks.
- Check `tests/{{GUARD_TEST}}.py` for what the codebase rejects.
- Ask the user. Don't guess on {{HIGH_STAKES_DECISIONS}}.
