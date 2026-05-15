# {{TOPIC}} Goal Queue

**Date:** {{DATE}}
**Controller:** This Claude Code session.
**Working directory:** {{REPO_ROOT}}

Each goal is a self-contained `codex exec` (or Claude subagent) directive. Goals carry their own hard invariants — the executor must not relax them. Read together with:

- `AGENTS.md` (project-wide invariants)
- `docs-private/{{TOPIC}}-binding-spec-{{DATE}}.md` (object/intent contracts)
- `docs-private/{{TOPIC}}-refactor-plan-{{PLAN_DATE}}.md` (plan of record, if separate)

## Progress (as of {{DATE}}, `main` @ {{HEAD}})

| Goal | Status | Commit |
|------|--------|--------|
| 1. `{{SLUG_1}}` | TODO | — |
| 2. `{{SLUG_2}}` `[parallel-safe:A]` | TODO | — |
| 3. `{{SLUG_3}}` `[parallel-safe:A]` | TODO | — |

Status values: ✅ DONE — `<hash>` · 🟡 IN-FLIGHT — `<executor-id>` · TODO · BLOCKED — `<reason>` · PARTIAL — `<reason; see #N>`.

Independence tags:
- `[parallel-safe:<group>]` — chunks in the same group can run together via `/goal-flight execute --parallel N`. Different groups must respect implicit ordering. Untagged chunks are sequential-only.
- `[milestone]` — append to a chunk's slug to trigger a gstack review sweep after this chunk lands (in addition to the every-K-commits cadence).

## Next dispatch batch

1. {{NEXT_1}}
2. {{NEXT_2}}
3. {{NEXT_3}}

Codex `exec` has been observed to stall silently in long sessions — prefer Claude subagents (Agent tool, NOT `claude -p`) where the work is well-scoped; reserve codex for tasks that genuinely benefit from it. The skill's gstack milestone reviews use codex + claude in parallel for independent verification.

Any P0/P1/P2 findings from milestone reviews become fix-work that preempts this batch — converge the review first.

## Universal preconditions (do not relax for any goal)

- {{INVARIANT_CHECK_1}}
- {{INVARIANT_CHECK_2}}
- All currently passing tests stay green. New tests added per goal.
- No silent fallback between {{PROVIDERS}} on {{UNIT}} failure.
- No emoji in code or commit messages.

---

## Optional sections — use when applicable

Real goals from the goal-flight reference project carry sections beyond the basic SCOPE/CHECKLIST/ACCEPTANCE/FORBIDDEN skeleton. Use these when they apply; don't pad goals with empty stubs.

- **STATUS** (top of goal): pin observed reality when a goal has been touched after writing. Examples: `STATUS: all 7 intents committed at f10c405; PER-INTENT FLIP RULE below is historical, do NOT re-dispatch.` Or: `STATUS: DEFERRED INDEFINITELY — 2026-05-15 user call.` The progress table is NOT a substitute — STATUS lives with the goal so future dispatchers see it inline.
- **PRECONDITION** (first-class, after SCOPE): list upstream goals or environmental requirements. Examples: `PRECONDITION: goal #18 JSON runner harness merged; goal #20 framework + Phase B VapoRock convergence cohort GREEN.`
- **`[Reviewer note: ...]` annotations inline in CHECKLIST**: when a planning error surfaces mid-execute, annotate the affected checklist item in place rather than rewriting. Example: `1. engines/alphamelts/provider.py — declares ONLY {SILICATE_LIQUIDUS, FREEZE_PATH} as its intent set. [Reviewer note: queue text says FREEZE_PATH but the binding spec uses SILICATE_LIQUIDUS + SILICATE_EQUILIBRIUM. Use the latter.]`
- **PRIORITY CANDIDATES** for research/exploration goals: user-stated priorities pinned in the goal so they don't drift to chat. Example from a literature-reproduction goal: `PRIORITY CANDIDATES: 1. The French SiO paper (last few years; HAL preferred). 2. Schaefer & Fegley 2004 (canonical baseline). 3. ...`

---

## 1. `\goal {{SLUG_1}}`

```
STATUS (optional)
{{If goal has been touched since writing, pin reality here.}}

SCOPE
{{1–3 sentence problem statement + boundary; what module(s), what contract.}}

PRECONDITION (optional)
- {{Upstream goal slug + commit hash, or "license XYZ acquired", etc.}}

REFERENCE
- AGENTS.md (hard invariants)
- docs-private/{{TOPIC}}-binding-spec-{{DATE}}.md §{{N}}

CHECKLIST
1. {{Smallest-first imperative.}}
   [Reviewer note (optional): {{margin-note when checklist item amended mid-flight}}]
2. {{...}}
3. {{...}}

ACCEPTANCE
- {{Pass/fail criterion 1.}}
- {{Pass/fail criterion 2.}}
- All previously passing tests stay green.

FORBIDDEN
- {{Hard barrier 1.}}
- {{Hard barrier 2.}}
- Any action that violates a §7 caution in the binding-spec.

PRIORITY CANDIDATES (optional — for research/exploration goals)
- {{user-stated priority 1}}
- {{...}}
```

