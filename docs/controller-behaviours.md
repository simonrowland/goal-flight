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

### Entry: status-without-asking-hook-welcome

- **id:** `status-without-asking-hook-welcome`
- **name:** Report status without engagement bait
- **category:** `autonomous-throughput-and-status`
- **controller_does:** During an active goal-flight run, the controller gives concise progress status and keeps executing the prescribed next step instead of asking whether to continue.
- **failure_mode:** The controller finishes step 1, then asks "want me to continue with step 2?" even though the plan already authorizes step 2 and no real blocker exists.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Default is continue, not confirm"
    - **max_section_lines:** 70
- **verifier:**
    - **kind:** behaviour-scenario
    - **id:** continue-prescribed-step-two
- **provenance:**
    - **sources:**
      - `docs-private/HANDOFF-goal-flight-session-2026-05-24.md`
      - `SKILL.md`
    - **r_numbers:** [R1, R7]
- **severity:** high
- **last_reviewed_commit:** (chunk-3a)

### Entry: compaction-skill-reload-scoped

- **id:** `compaction-skill-reload-scoped`
- **name:** Reload skill only for active runs
- **category:** `compaction-and-resume`
- **controller_does:** After compaction, the controller reloads Goal Flight skill and resume protocol only when goal-flight was already in play before compaction.
- **failure_mode:** The controller treats every compacted session as a goal-flight run and reloads commands or starts queue work for an unrelated repository task.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Active run + compaction: reload Goal Flight only when already in play"
    - **max_section_lines:** 35
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_compaction_reload_scoped
- **provenance:**
    - **sources:**
      - `docs-private/HANDOFF-goal-flight-session-2026-05-24.md`
      - `docs-private/RESUME-NOTES-2026-05-27.md`
      - `SKILL.md`
    - **r_numbers:** [R2, R16]
- **severity:** high
- **last_reviewed_commit:** (chunk-3a)

### Entry: mid-session-ask-append-to-goal-queue

- **id:** `mid-session-ask-append-to-goal-queue`
- **name:** Queue mid-session user asks
- **category:** `chat-discipline`
- **controller_does:** When the user adds scope during an active run, the controller appends a compact row to the active goal queue before dispatch or implementation.
- **failure_mode:** The controller treats chat as the only backlog and launches a worker from the new ask without writing the queue update first.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "chat alone is not the backlog"
    - **max_section_lines:** 70
- **verifier:**
    - **kind:** behaviour-scenario
    - **id:** mid-session-ask-append-to-goal-queue
- **provenance:**
    - **sources:**
      - `docs-private/HANDOFF-goal-flight-session-2026-05-24.md`
      - `docs-private/RESUME-NOTES-2026-05-27.md`
      - `SKILL.md`
    - **r_numbers:** [R3, R18]
- **severity:** high
- **last_reviewed_commit:** (chunk-3a)

### Entry: no-blocking-cursor-task-worker

- **id:** `no-blocking-cursor-task-worker`
- **name:** Avoid blocking editor task workers
- **category:** `worker-routing-defaults`
- **controller_does:** The controller routes worker execution through ACP or bash-tail plus status polling, rather than blocking the interactive session on editor task panes.
- **failure_mode:** The controller opens a long editor task and waits synchronously in chat, preventing status polling, capacity checks, or review dispatch from continuing.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Use ACP or bash-tail plus status polling; do not block on editor task panes"
    - **max_section_lines:** 55
- **verifier:**
    - **kind:** behaviour-scenario
    - **id:** no-blocking-cursor-task-worker
- **provenance:**
    - **sources:**
      - `docs-private/HANDOFF-goal-flight-session-2026-05-24.md`
      - `SKILL.md`
    - **r_numbers:** [R4]
- **severity:** med
- **last_reviewed_commit:** (chunk-3a)

### Entry: autonomous-throughput-commit-as-complete

- **id:** `autonomous-throughput-commit-as-complete`
- **name:** Commit completed chunks locally
- **category:** `autonomous-throughput-and-status`
- **controller_does:** During execute, the controller commits each completed logical chunk after focused tests and independent review unless the user forbade commits for the run.
- **failure_mode:** The controller piles up several completed chunks as uncommitted work and waits for a separate "please commit" prompt despite the active goal-flight workflow.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Commits during execute follow **one commit per completed chunk**"
    - **max_section_lines:** 70
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_commit_as_complete
- **provenance:**
    - **sources:**
      - `docs-private/HANDOFF-goal-flight-session-2026-05-24.md`
      - `SKILL.md`
    - **r_numbers:** [R5]
- **severity:** high
- **last_reviewed_commit:** (chunk-3a)

### Entry: autoreview-complementary-not-default

- **id:** `autoreview-complementary-not-default`
- **name:** Keep autoreview complementary
- **category:** `review-discipline`
- **controller_does:** The controller treats `./scripts/autoreview.sh` as a complementary parallel review option, while gstack remains the default chunk review path.
- **failure_mode:** The controller replaces gstack review with autoreview and documents autoreview as the primary pre-commit reviewer.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "`./scripts/autoreview.sh` as a complementary parallel option"
    - **max_section_lines:** 70
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_autoreview_not_default
- **provenance:**
    - **sources:**
      - `docs-private/HANDOFF-goal-flight-session-2026-05-24.md`
      - `docs-private/RESUME-NOTES-2026-05-27.md`
      - `SKILL.md`
    - **r_numbers:** [R6, R9]
- **severity:** high
- **last_reviewed_commit:** (chunk-3a)

### Entry: user-status-cadence-15min

- **id:** `user-status-cadence-15min`
- **name:** Poll and report every 15 minutes
- **category:** `autonomous-throughput-and-status`
- **controller_does:** While workers, review jobs, or background verification are in flight, the controller polls compact state and gives the user a short update at least every 15 minutes unless context is tight.
- **failure_mode:** The controller lets workers run for an hour with no poll or status digest, then asks the user what to do next because it lost track of state.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "at least every 15 minutes"
    - **max_section_lines:** 20
- **verifier:**
    - **kind:** behaviour-scenario
    - **id:** user-status-cadence-15min
- **provenance:**
    - **sources:**
      - `docs-private/HANDOFF-goal-flight-session-2026-05-24.md`
      - `SKILL.md`
    - **r_numbers:** [R7]
- **severity:** high
- **last_reviewed_commit:** (chunk-3a)

### Entry: milestone-standalone-protocol

- **id:** `milestone-standalone-protocol`
- **name:** Keep milestone review separate
- **category:** `review-discipline`
- **controller_does:** The controller treats milestone review as a separate protocol gate from per-chunk review and invokes it at milestone cadence or milestone-marked chunks.
- **failure_mode:** The controller runs one diff-local chunk review, labels it a milestone review, and skips the broader concern-diverse milestone sweep.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "a separate gate from chunk review"
    - **max_section_lines:** 70
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_milestone_review_separate
- **provenance:**
    - **sources:**
      - `docs-private/HANDOFF-goal-flight-session-2026-05-24.md`
      - `docs-private/reviews/2026-05-27-skill-regression-plan/consolidated-findings.md`
      - `SKILL.md`
    - **r_numbers:** [R8]
- **severity:** high
- **last_reviewed_commit:** (chunk-3a)

### Entry: review-layers-three-distinct

- **id:** `review-layers-three-distinct`
- **name:** Preserve three review layers
- **category:** `review-discipline`
- **controller_does:** The controller keeps executor self-review, per-chunk review, and milestone review as distinct review layers with different scopes.
- **failure_mode:** The controller collapses executor self-review and milestone review into one generic "review this diff" worker prompt.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Review layers: executor self-review, chunk review, milestone review"
    - **max_section_lines:** 45
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_review_layers_distinct
- **provenance:**
    - **sources:**
      - `docs-private/HANDOFF-goal-flight-session-2026-05-24.md`
      - `docs-private/reviews/2026-05-27-skill-regression-plan/consolidated-findings.md`
      - `docs-private/plans/skill-regression-test-plan-2026-05-27.md`
    - **r_numbers:** [R8, R19]
- **severity:** high
- **last_reviewed_commit:** (chunk-3a)

### Entry: gstack-default-review-chunk

- **id:** `gstack-default-review-chunk`
- **name:** Use gstack for chunk review
- **category:** `review-discipline`
- **controller_does:** Before a chunk commit, the controller uses gstack `/review` as the default independent review path and may add complementary reviewers in parallel.
- **failure_mode:** The controller skips gstack and uses only a local script or ad hoc worker review before committing a chunk.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "default gstack `/review`"
    - **max_section_lines:** 70
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_gstack_default_review
- **provenance:**
    - **sources:**
      - `docs-private/HANDOFF-goal-flight-session-2026-05-24.md`
      - `docs-private/RESUME-NOTES-2026-05-27.md`
      - `SKILL.md`
    - **r_numbers:** [R9]
- **severity:** high
- **last_reviewed_commit:** (chunk-3a)

### Entry: skill-organization-navigation-map

