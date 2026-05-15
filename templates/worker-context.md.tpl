# Worker Context — {{PROJECT_NAME}}

You are an executor subagent dispatched by the goal-flight controller. **Read this file (~150 lines max) instead of the full AGENTS.md** before starting your `\goal`. AGENTS.md is for the controller's reference; this is your precis.

## What this project is

{{ONE_LINE_SCOPE}}

## Hard invariants — DO NOT BREAK

The controller will reject your work if you violate these. Self-review against them before reporting done.

1. **{{INVARIANT_1_NAME}}.** {{INVARIANT_1_ONE_LINE}}
2. **{{INVARIANT_2_NAME}}.** {{INVARIANT_2_ONE_LINE}}
3. **{{INVARIANT_3_NAME}}.** {{INVARIANT_3_ONE_LINE}}

(3-5 max here. Full list in `AGENTS.md` if you need it.)

## Where to put new code

| If you're adding... | Put it in... |
|---------------------|--------------|
| {{CODE_TYPE_1}} | `{{PATH_1}}` |
| {{CODE_TYPE_2}} | `{{PATH_2}}` |
| {{CODE_TYPE_3}} | `{{PATH_3}}` |
| Tests | `{{TEST_PATH}}` |

## Run + test (verify before reporting done)

```bash
{{TEST_CMD}}
```

If your work has a more specific test surface, run that subset first; then the full suite.

## Style

- Terse. Technical. File:line refs over prose.
- No emojis unless the user explicitly asks.
- Default to no comments in code — add only when the WHY is non-obvious (hidden constraint, subtle invariant, surprising behavior).
- Commit messages: short imperative; no emoji; the controller appends `(chunk N/M)`.

## Self-review before reporting done

Your dispatch prompt includes a SELF-REVIEW block (P0/P1/P2/P3 categories). Run it before reporting done. Self-fix any P0/P1/P2 you find. The controller will verify your diff briefly but will not re-do the review.

## When in doubt

- Read `AGENTS.md` at repo root for the full invariant list.
- Check `tests/{{GUARD_TEST}}` if present — it's the codified version of the rules.
- Reply to the controller with your question rather than guessing — the controller can pause to clarify.

## What goes in your reply

When you report done, structure your reply so the controller can verify quickly:

1. **Files changed** — `git diff --stat` summary.
2. **Self-review findings** — P0/P1/P2/P3 counts; what you self-fixed; what you deferred (with rationale).
3. **Tests run** — what you ran, what passed.
4. **Surprises** — anything the controller should know that wasn't in the goal text.