---

## 2. `\goal {{SLUG_2}}`

```
SCOPE
{{...}}

REFERENCE
- AGENTS.md
- docs-private/{{TOPIC}}-binding-spec-{{DATE}}.md §{{N}}

CHECKLIST
1. {{...}}

ACCEPTANCE
- {{...}}

FORBIDDEN
- {{...}}
```

---

## N. `\goal {{MULTI_STEP_SLUG}}` — multi-unit, embedded review flight

```
SCOPE
{{Problem requiring N units flipped one-by-one.}}

REFERENCE
- AGENTS.md
- docs-private/{{TOPIC}}-binding-spec-{{DATE}}.md §3 (authority matrix)

PER-INTENT FLIP RULE
For each <unit> (in order: <unit-1>, <unit-2>, <unit-3>, ...):

1. Implement <module-path>/<unit>.<ext>.
2. Register <unit> with <central-dispatcher>.
3. Run shadow mode: legacy path AND new path in parallel.
4. Assert <new-result> == <legacy-result> within <tolerance>
   on <representative-input-set>.
5. Once parity holds across a full smoke run, flip the legacy call site
   to the new path.
6. **Adversarial SELF-review by the executor before reporting done.**
   Cheaper for controller context than dispatching a separate reviewer.
   Frame the review explicitly: "treat the code as if a different agent
   submitted it; you gain credit only for what you find, not for what
   you wrote." Severity-ranked questions (P0/P1/P2/P3):

   - **INVARIANT GAP** — does every <state-mutation> close the
     <conservation-law> exactly? does the <proof-artifact> actually
     prove it?
   - **SCOPE LEAK** — does the new code read or write any <resource> it
     did NOT declare in <scope-declaration>?
   - **MUTATION PURITY** — does any flipped call site still mutate
     <canonical-store> directly? (Grep for <mutator-pattern>; must be
     empty.)
   - **BEHAVIOR DRIFT** — are existing tests numerically identical?
     worst-case shadow-parity delta within tolerance?
   - **DEAD CODE** — any leftover legacy branches the flip rendered
     unreachable? sibling duplication that should be lifted into a
     shared helper?
   - **CONTRACT LEAK** — does <input-payload> carry the exact auxiliary
     data the legacy path needed, with the same units and names?
   - **INTEGRITY** — for authoritative units, does the new code mirror
     the legacy stoich/algorithm exactly, not a re-derivation?

   The executor self-fixes any P0/P1/P2 it finds before reporting done.
   P3 may be deferred with a note. The controller verifies the final
   diff briefly before committing — full multi-agent review machinery
   is reserved for milestone reviews, not per-flip.
7. Commit after the executor's self-review reports clean. One unit per commit.

ACCEPTANCE
- After all units flipped: no {{LEGACY_PATH}} mutates {{CANONICAL_STORE}} directly.
- All invariants from §7 of binding-spec hold.
- {{NUMERICAL_BASELINE}} unchanged within <tolerance> on a recorded fixture.
- All previously passing tests stay green; per-unit parity tests added.

FORBIDDEN
- Multi-unit flips per commit.
- Skipping the shadow-parity step.
- Removing legacy code before the new path has been load-bearing for
  one full smoke run.
```

---

## M. `\goal {{PHASED_SLUG}}` — phased rollout (iterative goal, distinct from multi-unit flip)

For goals whose work doesn't fit a single dispatch but isn't a per-unit flip pattern — e.g., test-regime build-out, corpus assembly, literature reproduction. First iteration lands framework + first cohort; subsequent iterations land additional cohorts as separate goals or commits.

```
SCOPE
{{Problem requiring iterative phased work; framework + first cohort lands this iteration.}}

PRECONDITION
- {{...}}

CHECKLIST (FIRST ITERATION — phases A + B scope)

PHASE A — {{FRAMEWORK_NAME}} (this iteration)
1. {{...}}
2. {{...}}

PHASE B — {{FIRST_COHORT_NAME}} (this iteration)
For each {{ITERATION_UNIT}} (e.g., per-feedstock, per-engine, per-paper):
1. {{...}}
2. {{...}}

FUTURE ITERATIONS (separate goals or follow-up commits — do NOT bundle):
- PHASE C: {{SECOND_COHORT_NAME}}
- PHASE D: {{THIRD_COHORT_NAME}}
- PHASE E: {{...}}

ACCEPTANCE (this iteration)
- Phase A framework lands.
- All Phase B {{cohort}} tests pass OR are explicitly skipped with cited justification.
- {{Iteration-specific acceptance.}}
- docs/{{topic}}-{{phase}}.md documents the structure + cadence.

FORBIDDEN
- Bundling phases C+ into this iteration.
- {{Pattern-specific hard barriers — e.g., snapshot tests that lock in
  whatever the code currently produces without an independent expected value.}}
- Loosening tolerances to make tests pass; either fix the implementation
  or fix the expectation (with citation).

NOTE: name follow-up goals `{{TOPIC}}-COHORT-<NAME>` and reference back to this goal's
framework. The framework is stable; each cohort is a separate review surface.
```