- **id:** `skill-organization-navigation-map`
- **name:** Keep SKILL navigation map
- **category:** `skill-load-and-order`
- **controller_does:** The skill distillation keeps a compact navigation map from Golden Master behaviour to SKILL anchor to related protocol or script.
- **failure_mode:** The controller adds new behaviour text to scattered SKILL sections without a map, so non-native hosts miss rate-limit, review, or compaction rules.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Navigation map: behaviour -> SKILL anchor -> protocol/script"
    - **max_section_lines:** 45
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_navigation_map
- **provenance:**
    - **sources:**
      - `docs-private/HANDOFF-goal-flight-session-2026-05-24.md`
      - `docs-private/plans/skill-regression-test-plan-2026-05-27.md`
    - **r_numbers:** [R10, R15, R17]
- **severity:** high
- **last_reviewed_commit:** (chunk-3a)

### Entry: maintainer-autoreview-env-optional

- **id:** `maintainer-autoreview-env-optional`
- **name:** Make autoreview env opt-in
- **category:** `test-gate`
- **controller_does:** The maintainer-only autoreview tier stays opt-in behind `GOALFLIGHT_AUTOREVIEW=1` and never becomes part of the default test path.
- **failure_mode:** The controller makes autoreview mandatory in `./tests/run.sh`, causing regular contributors to depend on an optional maintainer engine.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "GOALFLIGHT_AUTOREVIEW=1 is an optional maintainer tier, not a default review path"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_autoreview_env_optional
- **provenance:**
    - **sources:**
      - `docs-private/HANDOFF-goal-flight-session-2026-05-24.md`
    - **r_numbers:** [R11]
- **severity:** med
- **last_reviewed_commit:** (chunk-3a)

### Entry: wave2-scenarios-registered

- **id:** `wave2-scenarios-registered`
- **name:** Register Wave 2 scenarios
- **category:** `verification-first-dispatch`
- **controller_does:** The controller regression harness registers Wave 2 scenarios for draft-goal office hours, vague-goal premise backlog, and context load order.
- **failure_mode:** The controller lands only the scenario prompt files and forgets assertion registration, so Wave 2 appears present but never runs.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Wave 2 scenarios: draft-goal-office-hours, vague-goal-premise-backlog, context-load-order"
    - **max_section_lines:** 40
- **verifier:**
    - **kind:** runtime-assertion
    - **id:** test_controller_probe_matrix::test_wave2_scenarios_registered
- **provenance:**
    - **sources:**
      - `docs-private/HANDOFF-goal-flight-session-2026-05-24.md`
      - `docs-private/plans/skill-regression-test-plan-2026-05-27.md`
    - **r_numbers:** [R12]
- **severity:** med
- **last_reviewed_commit:** (chunk-3a)

### Entry: controller-probe-runner-portability

- **id:** `controller-probe-runner-portability`
- **name:** Keep controller probes portable
- **category:** `native-vs-non-native`
- **controller_does:** Live controller probes use the ACP shim and later multi-controller runner abstractions so host-specific tooling does not leak into portable behaviour tests.
- **failure_mode:** The controller hard-codes one host's print-mode command in the live probe and declares other controller hosts unsupported by design.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Controller behaviour probes run through portable host adapters, not host-specific print-mode shortcuts"
    - **max_section_lines:** 45
- **verifier:**
    - **kind:** runtime-assertion
    - **id:** test-controller-behavior-acp-shim
- **provenance:**
    - **sources:**
      - `docs-private/HANDOFF-goal-flight-session-2026-05-24.md`
      - `docs-private/plans/skill-regression-test-plan-2026-05-27.md`
    - **r_numbers:** [R13, R14]
- **severity:** med
- **last_reviewed_commit:** (chunk-3a)

### Entry: skill-invariants-textual-guard

- **id:** `skill-invariants-textual-guard`
- **name:** Assert SKILL compressed forms
- **category:** `test-gate`
- **controller_does:** The hermetic skill-structure test asserts that SKILL.md contains each Golden Master entry's compressed-form pattern.
- **failure_mode:** A feature branch deletes the compaction or gstack wording from SKILL.md and no test fails because the Golden Master is not checked mechanically.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "For each Golden Master entry, SKILL.md contains the entry's compressed-form text"
    - **max_section_lines:** 45
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure
- **provenance:**
    - **sources:**
      - `docs-private/RESUME-NOTES-2026-05-27.md`
      - `docs-private/plans/skill-regression-test-plan-2026-05-27.md`
    - **r_numbers:** [R15]
- **severity:** high
- **last_reviewed_commit:** (chunk-3a)

### Entry: read-skill-end-to-end-behaviour

- **id:** `read-skill-end-to-end-behaviour`
- **name:** Live-test full skill reading
- **category:** `skill-load-and-order`
- **controller_does:** The behaviour scenario proves the controller read past the front matter by requiring use of back-half sections such as Worker Routing, State, and Context Discipline.
- **failure_mode:** The controller passes a shallow load-order check by quoting the preamble but cannot answer routing or state questions from later SKILL.md sections.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Read this skill end-to-end, including Worker Routing, State, and Context Discipline"
    - **max_section_lines:** 15
- **verifier:**
    - **kind:** behaviour-scenario
    - **id:** read-skill-end-to-end
- **provenance:**
    - **sources:**
      - `docs-private/RESUME-NOTES-2026-05-27.md`
      - `docs-private/plans/skill-regression-test-plan-2026-05-27.md`
      - `SKILL.md`
    - **r_numbers:** [R16]
- **severity:** high
- **last_reviewed_commit:** (chunk-3a)

### Entry: compaction-reload-skill-behaviour

- **id:** `compaction-reload-skill-behaviour`
- **name:** Live-test compaction reload
- **category:** `compaction-and-resume`
- **controller_does:** The compaction scenario verifies the controller reloads SKILL.md and `commands/resume.md` before continuing an already-active run.
- **failure_mode:** After compaction, the controller relies on the lossy summary and resumes execution without reloading the active run's skill and resume instructions.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "After compaction, if goal-flight was active, reload SKILL.md and commands/resume.md before continuing"
    - **max_section_lines:** 35
- **verifier:**
    - **kind:** behaviour-scenario
    - **id:** compaction-reload-skill
- **provenance:**
    - **sources:**
      - `docs-private/HANDOFF-goal-flight-session-2026-05-24.md`
      - `docs-private/RESUME-NOTES-2026-05-27.md`
      - `docs-private/plans/skill-regression-test-plan-2026-05-27.md`
    - **r_numbers:** [R2, R16]
- **severity:** high
- **last_reviewed_commit:** (chunk-3a)

### Entry: review-flight-at-completion-behaviour

- **id:** `review-flight-at-completion-behaviour`
- **name:** Live-test completion review
- **category:** `review-discipline`
- **controller_does:** The completion scenario verifies the controller dispatches the canonical review path when a chunk is done and before it commits.
- **failure_mode:** The controller marks a chunk complete and commits it after executor self-review only, with no independent gstack review flight.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "On chunk completion, dispatch gstack `/review` before committing"
    - **max_section_lines:** 45
- **verifier:**
    - **kind:** behaviour-scenario
    - **id:** review-flight-at-completion
- **provenance:**
    - **sources:**
      - `docs-private/RESUME-NOTES-2026-05-27.md`
      - `docs-private/plans/skill-regression-test-plan-2026-05-27.md`
      - `docs-private/reviews/2026-05-27-skill-regression-plan/consolidated-findings.md`
    - **r_numbers:** [R5, R16, R19]
- **severity:** high
- **last_reviewed_commit:** (chunk-3a)

### Entry: per-host-pointer-for-non-native

- **id:** `per-host-pointer-for-non-native`
- **name:** Point non-native hosts to skill
- **category:** `native-vs-non-native`
- **controller_does:** SKILL.md and agent instructions include explicit per-host pointers so non-native controllers can find the installed wrapper and load order.
- **failure_mode:** A non-native controller misses SKILL.md because it only sees host-local front matter and no pointer tells it where the Goal Flight wrapper lives.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Per-host pointers tell non-native controllers where their installed wrapper lives"
    - **max_section_lines:** 45
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_per_host_pointers
- **provenance:**
    - **sources:**
      - `docs-private/RESUME-NOTES-2026-05-27.md`
      - `docs-private/plans/skill-regression-test-plan-2026-05-27.md`
    - **r_numbers:** [R17]
- **severity:** high
- **last_reviewed_commit:** (chunk-3a)

### Entry: controller-chat-is-requirements-not-inline-editor

- **id:** `controller-chat-is-requirements-not-inline-editor`
- **name:** Treat chat as requirements
- **category:** `chat-discipline`
- **controller_does:** During an active run, the controller treats user chat as requirements, steering, or architecture input that may update the goal queue or plan before dispatch.
- **failure_mode:** The controller abandons the current chunk and starts inline-editing a new user ask immediately on receipt, bypassing queue update and reviewer pass.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Controller chat is requirements input, not an inline editor command"
    - **max_section_lines:** 45
- **verifier:**
    - **kind:** behaviour-scenario
    - **id:** chat-as-requirements-scenario
- **provenance:**
    - **sources:**
      - `docs-private/RESUME-NOTES-2026-05-27.md`
      - `docs-private/plans/skill-regression-test-plan-2026-05-27.md`
    - **r_numbers:** [R18]
