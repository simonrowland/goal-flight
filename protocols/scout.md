# Scout Protocol

Pre-dispatch verification for prompts whose factual premises must survive contact
with the current tree. `protocols/premises.md` is the soft ancestor: it captures
validated, open, and corrected premises during planning. This protocol hardens
that practice into an evidence table, a read-only refutation pass, explicit
verdicts, and a dispatch gate.

## Purpose

A prompt review asks whether a brief is well-formed. A scout asks whether the
brief is true.

Critical work needs both. A structurally complete prompt can still name a
deleted symbol, assign behavior to the wrong owner, request work already
present, or promise a test state the current tree cannot produce. Never treat a
review verdict such as `CONVERGED` as verification of tree facts.

The normal flow is:

```text
draft prompt -> premise scout -> fold into v2 -> adversarial prompt review
-> dispatch worker -> normal chunk review
```

Scouting moves tree discovery ahead of the expensive write-mode fire. Its
deliverable is a verified prompt, not an implementation and not a substitute
for chunk review.

Scouting optimizes, in order: output quality first, build wall-clock second,
tokens a distant third. Token savings are a common dividend — verified state
replaces defensive check-first hedging and shrinks the fired prompt — but
they are never the gate. The economics that matter: one avoided
iterate-review-refire cycle on a long write-mode precision loop dwarfs any
scouting spend, so the more complicated the project, the more scouting pays.
Skip scouting only where it has nothing to verify (see the skip rule below).

Scouting compresses the **front** of the funnel: do not dispatch work that is
done, blocked, mis-anchored, or unanswerable. It does nothing for rounds caused
by shallow first fixes; review depth is a separate lever.

Scouting is prompt de-risking: the staleness menu is the risk register, the
premises table is the evidence, and the verdict is an explicit residual-risk
acceptance before the expensive write-mode fire.

## Modes — VERIFY and AUTHOR

The two co-equal scout modes are:

- **VERIFY** — inspect an existing prompt against the current tree, using the
  staleness menu to scope suspect surfaces, then return the requested
  corrections and evidence.
- **AUTHOR** — receive a controller-written stub containing intent plus an
  acceptance sketch, investigate the live tree, and write the complete
  blind-fire-grade worker brief. This spends scout context on prompt authoring
  instead of controller context.

Both modes obey the evidence, transport, verdict, and fold-in contracts below.
AUTHOR is not a shortcut around verification: its finished brief must carry
the same current-tree evidence as a verified prompt.

## The two scout lenses

### 1. Premise and anchor scout

Attack the factual claims that make the draft dispatchable:

- Does each named file, symbol, behavior, fixture, and test state exist as
  claimed on the current tree?
- Does the named owner still own the behavior, or has ownership moved?
- Is the requested work absent, partly present, or already satisfied?
- Are prerequisites landed, or are they only uncommitted work?
- Can the proposed red/green path actually be observed?
- Do non-obvious derivations survive an independent unit and sanity check?

Return the premises table, resolved symbol anchors, empirical evidence where
needed, numbered prompt refinements, and a verdict.

### 2. Adversarial prompt review

Use a read-only reviewer with the persona: "How could a technically compliant
worker satisfy these words while defeating their intent?" Report each loophole
as the permitting sentence followed by the smallest defensive correction.

Classify findings with severity `P0` through `P2` and this taxonomy:

| Class | Question |
|---|---|
| Ambiguity | Can materially different implementations all claim compliance? |
| False-COMPLETE route | Can the worker emit `COMPLETE` without proving the intended outcome? |
| Missing context | Is a necessary invariant, dependency, owner, or decision absent? |
| Silent break | Can the requested change damage an adjacent contract without a required signal? |
| Honest-outcome gap | Does the prompt pressure the worker to manufacture green instead of returning the next truthful blocker or finding? |

The lenses are complementary. Premise scouting checks claims against the tree;
adversarial review checks whether the wording preserves intent.

## Deliverable menu

The controller names which deliverables each scout must produce:

