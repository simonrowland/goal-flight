# {{TOPIC}} — Goal Statement

**Date:** {{DATE}}
**Owner:** {{OWNER}}
**Source:** {{SOURCE}}
**Status:** {{STATUS}}

<!-- STATUS values:
     CONCRETE       — Goal is sharp; decompose-plan + execute may proceed.
     DRAFT          — Goal is fuzzy; decompose-plan will refuse until sharpened.
     SUPERSEDED     — A later goal-statement file replaces this one.
     SOURCE values:
     "user statement (concrete from the start)"
     "office-hours interrogation (gstack)"
     "office-hours interrogation (local subagent)"
     "refactor-plan §N"
-->

## What changes when this is done

{{ONE_PARAGRAPH_SUCCESS_STATE}}

(One paragraph. Concrete. Names a user/system that benefits and what they observe differently. Not "the code is cleaner" — "users can do X without Y" or "the system tolerates Z without manual intervention.")

## Why now

{{ONE_PARAGRAPH_MOTIVATION}}

(What triggered this work? What cost are we paying today by not doing it? What's the deadline or window, if any?)

## The narrowest wedge

{{ONE_SENTENCE_WEDGE}}

(The smallest visible thing that proves this works. The first chunk in the decomposition should ship the wedge, not just lay foundation.)

## What's explicitly NOT in scope

- {{NON_GOAL_1}}
- {{NON_GOAL_2}}
- {{NON_GOAL_3}}

(Be ruthless. Drive-by refactors, "while we're here" cleanups, broader rewrites adjacent to this work — list them explicitly so executors don't pull them in.)

## Success criteria (measurable)

- {{CRITERION_1}}
- {{CRITERION_2}}
- {{CRITERION_3}}

(Each criterion: testable, quantitative where possible, has a clear pass/fail. "Performance improves" is bad; "P50 latency under 100ms on the 10k-row fixture" is good.)

## Anti-success (what counts as failure even if all criteria pass)

- {{ANTI_1}}

(E.g.: "criteria pass but the cost is X engineering-weeks of unrelated cleanup", or "criteria pass but the gstack /review at milestones surfaces P0 invariant breaks".)

## Constraints

- **Time**: {{TIME_CONSTRAINT}}
- **Reversibility**: {{REVERSIBILITY}}
- **Dependencies**: {{DEPENDENCY_RULES}}

## Source materials

- Plan-of-record: `{{PLAN_PATH}}`
- Binding spec / contracts: `{{SPEC_PATH}}`
- Office-hours transcript (if used): `{{OFFICE_HOURS_PATH}}`
- Prior conversation context: {{SESSION_REF}}

---

This document is the load-bearing anchor for the {{TOPIC}} initiative.

The controller cites it when:
- Reviewing the decomposition for goal-coherence (`decompose-plan` step 4.5).
- Deciding what to defer when chunks slip.
- Asking the user clarifying questions (anticipatory or mid-execute).
- Writing milestone summaries.
- Composing the queue-completion notification.

Workers do NOT need to read this. Their concerns are chunk-level. The controller is the only consumer.