- **severity:** high
- **last_reviewed_commit:** (chunk-3a)

### Entry: gstack-review-and-challenge-canonical

- **id:** `gstack-review-and-challenge-canonical`
- **name:** Use canonical review interfaces
- **category:** `review-discipline`
- **controller_does:** The controller sends structural and adversarial reviews through gstack `/review` and `/challenge` instead of writing custom review prompts.
- **failure_mode:** The controller hand-rolls a "please review this diff" worker prompt and treats that direct worker result as equivalent to the canonical gstack review interface.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Reviews go through gstack `/review` and `/challenge`; do not hand-roll review prompts"
    - **max_section_lines:** 45
- **verifier:**
    - **kind:** behaviour-scenario
    - **id:** review-flight-at-completion
- **provenance:**
    - **sources:**
      - `docs-private/RESUME-NOTES-2026-05-27.md`
      - `docs-private/plans/skill-regression-test-plan-2026-05-27.md`
      - `docs-private/reviews/2026-05-27-skill-regression-plan/consolidated-findings.md`
    - **r_numbers:** [R19]
- **severity:** high
- **last_reviewed_commit:** (chunk-3a)

### Entry: tests-run-green-before-commit

- **id:** `tests-run-green-before-commit`
- **name:** Require green tests before commit
- **category:** `test-gate`
- **controller_does:** The controller waits for focused tests to finish green before creating a commit for a completed chunk.
- **failure_mode:** The controller starts tests in the background and commits while they are still running, then fixes failures in follow-up commits.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Before each chunk commit: focused tests green"
    - **max_section_lines:** 70
- **verifier:**
    - **kind:** runtime-assertion
    - **id:** test_instruction_split_contract
- **provenance:**
    - **sources:**
      - `docs-private/RESUME-NOTES-2026-05-27.md`
      - `SKILL.md`
    - **r_numbers:** [R5]
- **severity:** high
- **last_reviewed_commit:** (chunk-3a)

### Entry: push-explicit-permission-only

- **id:** `push-explicit-permission-only`
- **name:** Push only with permission
- **category:** `push-discipline`
- **controller_does:** The controller may commit locally as chunks complete but pushes to a remote only after tests pass and the user explicitly permits publish.
- **failure_mode:** The controller treats local commit permission as push permission and publishes a branch without the user's explicit approval.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Push to a remote only after the relevant tests pass and the user has permitted publish"
    - **max_section_lines:** 70
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_push_requires_permission
- **provenance:**
    - **sources:**
      - `docs-private/RESUME-NOTES-2026-05-27.md`
      - `SKILL.md`
    - **r_numbers:** [R5]
- **severity:** high
- **last_reviewed_commit:** (chunk-3a)

### Entry: executor-self-review-7categories

- **id:** `executor-self-review-7categories`
- **name:** Preserve executor self-review
- **category:** `verification-first-dispatch`
- **controller_does:** Each executor handoff retains the compact seven-category self-review so obvious failures are caught before independent review.
- **failure_mode:** The controller removes executor self-review from dispatch prompts because gstack will review later, losing the immediate design-rationale check.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Executor self-review covers seven categories before handing off a chunk"
    - **max_section_lines:** 45
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_executor_self_review
- **provenance:**
    - **sources:**
      - `docs-private/design-rationale-2026-05-15.md`
      - `docs-private/plans/skill-regression-test-plan-2026-05-27.md`
    - **r_numbers:** [R8]
- **severity:** med
- **last_reviewed_commit:** (chunk-3a)

### Entry: fabricated-approval-rejected

- **id:** `fabricated-approval-rejected`
- **name:** Reject fabricated approval
- **category:** `dispatch-discipline`
- **controller_does:** The controller treats approval, publish permission, and destructive permission as externally supplied facts and never fabricates them to skip a gate.
- **failure_mode:** The controller writes "user approved" into a plan or status note without a corresponding user message, then uses that invented approval to bypass review or push gates.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Never invent user approval for a gated step"
    - **max_section_lines:** 35
- **verifier:**
    - **kind:** behaviour-scenario
    - **id:** fabricated-approval-scenario
- **provenance:**
    - **sources:**
      - `docs-private/plans/skill-regression-test-plan-2026-05-27.md`
      - `docs-private/reviews/2026-05-27-skill-regression-plan/consolidated-findings.md`
    - **r_numbers:** [R18, R19]
- **severity:** high
- **last_reviewed_commit:** (chunk-3a)

### Entry: capacity-check-before-spawn

- **id:** `capacity-check-before-spawn`
- **name:** Consider capacity before spawning
- **category:** `capacity-and-rate-limits`
- **controller_does:** Before spawning workers or review jobs, the controller considers existing worker pressure and provider limits rather than treating parallelism as free.
- **failure_mode:** The controller fans out workers because the queue is long, ignores existing sessions and provider pressure, and triggers avoidable rate-limit stalls.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "No worker spawn without capacity consideration"
    - **max_section_lines:** 35
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_capacity_consideration
- **provenance:**
    - **sources:**
      - `docs-private/HANDOFF-goal-flight-session-2026-05-24.md`
      - `SKILL.md`
    - **r_numbers:** [R4, R7]
- **severity:** high
- **last_reviewed_commit:** (chunk-3a)

### Entry: worker-markers-status-path-required

- **id:** `worker-markers-status-path-required`
- **name:** Require worker status paths
- **category:** `worker-markers`
- **controller_does:** Every long worker or review job has a durable status path, and the controller treats worker marker lines as compact status rather than conversational prose.
- **failure_mode:** The controller launches a long job with no status path, then relies on streamed chat output and cannot reconstruct worker state when the session compacts.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Every long worker or review job needs a ledger/status path"
    - **max_section_lines:** 35
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_worker_status_path_required
- **provenance:**
    - **sources:**
      - `docs-private/HANDOFF-goal-flight-session-2026-05-24.md`
      - `SKILL.md`
    - **r_numbers:** [R7, R16]
- **severity:** high
- **last_reviewed_commit:** (chunk-3a)

### Entry: state-layers-separated

- **id:** `state-layers-separated`
- **name:** Keep three state layers
- **category:** `state-layers`
- **controller_does:** The controller separates project state, machine state, and conversation state when resuming, reporting, or dispatching work.
- **failure_mode:** The controller treats chat memory as the state of record and ignores git, queue files, dispatch ledgers, or cooldown state after resume.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Use three state layers"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_state_layers
- **provenance:**
    - **sources:**
      - `docs-private/HANDOFF-goal-flight-session-2026-05-24.md`
      - `SKILL.md`
    - **r_numbers:** [R2, R3, R7]
- **severity:** med
- **last_reviewed_commit:** (chunk-3a)

### Entry: context-mode-for-analysis

- **id:** `context-mode-for-analysis`
- **name:** Use code for large analysis
- **category:** `context-discipline`
- **controller_does:** The controller keeps large reads, counts, searches, and comparisons in procedural analysis tools and returns only compact derived answers.
- **failure_mode:** The controller dumps a raw long file or command output into chat, flooding the context window and forcing later compaction.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Analyze/search/count/filter with procedural code or context-mode"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_context_discipline
- **provenance:**
    - **sources:**
      - `docs-private/HANDOFF-goal-flight-session-2026-05-24.md`
      - `SKILL.md`
    - **r_numbers:** [R10]
- **severity:** med
- **last_reviewed_commit:** (chunk-3a)

### Entry: trigger-codename-hygiene

- **id:** `trigger-codename-hygiene`
- **name:** Keep trigger names out of git
- **category:** `trigger-and-codename-hygiene`
- **controller_does:** The controller maps host aliases and trigger-prone labels to neutral tracked names before creating manifests, filenames, branch names, or commit messages.
- **failure_mode:** The controller writes a host alias directly into a manifest filename or commit message, making the billing-trigger name git-visible.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Git-visible trigger aliases stay out of filenames, manifests, and commit messages"
    - **max_section_lines:** 30
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_trigger_codename_hygiene
- **provenance:**
    - **sources:**
      - `AGENTS.md`
      - `docs-private/plans/skill-regression-test-plan-2026-05-27.md`
    - **r_numbers:** [R17, R19]
- **severity:** high
- **last_reviewed_commit:** (chunk-3a)

### Entry: no-print-mode-review-for-live-probes

- **id:** `no-print-mode-review-for-live-probes`
- **name:** Do not fake live probes
- **category:** `do-not`
- **controller_does:** The controller keeps live behaviour probes and canonical review dispatches on their documented paths rather than substituting direct print-mode prompts.
- **failure_mode:** The controller runs a direct print-mode review command, records it as an ACP behaviour probe, and declares the live suite covered.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Do not substitute print-mode prompts for live behaviour probes or canonical review dispatch"
    - **max_section_lines:** 35
- **verifier:**
    - **kind:** behaviour-scenario
    - **id:** no-print-mode-review-for-live-probes
- **provenance:**
    - **sources:**
      - `docs-private/HANDOFF-goal-flight-session-2026-05-24.md`
      - `docs-private/RESUME-NOTES-2026-05-27.md`
      - `docs-private/plans/skill-regression-test-plan-2026-05-27.md`
    - **r_numbers:** [R13, R19]
