<!--
  Dual-subagent adversarial planning pattern.

  Use when the orchestrator hits an open architectural question where the right
  answer is unclear and reading code alone won't decide it. Examples:
    - "Should we extend the schema for X?"
    - "How should we handle Y edge case?"
    - "Is feature Z worth implementing now?"

  Dispatch TWO planning subagents in parallel, each with a different lens.
  Each writes a plan document; orchestrator synthesizes. Cheaper for context
  than one big planner because each subagent has a focused mandate and
  produces a bounded artifact.

  Field example: mixed-cation oxide vapor species.
    - Plan A (chemistry-first): "What does the thermodynamics literature say
      about these species and what would the schema need?"
    - Plan B (engineering-first): "What's the minimum schema extension and
      what's the implementation cost?"
    - Synthesis: both converged on "defer with trip-wire defensively." If
      A had said "huge flux contribution" and B "expensive," the tension
      would have been useful too.

  Do NOT use for questions a single subagent can answer cleanly. The dual
  pattern's value is in adversarial divergence — if both agents would
  produce near-identical plans, just dispatch one.
-->

DUAL-SUBAGENT ADVERSARIAL PLANNING — DISPATCH SHAPE

The orchestrator hits an open architectural question. Dispatch two
planning subagents in parallel (Agent tool, general-purpose, no
isolation). Each writes a plan document to `docs-private/`. Orchestrator
reads both, synthesizes a decision (often "do option C from neither"),
and records the decision plus the threshold that would force re-opening.

PROMPT TEMPLATE FOR PLAN A (LENS-ONE)

```
You are a planning subagent for the <project> orchestrator. Your task:
plan how the simulator should handle <question>.

You are NOT writing code. You are writing a plan document to
`docs-private/<topic>-plan-A-<lens-one>.md` (gitignored). A sibling
subagent is writing plan B (<lens-two>). The orchestrator will compare
and synthesize.

# Your angle: <lens-one description>

Approach the problem starting from <lens-one's first principles>:
- <question 1 about the problem>
- <question 2>
- <question 3>

# Context (read before writing)

<3-5 critical file paths the subagent should read>

# Deliverable: `docs-private/<topic>-plan-A-<lens-one>.md`

Cover:
1. <section 1>
2. <section 2>
3. <section 3 — recommendation>
4. <section 4 — open questions for the user>

# Hard constraints

- This is a research/planning task. NO code changes.
- Aim for 400-800 words; concise > verbose.
- Cite specific files, papers, or commits where possible.
- DO write the plan to the file path above.

# Report (under 200 words)

- File path of the deliverable
- Word count
- Your bottom-line recommendation (1 sentence)
- The 1-2 most important open questions
```

PROMPT TEMPLATE FOR PLAN B (LENS-TWO)

Same shape but with `<lens-two>` framing — typically the complementary
lens (chemistry vs engineering, performance vs correctness, near-term
vs long-term, etc.). The two lenses should be GENUINELY different —
not "be thorough" vs "be careful" but "approach from thermodynamics" vs
"approach from existing architecture."

SYNTHESIS PATTERN (controller-side)

When both reports land:
1. Read both files. Look for convergence (likely outcome) vs divergence
   (interesting tension worth surfacing to user).
2. If convergent: encode the decision in the goal queue and binding spec.
   Often the convergent answer is "defer with a defensive trip-wire" —
   document the trip-wire condition that would force re-opening.
3. If divergent: surface to user with the specific trade-off. Don't
   resolve unilaterally — the divergence is the data.

When dispatching, prefer `run_in_background: true` for both so they run
in parallel; the notifications arrive independently and the orchestrator
synthesizes when both have landed.
