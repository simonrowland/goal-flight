---
schema_version: 1
description: >
  Golden Master of desired goal-flight controller behaviours. Source-of-truth
  declarative spec; SKILL.md is the compiled compressed distillation of this
  document. Adding a feature means adding an entry here FIRST, then re-distilling
  SKILL.md, then implementing.
entry_schema:
  id:
    type: string
    format: kebab-case
    required: true
    constraint: unique across all entries
  name:
    type: string
    required: true
    constraint: short human-readable name (≤60 chars)
  category:
    type: string
    required: true
    enum:
      - skill-load-and-order
      - compaction-and-resume
      - review-discipline
      - chat-discipline
      - dispatch-discipline
      - autonomous-throughput-and-status
      - capacity-and-rate-limits
      - worker-markers
      - verification-first-dispatch
      - test-gate
      - push-discipline
      - trigger-and-codename-hygiene
      - native-vs-non-native
      - worker-routing-defaults
      - state-layers
      - context-discipline
      - do-not
  controller_does:
    type: string
    required: true
    constraint: one sentence describing the desired controller action
  failure_mode:
    type: string
    required: true
    constraint: one sentence describing what counts as failure, with specific anti-pattern example
  skill_md_compressed_form:
    type: object
    required: true
    fields:
      kind:
        type: string
        enum: [literal, regex]
        default: literal
      pattern:
        type: string
        required: true
        constraint: text that must appear in SKILL.md verbatim (literal) or match (regex)
      max_section_lines:
        type: integer
        constraint: budget for the SKILL.md section containing this pattern
  verifier:
    type: object
    required: true
    fields:
      kind:
        type: string
        enum: [textual-invariant, behaviour-scenario, runtime-assertion, manual]
      id:
        type: string
        constraint: test name or scenario id (e.g. test_skill_structure, read-skill-end-to-end); 'manual' if no automated check
  provenance:
    type: object
    required: true
    fields:
      sources:
        type: array
        items: string
        constraint: file paths that contributed this entry (relative to repo root)
      r_numbers:
        type: array
        items: string
        constraint: R-numbers from the handoff backlog (e.g. R9, R18); empty if none
  severity:
    type: string
    required: true
    enum: [high, med, low]
    constraint: how load-bearing the entry is; high = test failures here are P0
  max_skill_lines:
    type: integer
    required: false
    constraint: budget for the dedicated H2 section in SKILL.md if the entry has its own section
  last_reviewed_commit:
    type: string
    required: true
    constraint: git short-SHA when this entry was last reviewed; bump on any field change
validation:
  total_entry_count:
    min: 50
    max: 120
    constraint: empirical band; adjust if structural changes shift the count
  per_category_min:
    skill-load-and-order: 1
    compaction-and-resume: 1
    review-discipline: 3
    chat-discipline: 1
    dispatch-discipline: 1
    autonomous-throughput-and-status: 2
    capacity-and-rate-limits: 2
    worker-markers: 1
    verification-first-dispatch: 1
    test-gate: 1
    push-discipline: 1
    trigger-and-codename-hygiene: 1
    native-vs-non-native: 1
    worker-routing-defaults: 1
    state-layers: 1
    context-discipline: 1
    do-not: 1
  unique_id: true
  anchor_uniqueness:
    constraint: no two entries' skill_md_compressed_form.pattern share a substring ≥80 chars
  provenance_path_exists:
    constraint: every path in provenance.sources must exist in repo
behaviours:
  - id: skill-load-order-mandatory
    see: '#entry-skill-load-order-mandatory'
---

# Goal-Flight Controller Behaviours — Golden Master

This is the source-of-truth declarative spec of desired goal-flight controller
behaviours. `SKILL.md` is the compiled compressed distillation of this document.
The hermetic test in `tests/python/test_skill_structure.py` (chunk-5) asserts
`SKILL.md` still distills every entry below.

When adding a new behaviour or feature to goal-flight:

1. Add the entry HERE first (with all required fields).
2. Re-distill `SKILL.md` to fit (a worker dispatch with this file as full context, or controller-direct for small changes).
3. The hermetic invariants test confirms `SKILL.md` still satisfies the spec.
4. Add a behaviour scenario under `tests/fixtures/controller_scenarios/` if the entry has a testable failure mode.
5. Only then implement the feature's runtime code.