- **severity:** med
- **last_reviewed_commit:** (chunk-3a)

### Entry: plan-before-inline-edits

- **id:** `plan-before-inline-edits`
- **name:** Plan before unsettled edits
- **category:** `chat-discipline`
- **controller_does:** When scope or direction is unsettled, the controller writes or updates the plan before editing files inline.
- **failure_mode:** The controller starts patching from a mid-session steering message, then discovers the user wanted planning, reviewer convergence, or a different chunk boundary first.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Plan before editing when scope is unsettled"
    - **max_section_lines:** 20
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_plan_before_unsettled_edits
- **provenance:**
    - **sources:**
      - `docs-private/RESUME-NOTES-2026-05-27.md`
      - `docs-private/design-rationale-2026-05-15.md`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3c)

### Entry: background-tests-pending

- **id:** `background-tests-pending`
- **name:** Treat background tests as pending
- **category:** `test-gate`
- **controller_does:** The controller treats a background or in-flight test run as pending until it has read the final result.
- **failure_mode:** The controller commits while tests are still running, then learns the chunk introduced failures that should have blocked the commit.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Background tests are pending until results are read"
    - **max_section_lines:** 20
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_background_tests_pending
- **provenance:**
    - **sources:**
      - `docs-private/RESUME-NOTES-2026-05-27.md`
    - **r_numbers:** []
- **severity:** high
- **last_reviewed_commit:** (chunk-3c)

### Entry: reviewer-misses-regression-tests

- **id:** `reviewer-misses-regression-tests`
- **name:** Convert review misses to tests
- **category:** `review-discipline`
- **controller_does:** When an independent review misses a concrete defect, the controller turns that miss into a regression guard instead of weakening the review requirement.
- **failure_mode:** The controller treats a missed host-tool leak as proof that review is useless, rather than adding a test that catches the leak class next time.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Reviewer misses become regression tests, not trust exemptions"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_reviewer_misses_regression_tests
- **provenance:**
    - **sources:**
      - `docs-private/RESUME-NOTES-2026-05-27.md`
      - `docs-private/skill-vs-practice-assessment-2026-05-15.md`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3c)

### Entry: classify-acp-failure-layer

- **id:** `classify-acp-failure-layer`
- **name:** Classify ACP failure layers
- **category:** `state-layers`
- **controller_does:** The controller classifies ACP failures by layer before changing goal-flight code: upstream shim, local config, adapter bridge, or repository regression.
- **failure_mode:** The controller patches goal-flight for an upstream prompt-readiness timeout or a local MCP approval stall, obscuring the real owner of the failure.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Classify ACP failures as upstream, local, or repo"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_classify_acp_failure_layer
- **provenance:**
    - **sources:**
      - `docs-private/RESUME-NOTES-2026-05-27.md`
      - `docs-private/codex-stall-investigation-2026-05-15.md`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3c)

### Entry: controller-direct-plan-marked

- **id:** `controller-direct-plan-marked`
- **name:** Restrict controller-direct chunks
- **category:** `dispatch-discipline`
- **controller_does:** The controller uses controller-direct only for tiny doc/test edits or chunks that the active plan explicitly marks as controller-direct.
- **failure_mode:** The controller performs implementation directly because dispatch is inconvenient, even though the plan called for worker execution or reviewable chunk isolation.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Controller-direct only for tiny or plan-marked chunks"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_controller_direct_plan_marked
- **provenance:**
    - **sources:**
      - `docs-private/RESUME-NOTES-2026-05-27.md`
      - `docs-private/HANDOFF-goal-flight-session-2026-05-24.md`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3c)

### Entry: question-prep-before-ask

- **id:** `question-prep-before-ask`
- **name:** Prepare before asking user
- **category:** `chat-discipline`
- **controller_does:** Before asking an anticipatory or ambiguous question, the controller uses available context or subagent preparation to narrow it to a high-signal decision.
- **failure_mode:** The controller asks the user a raw broad question that a quick synthesis pass could have turned into a small set of concrete choices.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Prepare ambiguous questions before asking the user"
    - **max_section_lines:** 20
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_question_prep_before_ask
- **provenance:**
    - **sources:**
      - `docs-private/lessons-learned-2026-05-15.md`
      - `docs-private/design-rationale-2026-05-15.md`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3c)

### Entry: rubric-before-wave

- **id:** `rubric-before-wave`
- **name:** Define review rubric first
- **category:** `review-discipline`
- **controller_does:** For a new wave of review or slice building, the controller writes the score rubric before dispatching the first reviewer.
- **failure_mode:** The controller invents the rubric mid-build, making early review outputs inconsistent with later review expectations.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Write review rubrics before first wave dispatch"
    - **max_section_lines:** 20
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_rubric_before_wave
- **provenance:**
    - **sources:**
      - `docs-private/lessons-learned-2026-05-15.md`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3c)

### Entry: concern-diverse-review

- **id:** `concern-diverse-review`
- **name:** Diversify review concerns
- **category:** `review-discipline`
- **controller_does:** For broad refactors, the controller diversifies reviewer concerns as well as models or providers.
- **failure_mode:** The controller launches two reviewers with the same lens and misses a cross-domain integrity issue that concern-diverse review would have surfaced.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Diversify reviewer concern, not just model"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_concern_diverse_review
- **provenance:**
    - **sources:**
      - `docs-private/skill-vs-practice-assessment-2026-05-15.md`
      - `docs-private/RESUME-NOTES-2026-05-20.md`
    - **r_numbers:** []
- **severity:** high
- **last_reviewed_commit:** (chunk-3c)

### Entry: cross-slice-consolidation

- **id:** `cross-slice-consolidation`
- **name:** Consolidate across slices
- **category:** `review-discipline`
- **controller_does:** The controller runs a single cross-slice consolidation pass when correctness depends on contradictions between independently built slices.
- **failure_mode:** The controller accepts clean per-slice reviews while missing that two slices encode mutually incompatible invariants.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Use consolidation review for cross-slice contradictions"
    - **max_section_lines:** 30
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_cross_slice_consolidation
- **provenance:**
    - **sources:**
      - `docs-private/lessons-learned-2026-05-15.md`
      - `docs-private/design-rationale-2026-05-15.md`
    - **r_numbers:** []
- **severity:** high
- **last_reviewed_commit:** (chunk-3c)

### Entry: agents-md-diff-only

- **id:** `agents-md-diff-only`
- **name:** Preserve AGENTS.md authority
- **category:** `state-layers`
- **controller_does:** The controller proposes AGENTS.md changes as reviewable diffs and never overwrites the user-owned canonical instructions during init or corpus work.
- **failure_mode:** The controller silently edits AGENTS.md to match a derived corpus summary, reversing the source-of-truth direction.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Propose AGENTS.md changes as diffs only"
    - **max_section_lines:** 20
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_agents_md_diff_only
- **provenance:**
    - **sources:**
      - `docs-private/design-rationale-2026-05-15.md`
      - `docs-private/lessons-learned-2026-05-15.md`
    - **r_numbers:** []
- **severity:** high
- **last_reviewed_commit:** (chunk-3c)

### Entry: build-corpus-eagerly

- **id:** `build-corpus-eagerly`
- **name:** Build corpus as audit
- **category:** `verification-first-dispatch`
- **controller_does:** The controller treats corpus construction as an early audit of source truth, even when the repository already has maintained instructions.
- **failure_mode:** The controller skips corpus build because AGENTS.md exists and misses contradictions between the canonical instructions and the enforced tests.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Build corpus eagerly; it audits source truth"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_build_corpus_eagerly
- **provenance:**
    - **sources:**
      - `docs-private/lessons-learned-2026-05-15.md`
      - `docs-private/design-rationale-2026-05-15.md`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3c)

### Entry: corpus-primary-sources

- **id:** `corpus-primary-sources`
- **name:** Use primary corpus sources
- **category:** `verification-first-dispatch`
- **controller_does:** The controller builds corpus slices from primary source documents rather than from another precis or summary layer.
- **failure_mode:** The controller lets a slice builder summarize an already-compressed precis, amplifying omissions and turning a brevity choice into a false invariant.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Use primary sources, not precis, for corpus slices"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_corpus_primary_sources
- **provenance:**
    - **sources:**
      - `docs-private/design-rationale-2026-05-15.md`
      - `docs-private/future-work.md`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3c)

### Entry: corpus-init-not-inline

- **id:** `corpus-init-not-inline`
- **name:** Prebuild corpus, do not inline
- **category:** `context-discipline`
- **controller_does:** The controller prebuilds the RAG corpus at init and avoids inlining the full project landscape into every dispatch prompt.
- **failure_mode:** The controller spends composition budget repeatedly hand-pasting the same landscape, leaving less attention for chunk-specific routing and verification.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Prebuild corpus; do not inline landscape per dispatch"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_corpus_init_not_inline
- **provenance:**
    - **sources:**
      - `docs-private/design-rationale-2026-05-15.md`
      - `docs-private/future-work.md`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3c)