1. **PREMISES table** — falsifiable prompt claims with current-tree evidence.
2. **Resolved anchors** — semantic edit and proof sites with current owners.
3. **Derivations and research** — non-obvious reasoning, units, sanity cases,
   or external constraints already worked through.
4. **FRAMING analysis** — numbered corrections plus the required verdict.
5. **DECISION EXTRACTION** — implicit choices the tree cannot settle, surfaced
   as one-line controller TL;DRs: `This prompt implicitly decides <X>;
   recommend <A> because <cite>.`
6. **BLOCKER/USER-NEED FORECAST** — a dry-walk of the worker's likely command,
   permission, dependency, and interactive-gate path. Give each predicted
   block or question one disposition: `PRE-ANSWER` in the prompt,
   `PRE-PROVISION` before firing, or `QUEUE-TO-OPERATOR` through one batched
   pre-fire ask-questions pass.

Keep the menu light. VERIFY scouts produce only the controller-scoped subset;
a narrow staleness re-check usually needs one or two. AUTHOR scouts produce
all six. Required report metadata, one verdict, and terminal markers remain
present regardless of the selected content deliverables.

Decision routing stays explicit: tree-settleable choices are answered by the
scout; judgment choices become one-line controller TL;DRs; human-only choices
go to ask-questions. Do not let the worker make an implicit architecture or
scope decision by accident.

### Context brokerage and documentation upkeep

Two further deliverables, both cheap because the scout is already reading the
tree for this task:

**Task-scoped context slice.** Resolve the documentation, specifications, and
corpus material that bear on *this* work item and hand the worker pointed
excerpts with locations — not a blanket instruction to read a package. A
resolved slice is shorter than the package it replaces and removes the
missing-or-contradicting-context failure at its source.

**Documentation-upkeep sites.** A project may declare its code-coupled
documents — architecture notes, specifications, design records, benchmark
standards — in a maintained registry (for example `docs-to-maintain.md`) that
names each document, what it is coupled to, and who consumes it. The scout
resolves which of those documents this change invalidates, names the specific
section, and writes the upkeep site into the brief so the worker's changeset
carries the documentation patch alongside the code. The worker's prose is a
draft: whoever folds the changeset may rewrite it freely. The point is that
the prompt names the site at all, so coupled documentation cannot rot silently
while the code moves. Where no registry exists, the scout may propose the
first list from what the lane actually cites.

## Ordering — truth before well-formed

Run the lenses sequentially by default:

1. run the premise-and-anchor scout against the draft prompt;
2. fold its verified facts and refinements into a v2 prompt; then
3. run the adversarial well-formedness pass against that folded v2 prompt.

Truth findings frequently invalidate whole prompts. Running both passes in
parallel can spend reviewer tokens polishing text that the premise pass is
about to retire, which is the waste scouting exists to prevent.

Parallel execution is the exception for wall-clock-critical lanes whose
premises are already evidence-backed. If the freshness pass changes a material
fact, repeat the adversarial pass against the folded prompt before dispatch.

## Classifier-safe framing — hard rule

Some provider content classifiers reject adversarial or refutation framing in
write-capable dispatches. Therefore refutation, adversarial, and
null-hypothesis stances belong **only** in read-only scout or review
dispatches.

Do not copy their offensive wording into a write-mode worker prompt.

Before a follow-on write dispatch:

- describe a gap functionally, such as "the receipt does not bind the declared
  subject";
- describe tests as fail-closed regressions that reject unsafe or invalid
  input;
- state the safe success semantics the implementation must preserve; and
- remove attack, forge, poison, or similar imperatives from worker
  instructions.

Keep the evidence and acceptance condition; translate the stance. If a scout is
promoted through the patching exception below, replace its read-only
refutation prompt with a separately authorized, defensively framed write
prompt.

## Canonical read-only scout constraint

Use this block in premise and adversarial scout prompts:

