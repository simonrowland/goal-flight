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

<!--
chunk-3a rationale:
- Added 31 foundation entries rather than ~25 because the reviewed plan requires every R1-R19 to map and every schema category to have at least one foundation anchor before 3b/3c.
- Kept duplicate-looking compaction and review entries where the backlog separates textual invariants from live behaviour scenarios; chunk-3d can dedupe after 3b/3c.
- Deferred schema-encoded behaviours to chunk-3b: readiness state, live gate, discovery probe, permission surface, tool-name map, status contract, packaging, adapter manifests, exact capacity script contracts, exact worker marker grammar, and ledger script contracts.
-->

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