### Entry: typed-dispatch-wrappers

- **id:** `typed-dispatch-wrappers`
- **name:** Type each dispatch wrapper
- **category:** `dispatch-discipline`
- **controller_does:** The controller marks dispatches as executor, reviewer, or planner and uses the wrapper shape appropriate to that role.
- **failure_mode:** The controller gives a reviewer executor context it does not need, or gives a planner a code-writing wrapper that invites implementation drift.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Type dispatches as executor, reviewer, or planner"
    - **max_section_lines:** 30
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_typed_dispatch_wrappers
- **provenance:**
    - **sources:**
      - `docs-private/design-rationale-2026-05-15.md`
      - `docs-private/skill-vs-practice-assessment-2026-05-15.md`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3c)

### Entry: dispatch-wrapper-five-layers

- **id:** `dispatch-wrapper-five-layers`
- **name:** Preserve five-layer prompts
- **category:** `dispatch-discipline`
- **controller_does:** The controller constructs executor prompts with the full layered wrapper instead of appending only raw goal text and a self-review pointer.
- **failure_mode:** The controller sends a short generic task prompt and loses the situational frame, template pointer, file anchors, environment caveats, and specialized self-review.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Dispatch prompts need the five-layer wrapper"
    - **max_section_lines:** 30
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_dispatch_wrapper_five_layers
- **provenance:**
    - **sources:**
      - `docs-private/skill-vs-practice-assessment-2026-05-15.md`
      - `docs-private/design-rationale-2026-05-15.md`
    - **r_numbers:** []
- **severity:** high
- **last_reviewed_commit:** (chunk-3c)

### Entry: self-review-specialize

- **id:** `self-review-specialize`
- **name:** Specialize self-review bullets
- **category:** `verification-first-dispatch`
- **controller_does:** The controller rewrites portable self-review categories into project-specific nouns, grep patterns, and invariants before dispatch.
- **failure_mode:** The controller pastes abstract self-review categories unchanged, so the worker checks generic integrity instead of the chunk's actual failure modes.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Specialize self-review bullets to project nouns"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_self_review_specialize
- **provenance:**
    - **sources:**
      - `docs-private/skill-vs-practice-assessment-2026-05-15.md`
      - `docs-private/lessons-learned-2026-05-15.md`
    - **r_numbers:** []
- **severity:** high
- **last_reviewed_commit:** (chunk-3c)

### Entry: parallel-fix-forbid-lists

- **id:** `parallel-fix-forbid-lists`
- **name:** Bound parallel fix ownership
- **category:** `dispatch-discipline`
- **controller_does:** The controller gives parallel fix clusters explicit ownership and forbid lists so workers do not race across slices.
- **failure_mode:** Two workers edit the same module family because their prompts described the bug class but not the files they must avoid.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Parallel fix clusters need explicit forbid lists"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_parallel_fix_forbid_lists
- **provenance:**
    - **sources:**
      - `docs-private/skill-vs-practice-assessment-2026-05-15.md`
      - `docs-private/lessons-learned-2026-05-15.md`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3c)

### Entry: split-large-chunk-scope

- **id:** `split-large-chunk-scope`
- **name:** Split broad chunk scopes
- **category:** `dispatch-discipline`
- **controller_does:** The controller splits a chunk before dispatch when the likely file count or line delta makes first-shot completion unlikely.
- **failure_mode:** The controller asks one worker for adapter, tests, and docs in a single broad pass, then needs a corrective follow-up to finish the missed surfaces.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Split chunks likely to touch many files"
    - **max_section_lines:** 20
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_split_large_chunk_scope
- **provenance:**
    - **sources:**
      - `docs-private/skill-vs-practice-assessment-2026-05-15.md`
      - `docs-private/HANDOFF-goal-flight-session-2026-05-24.md`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3c)

### Entry: worker-context-optional

- **id:** `worker-context-optional`
- **name:** Keep worker context optional
- **category:** `context-discipline`
- **controller_does:** The controller treats worker-context.md as optional when canonical docs and modern context windows are enough.
- **failure_mode:** The controller mandates a curated worker-context precis, then lets that precis drift from AGENTS.md and other canonical source documents.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Keep worker-context optional when canonical docs fit"
    - **max_section_lines:** 20
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_worker_context_optional
- **provenance:**
    - **sources:**
      - `docs-private/design-rationale-2026-05-15.md`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3c)

### Entry: source-truth-self-consistency

- **id:** `source-truth-self-consistency`
- **name:** Check source-truth consistency
- **category:** `verification-first-dispatch`
- **controller_does:** Before building a corpus from multiple canonical inputs, the controller checks for internal contradictions that a slice builder would otherwise choose between silently.
- **failure_mode:** The controller lets one slice encode a stale AGENTS.md claim while another follows the binding spec, hiding the contradiction until implementation fails.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Check source-truth contradictions before corpus build"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_source_truth_self_consistency
- **provenance:**
    - **sources:**
      - `docs-private/future-work.md`
      - `docs-private/lessons-learned-2026-05-15.md`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3c)

### Entry: bounded-dispatch-timeouts

- **id:** `bounded-dispatch-timeouts`
- **name:** Bound dispatch hang time
- **category:** `capacity-and-rate-limits`
- **controller_does:** The controller gives long-running dispatches explicit idle, quiet, tool, and heartbeat budgets so hangs have a bounded worst case.
- **failure_mode:** The controller lets goal-mode use a default multi-hour timeout and only discovers a stalled worker after the queue has been wedged for hours.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Bound dispatch hangs with idle and quiet timeouts"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_bounded_dispatch_timeouts
- **provenance:**
    - **sources:**
      - `docs-private/RESUME-NOTES-2026-05-20.md`
      - `docs-private/codex-stall-investigation-2026-05-15.md`
    - **r_numbers:** []
- **severity:** high
- **last_reviewed_commit:** (chunk-3c)

### Entry: heartbeats-file-pull-model

- **id:** `heartbeats-file-pull-model`
- **name:** Keep heartbeats file-based
- **category:** `worker-markers`
- **controller_does:** The controller treats heartbeats as runner-written files and wakes only on actionable transitions such as completion, wedge, or blocked state.
- **failure_mode:** The controller turns every heartbeat into a task notification, repeatedly reprocessing cached session context and flooding its own attention budget.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Heartbeats are files; wake only on transitions"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_heartbeats_file_pull_model
- **provenance:**
    - **sources:**
      - `docs-private/RESUME-NOTES-2026-05-20.md`
      - `docs-private/architecture/generalised-messaging.md`
    - **r_numbers:** []
- **severity:** high
- **last_reviewed_commit:** (chunk-3c)

### Entry: noninteractive-mcp-preflight

- **id:** `noninteractive-mcp-preflight`
- **name:** Preflight MCP approval stalls
- **category:** `verification-first-dispatch`
- **controller_does:** The controller preflights noninteractive worker launches for MCP approval-mode stalls before trusting them as a reliable dispatch path.
- **failure_mode:** A codex exec worker hangs with zero output because local MCP approval gates require interaction that the noninteractive process cannot provide.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Preflight noninteractive workers for MCP approval stalls"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_noninteractive_mcp_preflight
- **provenance:**
    - **sources:**
      - `docs-private/codex-stall-investigation-2026-05-15.md`
      - `docs-private/future-work.md`
    - **r_numbers:** []
- **severity:** high
- **last_reviewed_commit:** (chunk-3c)

### Entry: remote-worker-designated-controller

- **id:** `remote-worker-designated-controller`
- **name:** Keep one designated controller
- **category:** `state-layers`
- **controller_does:** In multi-server work, the controller keeps planning, steering, and observation on the designated controller surface while remote nodes execute worker turns.
- **failure_mode:** A remote worker node starts making steering decisions or maintaining its own status truth, splitting authority across machines.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Remote workers execute; controller remains designated surface"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_remote_worker_designated_controller
- **provenance:**
    - **sources:**
      - `docs-private/architecture/multi-server-workers.md`
      - `docs-private/architecture/META-ARCHITECTURE-phase0-convergence.md`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3c)

### Entry: single-status-plane

- **id:** `single-status-plane`
- **name:** Preserve one status plane
- **category:** `state-layers`
- **controller_does:** The controller keeps one canonical status/register plane across ACP, bash-tail, and gateway transports.
- **failure_mode:** A gateway transport becomes a second status store, so doctor, status, and controller views disagree about worker state.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Use one status plane across transports"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_single_status_plane
- **provenance:**
    - **sources:**
      - `docs-private/architecture/generalised-messaging.md`
      - `docs-private/architecture/worker-profiles-and-slots.md`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3c)

### Entry: user-need-controller-relay

- **id:** `user-need-controller-relay`
- **name:** Relay user needs centrally
- **category:** `chat-discipline`
- **controller_does:** Workers express USER-NEED or USER-CONFIRM through the controller relay rather than opening a separate chat path to the user.
- **failure_mode:** A worker asks the user directly, bypassing the controller's queue, status, and approval accounting.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Relay USER-NEED through controller, not worker chat"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_user_need_controller_relay
- **provenance:**
    - **sources:**
      - `docs-private/architecture/generalised-messaging.md`
      - `docs-private/RESUME-NOTES-2026-05-20.md`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3c)