```text
READ-ONLY SCOUT. Edit no project files. Commit nothing. Never stash, checkout,
clean, or discard concurrent work. You may run focused tests or probes that do
not mutate tracked sources, inspect history, and create disposable scratch
outputs. Verify the current tree, including uncommitted work. Return the
complete report inline for controller capture: STATUS, findings, numbered
REFINEMENTS for each named prompt file, one VERDICT from the required enum, and
a RESULT one-liner.
```

With a hard read-only transport, the controller persists the inline report in
the named durable location. If the scout must create the report directly,
launch it with an explicitly writable, report-scoped sandbox or worktree and
authorize only that report path. Neither transport authorizes source edits;
those require the separate patching gate below. Scratch output may be
temporary; the report and verdict may not be.

## Premises-table contract

Every scout report records:

1. the observed `HEAD`;
2. whether the worktree had relevant uncommitted changes;
3. the draft prompt version or digest inspected;
4. the exact surfaces included; and
5. the exact surfaces not swept.

Write suspect premises as falsifiable sub-questions before investigating them.
Every question must permit `NONE + recommendation` when the expected owner,
state, or evidence does not exist. Forced choice manufactures confirmation;
an explicit escape hatch makes absence reportable.

Use this table:

| ID | Falsifiable premise / `NONE` alternative | Current-tree evidence | Classification | Actual fact or owner | Prompt refinement |
|---|---|---|---|---|---|
| P1 | `<claim to disprove; or NONE + recommendation>` | `<path:symbol plus probe/test/history evidence>` | `FRESH` | `<fact>` | `NONE` |

Classification is mandatory for each claimed anchor:

| Classification | Meaning |
|---|---|
| `FRESH` | The symbol exists and still has the claimed behavior and ownership. |
| `MOVED` | The behavior remains, but its path or symbol owner changed; name the current owner. |
| `GONE` | No current owner or applicable behavior exists; state the prompt consequence. |
| `SEMANTIC-DRIFT` | The named path or symbol still exists, but its behavior, responsibility, or contract no longer matches the prompt; name the real owner or current semantics. |

Do not force an unresolved question into one of the four classifications.
Write `NONE` with the missing evidence and recommendation, then choose a
blocking verdict.

### Evidence rules

- Verify the **current tree**, including relevant uncommitted work. A landed
  fact and dirty worktree state are different sequencing facts.
- Record the observed `HEAD`. The value makes later staleness auditable; it
  does not imply a clean tree.
- Anchor by `path:symbol`, test ID, schema key, or other semantic identifier.
  Exact line numbers are supporting navigation only.
- Empirical checks are allowed. Run focused tests, simulations, or probes when
  reading alone cannot settle the premise, while editing nothing except the
  report.
- For non-obvious math or algorithms, attach the derivation from premise
  through algebra, units, and a sanity case.
- A zero-finding report is valid only with an explicit coverage statement and
  a `Surfaces not swept` line.

## Verdicts

Premise and prompt scouts use exactly one:

| Verdict | Meaning |
|---|---|
| `DISPATCHABLE-AS-IS` | All material premises and anchors are evidence-backed; no corrective refinement is required. Add only the mandatory `SCOUTED STATE` metadata. |
| `NEEDS-REFINEMENT` | The goal remains valid, but named corrections must be folded into the prompt before dispatch. |
| `PROMPT-INVALID` | The prompt's core goal, ordering, ownership, or proof path is false enough that editing its wording is insufficient; replan or retire it. |

Backlog-verification prompt-author scouts may also use:

| Verdict | Meaning |
|---|---|
| `ALREADY-FIXED` | Current evidence satisfies the backlog row; do not dispatch an implementation worker. |
| `NEEDS-CONTROLLER` | Tree facts are known, but a controller-held scope, ownership, or product decision is required before authoring the worker prompt. |

Do not invent synonyms. Stable enums make queue gates and report mining
reliable.

Compatibility boundary: `NEEDS-REANCHOR` is a binding blocking state from
existing reports and queue entries. Map it to `NEEDS-REFINEMENT` before
fold-in; new scout reports emit `NEEDS-REFINEMENT`, not `NEEDS-REANCHOR`.