This discipline is the explicit anti-impulse mechanism that prevents the
feature-add SKILL.md regression class observed in git history.

---

## Categories

The 17 behaviour categories are listed in the `entry_schema.category.enum`
frontmatter above. New categories require a frontmatter schema bump (raise
`schema_version`).

---

## Entries

Entries are flat under H2 `## Entries` (this section). Each entry is a single
H3 with fields below. Order does not matter; the `id` field is the canonical
reference. The hermetic test enumerates all H3 blocks and parses their fields.

### Entry: skill-load-order-mandatory

- **id:** `skill-load-order-mandatory`
- **name:** Read SKILL.md end-to-end at session start
- **category:** `skill-load-and-order`
- **controller_does:** At session start in a goal-flight-active repository, the controller reads `SKILL.md` **entirely end-to-end** before any command dispatch, and re-reads it after compaction when goal-flight is already in play (per `AGENTS.md` "Active run + compaction" rule).
- **failure_mode:** The controller skims `SKILL.md` to the command table or to "Hard Invariants" and stops, missing back-half sections (Worker Routing, Hard caps, Adaptive walkback, Controller-provider asymmetry, Context Discipline). Concrete anti-pattern: controller cites only the command table when asked "what's the routing default for code-writing chunks?", because it never reached `## Worker Routing` (line ~205 of current SKILL.md).
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Read this skill end-to-end"
    - **max_section_lines:** 10
    - **note:** the literal string must appear within the first 20 lines after YAML frontmatter (verified by the textual-invariant test in chunk-5).
- **verifier:**
    - **kind:** behaviour-scenario
    - **id:** read-skill-end-to-end
    - **fallback:** textual-invariant `test_skill_structure::test_read_skill_end_to_end_preamble` checks the literal pattern presence + position
- **provenance:**
    - **sources:**
      - `docs-private/HANDOFF-goal-flight-session-2026-05-24.md` (lines 14-27)
      - `docs-private/RESUME-NOTES-2026-05-20.md`
      - `docs-private/RESUME-NOTES-2026-05-21.md`
      - `docs-private/skill-vs-practice-assessment-2026-05-15.md`
    - **r_numbers:** [R2, R8, R10]
- **severity:** high
- **max_skill_lines:** 10 (the preamble section)
- **last_reviewed_commit:** (this commit — bump on first land via chunk-2.5)
- **notes:** Highest-recurrence regression in handoff history (observed ≥3 times). The "Read this skill end-to-end" preamble is the primary mechanical handle for catching skim-the-front-page failures; the behaviour-scenario `read-skill-end-to-end` (chunk-6) is the live-test handle.

---

## Adding a new entry

1. Pick a unique kebab-case `id` not already used.
2. Pick a `category` from the enum (or propose a new one — requires frontmatter `schema_version` bump and chunk-5 invariants test update).
3. Fill all required fields. Use the `skill-load-order-mandatory` exemplar above as a template.
4. Run `python3 tests/python/test_skill_structure.py` (or whichever test gates this file once chunk-5 lands) to confirm the schema + invariants still hold.
5. Re-distill `SKILL.md` so it carries the entry's `skill_md_compressed_form.pattern` in a stable anchor. The invariants test will fail until `SKILL.md` matches.
6. If the entry has a testable failure mode (`verifier.kind: behaviour-scenario`), add the scenario fixture under `tests/fixtures/controller_scenarios/<verifier.id>/prompt.md` plus an assertion function in `scripts/hosts/controller/behavior_scenario.py`.

---

## Roadmap for filling out the Golden Master

This file currently contains 1 exemplar entry. The full set of ~70-100 entries
will be filled in by chunks 3a (foundation R1-R19 + high-recurrence
regressions, ~25 entries), 3b (schema-encoded behaviours from
`adapters/agent-adapter.schema.json` and capacity/ledger scripts, ~25
entries), and 3c (long-tail past-steering items, ~30 entries). chunk-3d
dedupes overlap between 3a/3b/3c (especially capacity/dispatch/markers/gating
where past-steering and schema-encoded sources overlap).

Until those chunks land, this file's exemplar entry serves as both:
1. The schema template (chunk-5 invariants test can scaffold against it).
2. The first load-bearing Golden Master entry (highest-recurrence regression).