### Entry: phase-gate-before-remote-dispatch

- **id:** `phase-gate-before-remote-dispatch`
- **name:** Gate remote dispatch
- **category:** `verification-first-dispatch`
- **controller_does:** The controller blocks remote or fleet dispatch until the relevant phase-gate acceptances are green.
- **failure_mode:** The controller starts remote workers before the local contracts, status mirror, and allowlist gates are proven, multiplying failures across machines.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "No remote dispatch before phase gate is green"
    - **max_section_lines:** 20
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_phase_gate_before_remote_dispatch
- **provenance:**
    - **sources:**
      - `docs-private/RESUME-NOTES-multi-workstation-acp-fleet.md`
      - `docs-private/RESUME-NOTES-phase0-convergence.md`
      - `docs-private/architecture/META-ARCHITECTURE-phase0-convergence.md`
    - **r_numbers:** []
- **severity:** high
- **last_reviewed_commit:** (chunk-3c)

### Entry: pidfile-isolation-per-controller

- **id:** `pidfile-isolation-per-controller`
- **name:** Isolate controller pidfiles
- **category:** `state-layers`
- **controller_does:** The controller isolates pidfile and run-state directories per active controller session when multiple sessions may dispatch workers.
- **failure_mode:** Two sessions share the same pidfile directory, causing healthy workers to be misclassified or cleaned up by the wrong controller.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Isolate pidfiles per controller session"
    - **max_section_lines:** 20
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_pidfile_isolation_per_controller
- **provenance:**
    - **sources:**
      - `docs-private/RESUME-NOTES-2026-05-21.md`
      - `docs-private/RESUME-NOTES-generalize-2026-05-20.md`
    - **r_numbers:** []
- **severity:** high
- **last_reviewed_commit:** (chunk-3c)

### Entry: learned-rate-pressure-ledger

- **id:** `learned-rate-pressure-ledger`
- **name:** Learn rate pressure
- **category:** `capacity-and-rate-limits`
- **controller_does:** The controller learns rate-pressure thresholds from ledgered dispatch outcomes instead of relying only on hardcoded provider caps.
- **failure_mode:** The controller keeps launching at a stale cap after a provider enters capacity triage, because prior rate-limit failures were not persisted into threshold decisions.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Learn rate pressure from ledger, not constants"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_learned_rate_pressure_ledger
- **provenance:**
    - **sources:**
      - `docs-private/BACKLOG.md`
      - `docs-private/RESUME-NOTES-2026-05-20.md`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3c)

### Entry: controller-provider-conservative

- **id:** `controller-provider-conservative`
- **name:** Protect controller provider
- **category:** `capacity-and-rate-limits`
- **controller_does:** The controller probes non-controller providers upward after clean windows but keeps the controller's own provider conservative.
- **failure_mode:** The controller experiments upward on the same provider that hosts the controller session and burns the user's interactive workday on rate pressure.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Probe workers upward; keep controller provider conservative"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_controller_provider_conservative
- **provenance:**
    - **sources:**
      - `docs-private/BACKLOG.md`
      - `docs-private/RESUME-NOTES-2026-05-20.md`
    - **r_numbers:** []
- **severity:** high
- **last_reviewed_commit:** (chunk-3c)

<!--
chunk-3a rationale:
- Added 31 foundation entries rather than ~25 because the reviewed plan requires every R1-R19 to map and every schema category to have at least one foundation anchor before 3b/3c.
- Kept duplicate-looking compaction and review entries where the backlog separates textual invariants from live behaviour scenarios; chunk-3d can dedupe after 3b/3c.
- Deferred schema-encoded behaviours to chunk-3b: readiness state, live gate, discovery probe, permission surface, tool-name map, status contract, packaging, adapter manifests, exact capacity script contracts, exact worker marker grammar, and ledger script contracts.
-->

### Entry: controller-readiness-probes-before-dispatch

- **id:** `controller-readiness-probes-before-dispatch`
- **name:** Controller readiness gates dispatch
- **category:** `dispatch-discipline`
- **controller_does:** The controller verifies the adapter-declared controller readiness requirements before using that host as the active controller.
- **failure_mode:** The controller dispatches through a host because the binary exists but skips version, auth, safe-args, or status-contract readiness checks.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Controller dispatch waits for declared readiness requirements"
    - **max_section_lines:** 30
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_schema_controller_readiness_anchor
- **provenance:**
    - **sources:**
      - `adapters/agent-adapter.schema.json`
      - `adapters/claude-code.json`
      - `adapters/codex.json`
      - `adapters/cursor.json`
      - `adapters/grok.json`
      - `adapters/opencode.json`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3b)

### Entry: controller-live-gate-supported-ready

- **id:** `controller-live-gate-supported-ready`
- **name:** Controller live gate is conjunctive
- **category:** `dispatch-discipline`
- **controller_does:** The controller evaluates the controller live gate as supported capability plus ready local controller state before use.
- **failure_mode:** The controller treats advertised support as sufficient and ignores a not-ready local controller state.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Controller live gate requires supported capability and ready local state"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_schema_controller_live_gate_anchor
- **provenance:**
    - **sources:**
      - `adapters/agent-adapter.schema.json`
      - `adapters/claude-code.json`
      - `adapters/codex.json`
      - `adapters/cursor.json`
      - `adapters/grok.json`
      - `adapters/opencode.json`
    - **r_numbers:** []
- **severity:** high
- **last_reviewed_commit:** (chunk-3b)

### Entry: worker-live-gate-supported-ready-verified

- **id:** `worker-live-gate-supported-ready-verified`
- **name:** Worker live gate verifies transport
- **category:** `dispatch-discipline`
- **controller_does:** The controller evaluates the worker live gate as supported capability, ready worker state, and verified requested transport.
- **failure_mode:** The controller starts a worker with supported capability but without checking local worker readiness or transport verification.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Worker live gate also requires requested transport verified"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_schema_worker_live_gate_anchor
- **provenance:**
    - **sources:**
      - `adapters/agent-adapter.schema.json`
      - `adapters/claude-code.json`
      - `adapters/codex.json`
      - `adapters/cursor.json`
      - `adapters/grok.json`
      - `adapters/opencode.json`
    - **r_numbers:** []
- **severity:** high
- **last_reviewed_commit:** (chunk-3b)

### Entry: discovery-probe-budget-bounded

- **id:** `discovery-probe-budget-bounded`
- **name:** Discovery probes stay bounded
- **category:** `verification-first-dispatch`
- **controller_does:** The controller keeps adapter discovery within the manifest budget for path, version, help, time, stdout, and stderr probes.
- **failure_mode:** The controller loops through unbounded path probes or streams full help output while trying to discover a host.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Discovery probes stay within manifest budget caps"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_schema_discovery_budget_anchor
- **provenance:**
    - **sources:**
      - `adapters/agent-adapter.schema.json`
      - `adapters/claude-code.json`
      - `adapters/codex.json`
      - `adapters/cursor.json`
      - `adapters/grok.json`
      - `adapters/opencode.json`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3b)

### Entry: discovery-probes-no-network-model

- **id:** `discovery-probes-no-network-model`
- **name:** Discovery probes are non-consuming
- **category:** `dispatch-discipline`
- **controller_does:** The controller runs setup discovery probes only when they are non-network and non-model-consuming by default.
- **failure_mode:** The controller burns model quota or performs network discovery while merely checking whether an adapter can run.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Discovery probes do not use network or model calls"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_schema_discovery_nonconsuming_anchor
- **provenance:**
    - **sources:**
      - `adapters/agent-adapter.schema.json`
      - `adapters/claude-code.json`
      - `adapters/codex.json`
      - `adapters/cursor.json`
      - `adapters/grok.json`
      - `adapters/opencode.json`
    - **r_numbers:** []
- **severity:** high
- **last_reviewed_commit:** (chunk-3b)

### Entry: permission-forbidden-shell-families

- **id:** `permission-forbidden-shell-families`
- **name:** Forbidden shell families stay blocked
- **category:** `do-not`
- **controller_does:** The controller rejects shell commands from adapter-forbidden families such as permission bypass, destructive reset, raw web fetch, or unchecked long output.
- **failure_mode:** The controller allows a raw web fetch or permission-bypass shell command because it is syntactically valid shell.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Forbidden shell families never enter controller dispatch"
    - **max_section_lines:** 30
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_schema_forbidden_shell_anchor
- **provenance:**
    - **sources:**
      - `adapters/agent-adapter.schema.json`
      - `adapters/claude-code.json`
      - `adapters/codex.json`
      - `adapters/cursor.json`
      - `adapters/grok.json`
      - `adapters/opencode.json`
    - **r_numbers:** []
- **severity:** high
- **last_reviewed_commit:** (chunk-3b)

### Entry: auto-approve-scans-strict-fail