An aggressive scout can manufacture a `FALSE-OBSOLETE` verdict. Negative rulings
such as absent, already fixed, gone, or superseded require explicit evidence,
and the controller spot-checks that evidence before closing or re-scoping work.

## THE FOLD-IN GATE

`NEEDS-REFINEMENT` (including mapped `NEEDS-REANCHOR`), `PROMPT-INVALID`, and
`NEEDS-CONTROLLER` block the affected chunk. A scout report beside an
unchanged worker prompt is not a completed scout gate.

Before firing the chunk, the controller must:

1. fold each numbered refinement into the named prompt, or record an
   evidence-backed disposition that changes the verdict;
2. add the `SCOUTED STATE` block below;
3. resolve ownership, ordering, and already-satisfied scope in the queue or
   prompt; and
4. ensure the resulting write-mode language satisfies the classifier-safe
   framing rule.

`PROMPT-INVALID` requires re-planning and a new scoutable prompt.
`ALREADY-FIXED` closes or re-scopes the backlog row instead of firing it.

Scouting gates **unverified premises**, not the whole wave. Sibling chunks whose
premises are already evidence-backed may fire in parallel with outstanding
scouts.

## `SCOUTED STATE` fold-in

Put verified facts in the follow-on prompt. Do not merely link the report.

```text
SCOUTED STATE (verified <YYYY-MM-DD> at <observed-HEAD>; relevant uncommitted
work: <none | summary>)
Trust these facts; re-locate approximate line hints before editing.

- ~<line-hint> <path>:<symbol> — <FRESH | MOVED | GONE | SEMANTIC-DRIFT>:
  <current fact and ownership>.
- Already satisfied: <fact the worker must report as an already-satisfied
  finding, not silently rebuild or omit>.
- Required sequencing/context: <landed prerequisite or dependency fact>.
- Empirical evidence: <focused command/test and observed result>.
- Surfaces not swept: <explicit list or NONE>.
```

The tilde marks a line number as approximate. The fact and semantic anchor are
authoritative; the worker must re-locate the line. Fold facts, not stale line
coordinates.

When scouting removes apparent work, the follow-on prompt must say how to
report it: already-satisfied work is a finding, not silence and not permission
to rebuild parallel machinery.

## When to scout

Scout before firing prompts in:

- critical-path or reliability-critical chains;
- security- or credential-adjacent work;
- lanes with spec-resident invariants or costly shared seams;
- work where a failed dispatch consumes scarce capacity or blocks dependents;
  and
- prompts built over old, recently churned, or explicitly doubtful anchors.

Aim at doubt. Name why each surface is suspect: recent edits, age, ownership
change, a prior contradiction, or an unproved test state. Broadly checking
everything produces low-value confirmation rows and can still miss the actual
uncertainty.

The skip rule. Skip scouting only for trivial mechanical work whose premises
are already obvious from fresh evidence — where a scout would have nothing to
verify. Do not skip on token arithmetic: quality is the objective, and the
expensive failure is the wrong build, not the scout.

## Staleness menu

Staleness is a menu, not a mandate. The controller arms two to four of these
lines per scout:

1. **PACKAGE+PINS** — confirm every referenced task or work-item ID exists in
   the store as resolved from the worker cwd, and every cited path,
   specification section, fixture, and pinned hash exists on the current tree;
   recompute each pin. This is the cheapest check, and if the package is
   missing every other verdict is unfounded. The ID probe is hygiene;
   systematic enforcement belongs to store-integrity tooling.
2. **WORK-STATE+PREMISE** — classify the work as absent, `PARTLY present` /
   `HALF-done` with the residual re-scoped, already satisfied, or fixed by a
   `DIFFERENT` mechanism / `SUPERSEDED` by another fix, with
   duplicate-mechanism risk; first verify that the described defect is real.
   "a cited file or symbol that does not exist at HEAD is NOT automatically
   already-fixed; check other branches and work-in-progress before ruling".