- **id:** `auto-approve-scans-strict-fail`
- **name:** Auto-approve detection fails closed
- **category:** `do-not`
- **controller_does:** The controller treats adapter auto-approve detection probes as strict-fail safety checks, not warnings.
- **failure_mode:** The controller logs an auto-approve flag finding but proceeds with a worker launch anyway.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Auto-approve detection is strict-fail, not advisory"
    - **max_section_lines:** 30
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_schema_auto_approve_anchor
- **provenance:**
    - **sources:**
      - `adapters/agent-adapter.schema.json`
      - `adapters/claude-code.json`
      - `adapters/codex.json`
      - `adapters/cursor.json`
      - `adapters/grok.json`
      - `adapters/opencode.json`
    - **r_numbers:** []
- **severity:** high
- **last_reviewed_commit:** (chunk-3b)

### Entry: irreversible-ops-user-gated

- **id:** `irreversible-ops-user-gated`
- **name:** Irreversible operations need a gate
- **category:** `do-not`
- **controller_does:** The controller blocks or separately gates adapter-declared irreversible operations before execution.
- **failure_mode:** The controller runs `git reset --hard`, force-push, `rm -rf`, or credential deletion from generated automation without an explicit user gate.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Irreversible operations require explicit user gate"
    - **max_section_lines:** 30
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_schema_irreversible_ops_anchor
- **provenance:**
    - **sources:**
      - `adapters/agent-adapter.schema.json`
      - `adapters/claude-code.json`
      - `adapters/codex.json`
      - `adapters/cursor.json`
      - `adapters/grok.json`
      - `adapters/opencode.json`
    - **r_numbers:** []
- **severity:** high
- **last_reviewed_commit:** (chunk-3b)

### Entry: secrets-stay-out-of-probes

- **id:** `secrets-stay-out-of-probes`
- **name:** Secrets never leak through probes
- **category:** `do-not`
- **controller_does:** The controller keeps adapter-declared secret variables out of probe output, generated wrappers, checked-in configs, and logs.
- **failure_mode:** The controller copies an API token name or value into a committed wrapper or probe transcript.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Secrets stay out of probes, wrappers, and logs"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_schema_secrets_anchor
- **provenance:**
    - **sources:**
      - `adapters/agent-adapter.schema.json`
      - `adapters/claude-code.json`
      - `adapters/codex.json`
      - `adapters/cursor.json`
      - `adapters/grok.json`
      - `adapters/opencode.json`
    - **r_numbers:** []
- **severity:** high
- **last_reviewed_commit:** (chunk-3b)

### Entry: forbidden-exec-args-rejected-everywhere

- **id:** `forbidden-exec-args-rejected-everywhere`
- **name:** Forbidden exec args are global rejects
- **category:** `do-not`
- **controller_does:** The controller rejects adapter-forbidden exec arguments in invocation, probes, generated wrappers, and checked-in configs.
- **failure_mode:** The controller removes a forbidden flag from live invocation but leaves it in a generated wrapper or committed adapter config.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Forbidden exec args are rejected in every dispatch surface"
    - **max_section_lines:** 30
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_schema_forbidden_exec_args_anchor
- **provenance:**
    - **sources:**
      - `adapters/agent-adapter.schema.json`
      - `adapters/claude-code.json`
      - `adapters/codex.json`
      - `adapters/cursor.json`
      - `adapters/grok.json`
      - `adapters/opencode.json`
    - **r_numbers:** []
- **severity:** high
- **last_reviewed_commit:** (chunk-3b)

### Entry: risky-exec-args-need-justification

- **id:** `risky-exec-args-need-justification`
- **name:** Risky exec args require justification
- **category:** `do-not`
- **controller_does:** The controller requires explicit justification before using adapter-declared risky exec arguments such as bare, auto, force, or approval-bypass modes.
- **failure_mode:** The controller silently adds `--auto`, `--bare`, `--force`, or a headless approval-bypass mode to a worker command.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Risky exec args need explicit justification before use"
    - **max_section_lines:** 30
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_schema_risky_exec_args_anchor
- **provenance:**
    - **sources:**
      - `adapters/agent-adapter.schema.json`
      - `adapters/claude-code.json`
      - `adapters/codex.json`
      - `adapters/cursor.json`
      - `adapters/grok.json`
      - `adapters/opencode.json`
    - **r_numbers:** []
- **severity:** high
- **last_reviewed_commit:** (chunk-3b)

### Entry: abstract-tool-map-host-specific

- **id:** `abstract-tool-map-host-specific`
- **name:** Abstract tools map through manifests
- **category:** `worker-routing-defaults`
- **controller_does:** The controller resolves abstract tool roles through each adapter's host-specific tool name map before dispatching instructions.
- **failure_mode:** The controller tells a worker to call a generic tool name that does not exist on that host.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Abstract tool roles resolve through host tool-name maps"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_schema_tool_map_anchor
- **provenance:**
    - **sources:**
      - `adapters/agent-adapter.schema.json`
      - `adapters/claude-code.json`
      - `adapters/codex.json`
      - `adapters/cursor.json`
      - `adapters/grok.json`
      - `adapters/opencode.json`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3b)

### Entry: same-provider-policy-routing

- **id:** `same-provider-policy-routing`
- **name:** Provider trust policy shapes routing
- **category:** `worker-routing-defaults`
- **controller_does:** The controller applies the adapter provider policy when choosing review or worker routes, especially same-provider self-review restrictions.
- **failure_mode:** The controller accepts same-provider self-review despite a manifest policy that forbids it or requires cross-provider review.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Same-provider policy controls review routing trust"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_schema_provider_policy_anchor
- **provenance:**
    - **sources:**
      - `adapters/agent-adapter.schema.json`
      - `adapters/claude-code.json`
      - `adapters/codex.json`
      - `adapters/cursor.json`
      - `adapters/grok.json`
      - `adapters/opencode.json`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3b)

### Entry: repo-files-canonical-memory-backend

- **id:** `repo-files-canonical-memory-backend`
- **name:** Repo files win memory drift
- **category:** `state-layers`
- **controller_does:** The controller treats repository files as the canonical memory backend and warns while preferring repo state on drift.
- **failure_mode:** The controller overwrites repo files from cached memory or treats memory as more authoritative than checked-in state.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Repository files are the canonical memory backend"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_schema_memory_canonical_anchor
- **provenance:**
    - **sources:**
      - `adapters/agent-adapter.schema.json`
      - `adapters/claude-code.json`
      - `adapters/codex.json`
      - `adapters/cursor.json`
      - `adapters/grok.json`
      - `adapters/opencode.json`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3b)

### Entry: memory-writeback-lock-required

- **id:** `memory-writeback-lock-required`
- **name:** Memory writeback needs a lock
- **category:** `state-layers`
- **controller_does:** The controller writes back to a memory backend only when the adapter's migration lock requirement is satisfied.
- **failure_mode:** The controller writes memory-derived state during a migration without acquiring or verifying the required writeback lock.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Memory writeback requires migration lock ownership"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_schema_memory_lock_anchor
- **provenance:**
    - **sources:**
      - `adapters/agent-adapter.schema.json`
      - `adapters/claude-code.json`
      - `adapters/codex.json`
      - `adapters/cursor.json`
      - `adapters/grok.json`
      - `adapters/opencode.json`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3b)

### Entry: status-contract-heartbeats-required

- **id:** `status-contract-heartbeats-required`
- **name:** Live workers need heartbeats
- **category:** `worker-markers`
- **controller_does:** The controller requires heartbeat-capable status markers for live workers according to the adapter status contract.
- **failure_mode:** The controller launches a long-running worker without requiring heartbeat markers, leaving no reliable stale-worker signal.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Status contract requires heartbeat markers for live workers"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_schema_status_heartbeat_anchor
- **provenance:**
    - **sources:**
      - `adapters/agent-adapter.schema.json`
      - `adapters/claude-code.json`
      - `adapters/codex.json`
      - `adapters/cursor.json`
      - `adapters/grok.json`
      - `adapters/opencode.json`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3b)

### Entry: stale-after-threshold-enforced

- **id:** `stale-after-threshold-enforced`
- **name:** Stale thresholds are adapter-owned
- **category:** `worker-markers`
- **controller_does:** The controller uses each adapter's stale-after threshold when deciding whether a worker marker has gone stale.
- **failure_mode:** The controller hardcodes one stale timeout across all hosts and misclassifies a slower adapter as wedged.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Stale workers trip on manifest stale-after thresholds"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_schema_stale_after_anchor
- **provenance:**
    - **sources:**
      - `adapters/agent-adapter.schema.json`
      - `adapters/claude-code.json`
      - `adapters/codex.json`
      - `adapters/cursor.json`
      - `adapters/grok.json`
      - `adapters/opencode.json`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3b)

### Entry: terminal-states-closed-set

- **id:** `terminal-states-closed-set`
- **name:** Terminal states come from manifests
- **category:** `worker-markers`
- **controller_does:** The controller treats adapter terminal states as the closed set for status completion and failure classification.
- **failure_mode:** The controller invents an unregistered terminal state or ignores a manifest-declared terminal state.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Terminal states are closed manifest values"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_schema_terminal_states_anchor
- **provenance:**
    - **sources:**
      - `adapters/agent-adapter.schema.json`
      - `adapters/claude-code.json`
      - `adapters/codex.json`
      - `adapters/cursor.json`
      - `adapters/grok.json`
      - `adapters/opencode.json`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3b)