3. **ANCHORS+OWNERSHIP** — relocate by `path:symbol`; classify `FRESH`, `MOVED`,
   `GONE`, or `SEMANTIC-DRIFT`; name the current owner and, for waves, map
   parallel-lane ownership into review briefs. Drift rarely blocks because
   workers silently re-anchor, so force this check rather than inferring it
   from silence.
4. **CAN IT RUN AND LAND** — under the worker sandbox, confirm the gate command
   executes, the designed-RED condition fires, clean-base status is known,
   prerequisites are `LANDED` rather than work-in-progress, and the true edit
   surface stays inside the scope fence. Surface any needed ruling; do not
   dispatch.

Preliminary fire-time measurements put dependency/ordering and
acceptance-surface leaks first, followed by work-state and environment/context
failures. Tree-anchor checks remain cheap and first at scout time because
scouting catches them before fire: this ordering measures what leaks past
scouting, not intrinsic rot rates. Tighten it as evidence accumulates.
AUTHOR-mode economics and the one-shot outcome effect remain
measurement-pending.

Repeated re-fires of an already-diagnosed block belong to blocked-reason lookup
at the dispatch gate, not scouting; see `protocols/dispatch-routing.md`.

### Authored stamp and advisory signals

New prompts carry this header:

```text
AUTHORED STATE
- Authored at: <ISO-8601 timestamp>
- Observed HEAD: <commit>
- Referenced surfaces: <paths, symbols, decisions, or store rows>
```

A deterministic preflight may use prompt age, commits touching referenced
paths, decision-document deltas, and linked store-task audit entries to suggest
which staleness surfaces to arm. File modification time is a fallback only;
copies and checkouts can falsify it. These signals are **advisory focus
indications, never dispatch gates**. Signal tooling ships separately from this
protocol.

## Scout ahead — pipeline and focus anchor

During worker or review wait windows, dispatch scouts for the next queued
prompts. Scouting the near frontier overlaps verification with build time, so
the opening gate need not add critical-path latency.

This forward pass also keeps the controller anchored: selecting the next scout
requires re-reading the upcoming program plan and queue frontier instead of
drifting into an unrelated side mission. Scout only the near frontier, where
findings are likely to remain fresh, and apply the re-scout rule below whenever
the tree moves under an unfired prompt.

### Re-scout on tree motion

A scouted but unfired prompt must be re-scouted when the tree moves under it.
Tree motion includes changes after the observed `HEAD` to a named anchor,
dependency, invariant, guard test, or relevant uncommitted prerequisite.
Unrelated commits do not invalidate the report; record why they are unrelated.

## Scaling

For one critical prompt, run the premise-and-anchor lens, fold its findings,
then run the adversarial lens against the folded prompt before dispatch.

For a campaign wave with shared invariants, run:

1. a premise-and-anchor pass over each prompt;
2. an adversarial prompt-review pass; and
3. a cross-prompt contradiction check for conflicting ownership, dependency
   order, edit surfaces, invariants, and completion claims.

Save the reports side by side in one durable review or lane-research
directory. Cross-check adjacent reports before folding them into the wave.

### Batching — amortize scout sessions along shared surfaces

Each scout session pays a fixed cost: boot, orientation, and tree
familiarization on the lane's surfaces. Batch to amortize it, along exactly
the axes where knowledge is shared:

- **The batch unit is a shared surface, never a count.** One scout covers all
  the prompts in a lane, wave, or subsystem — read once, verify many — and
  reports per-prompt verdict rows plus the cross-prompt contradiction section
  that only a batch can produce (contradictions live *between* prompts and are
  invisible to any single-prompt scout). Do not batch across unrelated
  subsystems: breadth dilutes depth and produces echo rows.
- **Prompt chains get two tiers with different rot rates.** At decomposition
  time, one chain-contract scout covers the whole set: dependency order,
  interface handoffs between chunks, ownership boundaries, and
  decision conformance — findings that rot slowly. During execution, rolling
  just-in-time freshness checks (light VERIFY passes on the next one or two
  chunks while the current chunk builds) cover the fast-rotting surfaces:
  anchors, pins, and work-state. A long chain costs one heavy session plus a
  few light ones instead of one heavy session per chunk.
- **AUTHOR batches only tightly-coupled pairs; VERIFY batches widely.** A
  scout authoring two briefs in one session gives the second less depth, so
  reserve AUTHOR batching for prompts that share an interface seam — where
  co-authoring also prevents the inter-brief interface drift that two
  parallel authors would create.

## Routing and capacity

Give a stronger-reasoning host-subagent tier a modest, deliberate scout slice
by default: nonzero in sustained runs, but never drain the limited pool. Use
the slice for read-heavy critical-lane scouting, prompt review, and an
occasional concern-diverse lens. It relieves controller context; it does not
replace abundant CLI-worker capacity.

Route the scout from a different provider reservoir than the follow-on worker.
Premise verification has no value if it consumes the same constrained seat and
both jobs fail together. Keep campaign fan-out within current capacity limits.

Every judgment-bearing host-subagent scout begins with
`protocols/subagent-preamble.md`, followed by the lane's north star, the draft
prompt, and any required context package.

Provider pools are not fungible. When a research-grade pool is abundant,
deepen the menu, widen the batch, and add empirical checks freely — without
going overboard into echo-row breadth — because de-risking spent from an
abundant pool protects the scarce precision-coding pool from bounce rounds.
Ration scout depth only on pools that are themselves scarce.

### Tier-calibrated brief detail

Calibrate procedural detail to the executing model tier. Frontier-tier workers
get goals, constraints, evidence, and output contracts; the method remains
theirs. Lower tiers get explicit procedures and worked steps.

Safety and process invariants are never tier-gated. Read-only defaults, no
commits, credential hygiene, and escalate-do-not-workaround are contracts, not
capability scaffolds.

## Read-only default and patching exception

Scouts are read-only by default. A hard read-only dispatch returns its complete
report inline for controller capture. Direct report creation requires an
explicitly writable, report-scoped dispatch; that permission covers only the
named report.

A scout may patch only when all of these gates are explicit:

1. the verified fix is small and obvious;
2. the controller authorizes exact write scope;
3. the task is re-dispatched or re-framed as write-mode under the
   classifier-safe rule;
4. the scout stages but does not commit; and
5. the patch enters the same focused-test and independent-review gate as any
   implementation chunk.

Scouting never bypasses review. If any gate is absent, report the patch plan
and leave the tree unchanged.

## Specialized forms

### AUTHOR backlog form

Use this form for stale or mined backlogs. A store row is a lead, not ground
truth. Starting from the controller's intent and acceptance stub, verify each
row live, choose a backlog-verification verdict, then author the full follow-on
worker prompt to the mandated, tier-calibrated template. Return a manifest with
`id`, `verdict`, `evidence`, and `prompt path`.

### Recon or dossier scout

Use this form before a design fork: build an option dossier with feasibility,
constraints, tradeoffs, and missing evidence for each option. It is
decision-support, not premise refutation, so do not impose the adversarial or
null-hypothesis stance. Mark which facts are observed and which choices still
belong to the controller or operator.

## Ask-questions boundary

Use a scout for premises the tree, tests, history, or local evidence can
settle. Use ask-questions only for ambiguity that requires human intent,
authority, credentials, or a product decision.

Do not route tree-settleable questions to the operator. A scout that reaches a
true human boundary reports the evidence and the smallest remaining question;
it does not invent a workaround.

## Durable reports

Store scout briefs and reports in the lane's durable private research
directory, for example:

```text
docs-private/research/<lane>/BRIEF-<chunk>-SCOUT.md
docs-private/research/<lane>/SCOUT-REPORT-<chunk>.md
```

Never leave the only verdict or refinement list in a temporary directory,
terminal transcript, task pane, or chat message. Queue state may reference the
report, but the fold-in gate still requires the verified facts in the worker
prompt.