### Entry: marker-namespace-grammar

- **id:** `marker-namespace-grammar`
- **name:** Marker namespace is fixed grammar
- **category:** `worker-markers`
- **controller_does:** The controller emits worker markers using the adapter status namespace grammar for dispatch id, transport, and sequence.
- **failure_mode:** The controller writes ad hoc marker names that cannot be correlated by dispatch id or transport.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Worker markers use goalflight dispatch transport sequence grammar"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_schema_marker_namespace_anchor
- **provenance:**
    - **sources:**
      - `adapters/agent-adapter.schema.json`
      - `adapters/claude-code.json`
      - `adapters/codex.json`
      - `adapters/cursor.json`
      - `adapters/grok.json`
      - `adapters/opencode.json`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3b)

### Entry: default-agent-caps-enforced

- **id:** `default-agent-caps-enforced`
- **name:** Default agent caps constrain dispatch
- **category:** `capacity-and-rate-limits`
- **controller_does:** The controller applies the capacity script's default per-agent caps when no explicit narrower agent cap is supplied.
- **failure_mode:** The controller launches unbounded workers for a provider because no per-run cap override was passed.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Capacity checks apply default per-agent caps"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_capacity_default_caps_anchor
- **provenance:**
    - **sources:**
      - `scripts/goalflight_capacity.py`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3b)

### Entry: capacity-acquire-wait-reasons

- **id:** `capacity-acquire-wait-reasons`
- **name:** Capacity acquire fails closed
- **category:** `capacity-and-rate-limits`
- **controller_does:** The controller obeys capacity acquire wait decisions for cooldowns, machine worker caps, agent worker caps, and RSS budget pressure.
- **failure_mode:** The controller receives a wait decision from capacity acquire but spawns anyway because it only checked total worker count.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Capacity acquire waits on machine, agent, RSS, or cooldown pressure"
    - **max_section_lines:** 30
- **verifier:**
    - **kind:** runtime-assertion
    - **id:** goalflight_capacity::cmd_acquire_wait_reasons
- **provenance:**
    - **sources:**
      - `scripts/goalflight_capacity.py`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3b)

### Entry: terminal-lease-lifecycle-pruned

- **id:** `terminal-lease-lifecycle-pruned`
- **name:** Terminal leases leave active capacity
- **category:** `capacity-and-rate-limits`
- **controller_does:** The controller relies on the capacity lease lifecycle so released, expired, complete, failed, wedged, timeout, and legacy oversized-result leases do not count as active forever.
- **failure_mode:** The controller counts terminal lease records as active capacity and starves later dispatches.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Terminal leases leave active capacity after completion"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** runtime-assertion
    - **id:** goalflight_capacity::terminal_lease_states
- **provenance:**
    - **sources:**
      - `scripts/goalflight_capacity.py`
    - **r_numbers:** []
- **severity:** med
- **last_reviewed_commit:** (chunk-3b)

### Entry: ledger-pid-plus-process-identity

- **id:** `ledger-pid-plus-process-identity`
- **name:** Ledger liveness uses process identity
- **category:** `state-layers`
- **controller_does:** The controller records and checks PID plus process identity fields rather than trusting PID alone for ledger liveness.
- **failure_mode:** The controller treats a reused PID as the original worker and reports a dead or unrelated process as live.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Ledger liveness matches PID plus process identity"
    - **max_section_lines:** 25
- **verifier:**
    - **kind:** runtime-assertion
    - **id:** goalflight_ledger::process_identity_match
- **provenance:**
    - **sources:**
      - `scripts/goalflight_ledger.py`
    - **r_numbers:** []
- **severity:** high
- **last_reviewed_commit:** (chunk-3b)

### Entry: acp-permit-file-ipc-contract

- **id:** `acp-permit-file-ipc-contract`
- **name:** Inline permits use file IPC
- **category:** `do-not`
- **controller_does:** The controller handles inline permission authorization through the request, decision, and ack file IPC contract.
- **failure_mode:** The controller bypasses the permit exchange with an implicit allow, hidden auto-approval, or direct denial-and-redispatch loop.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Inline permits use request, decision, and ack files"
    - **max_section_lines:** 30
- **verifier:**
    - **kind:** runtime-assertion
    - **id:** goalflight_acp_permits::request_decision_ack
- **provenance:**
    - **sources:**
      - `scripts/goalflight_acp_permits.py`
    - **r_numbers:** [R26]
- **severity:** high
- **last_reviewed_commit:** (chunk-3b)

### Entry: install-actions-user-gate-backups

- **id:** `install-actions-user-gate-backups`
- **name:** Install actions are gated and backed up
- **category:** `do-not`
- **controller_does:** The controller honors manifest install actions only when user gates and backup paths are present for mutable user files.
- **failure_mode:** The controller overwrites a user-level agent file during setup without a user gate or backup path.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Install actions need user gates and backup paths"
    - **max_section_lines:** 30
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_schema_install_actions_anchor
- **provenance:**
    - **sources:**
      - `adapters/agent-adapter.schema.json`
      - `adapters/codex.json`
      - `adapters/cursor.json`
      - `adapters/grok.json`
      - `adapters/opencode.json`
    - **r_numbers:** []
- **severity:** high
- **last_reviewed_commit:** (chunk-3b)

---

### Entry: worker-escalate-not-bypass

- **id:** `worker-escalate-not-bypass`
- **name:** Workers escalate sandbox/permission blocks, not bypass
- **category:** `worker-markers`
- **controller_does:** When a dispatched worker hits a sandbox, permission, hook, or tool-availability block during its task, the worker returns to the controller with a `BLOCKED:` marker plus the block detail and a recommended controller action, rather than executing a workaround (alternate APIs, git plumbing, inline content dumps for blocked file-writes). The controller decides whether the workaround is appropriate.
- **failure_mode:** Worker hits a block, rationalizes a workaround, completes via the alternate path. Concrete anti-patterns from the 2026-05-28 session: a worker whose `git commit` hit a `/dev/null` sandbox quirk published a commit via the GitHub Git Data API and self-pushed `main`; a worker whose findings-file write was rejected returned the ~5KB report inline, defeating the file-backed-return contract. In both cases the controller never got to decide whether the workaround was appropriate, and unauthorized state changes (push, inline payload dumps) landed.
- **skill_md_compressed_form:**
    - **kind:** literal
    - **pattern:** "Workers escalate sandbox / permission / tool blocks via `BLOCKED:` and return to the controller. They do NOT execute workarounds"
    - **max_section_lines:** 5
- **verifier:**
    - **kind:** textual-invariant
    - **id:** test_skill_structure::test_skill_md_matches_golden_master
    - **fallback:** behaviour-scenario `worker-blocked-no-bypass` (Wave-C; not yet implemented)
- **provenance:**
    - **sources:**
      - `protocols/dispatched-worker-recovery.md`
      - `docs-private/research/2026-05-28-skill-worker-escalate-review/findings.md`
    - **r_numbers:** []
- **severity:** high
- **max_skill_lines:** 8
- **last_reviewed_commit:** (this commit)
- **notes:** Observed 2x in the 2026-05-28 session — same root cause across different worker types (codex-acp executor and Claude Explore subagent). Detailed examples and the protocol-side handling live in `protocols/dispatched-worker-recovery.md` §"Worker bypass anti-pattern". The compressed-form text uses `BLOCKED:` (the canonical marker per `protocols/worker-markers.md`), not the proposed `READY-BLOCKED:` (rejected by review for marker-proliferation).

## Adding a new entry

1. Pick a unique kebab-case `id` not already used.
2. Pick a `category` from the enum (or propose a new one — requires frontmatter `schema_version` bump and chunk-5 invariants test update).
3. Fill all required fields. Use the `skill-load-order-mandatory` exemplar above as a template.
4. Run `python3 tests/python/test_skill_structure.py` (or whichever test gates this file once chunk-5 lands) to confirm the schema + invariants still hold.
5. Re-distill `SKILL.md` so it carries the entry's `skill_md_compressed_form.pattern` in a stable anchor. The invariants test will fail until `SKILL.md` matches.
6. If the entry has a testable failure mode (`verifier.kind: behaviour-scenario`), add the scenario fixture under `tests/fixtures/controller_scenarios/<verifier.id>/prompt.md` plus an assertion function in `scripts/hosts/controller/behavior_scenario.py`.

---

## Roadmap for filling out the Golden Master

This file contains the chunk-2.5 exemplar plus chunk-3a foundation entries. The
full set of ~70-100 entries will be filled in by chunks 3b (schema-encoded
behaviours from
`adapters/agent-adapter.schema.json` and capacity/ledger scripts, ~25
entries), and 3c (long-tail past-steering items, ~30 entries). chunk-3d
dedupes overlap between 3a/3b/3c (especially capacity/dispatch/markers/gating
where past-steering and schema-encoded sources overlap).

Until those chunks land, this file's exemplar plus foundation entries serve as:
1. The schema template (chunk-5 invariants test can scaffold against it).
2. The load-bearing Golden Master foundation for chunk-4 distillation.
