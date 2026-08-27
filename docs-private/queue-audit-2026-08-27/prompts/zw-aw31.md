# Pinned prompt — `zw-aw31`

- source: `prompt-file`
- prompt-file: `/tmp/goal-flight-501/dispatch/zw-aw31.assembled.prompt` (EXISTS on disk)
- note: prompt_file taken from ledger.prompt_path
- pinned_at: 2026-08-27T14:54:06.319466+00:00

```
You have a steer mailbox at `$GOALFLIGHT_STEER_FILE`. Read it AT THE TOP OF EACH ITERATION and IMMEDIATELY BEFORE ANY git commit/push. Incorporate new messages into your plan; ack each with `!STEER-ACK: <seq>` on its own line; a steer may redirect or HALT you — honor it.

Your FULL original brief is at `$GOALFLIGHT_PROMPT_FILE`. Re-read it after any internal compaction/summarization, at the start of each long-run goal-loop iteration, and before final commit/exit; the disk file is authoritative over summarized memory.

Terminal evidence identity contract:
- Every terminal marker payload starts with the exact dispatch id `zw-aw31`.
- Successful final shape: `!COMPLETE: zw-aw31 — <summary>`.
- Use the same id prefix for READY, RESULT, FAILED, USER-NEED, USER-CONFIRM, or BLOCKED. A generic or foreign marker is ignored.

# Mandate — North Star for Battery-Tool-v2

> **Prepend this to every worker dispatch.** It is the standing charge — the situational frame every
> executor reads before touching the work. Short by design so it travels on each dispatch without bloat.
> Canonical north-star; faithful to `specs/decision-lock-2026-06-21.md` (D-Q1…D-Q86+, authoritative) and `CORE-CONCEPT.md`.

## The mission
Build a **P.Eng-stampable, filing-grade** engineering tool: given a leased NYC rooftop, find diverse, **buildable**
ways to mount Tesla Megapacks on steel dunnage over an existing (often unreinforced-masonry) building, optimized for
capacity × fabricated cost, with a structural-engineer-runnable FEA handoff. The output goes to a Professional Engineer
who **opens it, runs it, and stamps it.** Build for that bar.

## The figure of merit (what the tool is FOR)

**ROOFTOP COVERAGE = megapack square footage ÷ buildable rooftop square footage.** That fraction is the
CENTRE figure of merit and the thing every design is judged by. Revenue is electricity sales **per
megapack**, so an under-tiled roof is directly lost money — under-placement is a product defect, not a
tuning shortfall. The roof is the thing being tiled; **the megapack count is an OUTPUT of the resolve,
never an input** (q-059). Buildable = the roof area inside the 10-ft FDNY walkaround. The target is DERIVED, not estimated
(q-067): the q-061 oracle's achievable count on the real polygon under the cited clearances —
equivalently, ratchet R2 → 1.0. (The historical ~2/3 was an estimate; the derivation supersedes it.)

**What stops the tiling is the EXISTING BUILDING, not the steel we add.** The density limit is the
calculated bearing capacity of the masonry (and the foundation beneath it) — the fixed, cited capacity
of the structure that is already there. Steel dunnage is a DESIGN VARIABLE: if members are overstressed,
the answer is heavier sections, a denser frame, more supports — never fewer megapacks. Tile until the
brick or the footing governs, and say which one did. A run that stops on steel utilization has stopped
at a constraint of its own making, and that is a defect. (Measured 2026-07-26 on 148-28: masonry
0.076-0.21 against its 0.85 allowable while steel ran 1.18-2.96 — the brick had ~4x headroom and the
tool stopped anyway.)

Report coverage on every candidate and every gallery tab as a headline number — it is more informative
than the megapack count alone, because "2 megapacks" says nothing about whether that is excellent or
catastrophic on a given roof.

**The structure is a SEARCH, and the search is WIDE (q-063).** Generate many diverse candidate
structures and evaluate them in parallel; do not refine one seed over many generations. Diversity
belongs at seeding, breadth-first — iteration is for polish, not discovery. Stage-4 width is
**memory-ledger-derived**; **512 = the pool cap, never a target** (q-069). A run that spends its
budget on generations rather than candidates is leaving the architecture unused, even if it
eventually arrives somewhere good.

**Evaluation throughput (MHz) is a SUPPORTING goal, not an end.** Speed exists to explore the design
space quickly enough to find well-tiled designs. A fast engine that returns a poorly-tiled roof has
failed; a slower engine that tiles it well has not. When perf work and coverage work compete, coverage
wins — and a perf number is never a substitute for a coverage number.

## The north star (non-negotiable)
- **Physics-correct, first-principles.** Real analytic mechanics — statics, strength-of-materials, code-reduced
  allowables — never proxies, curve-fits, or fudge factors. Every structural number must be **derivable and provable**
  against a closed-form result or an independent oracle (PyNite/CalculiX).
- **Maximum truth-seeking / no-bullshit.** The tool **never claims what it cannot prove.** If something is uncertain,
  say so and make it conservative — never paper over a gap with a plausible-looking number.
- **Buildable, not merely plausible.** Outputs are fabricable assemblies with real catalogue parts, real load paths,
  real allowables. A layout without a valid structural solution is not an answer.
- **Accuracy-first.** Correctness beats cleverness beats speed. Speed comes from the *architecture* (the keystone),
  never from cutting physical fidelity.

## Scope statements are not requirements (SC-95)

A spec sentence describing **what the system currently does** is not a statement of **what it must
do**. When a v1 narrowing gets written into the law layer, every downstream brief propagates it and
the narrowing hardens into the design — bounds, receipts, tests and docs all agree, so nobody sees it.
The tell is agreement.

Caught 2026-07-27, five days after it was minted and one day after it became law: the production
structure search could not add a member of steel (`ADD`/`RELOCATE`/`STORY_SHIFT`/`SPLICE_RUN` were
then gated at `core/search/graph_mutation.py:60-67`), which made q-060's "denser frame, more
supports" unimplementable. It reached the North Star and the decision-lock as a **controller**
amendment bundled into a commit of **owner** rulings, and was thereafter cited as ratified law.
(Status correction 2026-08-03, t-404 census: `ADD` and `SPLICE_RUN` are now LIVE in the production
tuple at `core/search/graph_edit_decode.py:72-79` — landed by b-747 W2F; only `RELOCATE`/
`STORY_SHIFT` remain gated, `SCOPE-v1(t-404)`. The incident record above is history; the operator
capability statement it described is no longer current, and citing it as a live gate is itself an
SC-95 violation in the opposite direction.)

Rules, all mechanically checkable:
- **An amendment that REDUCES capability is an owner decision.** Never a controller conformance
  ruling, and never in the same commit as owner rulings — provenance must be visible in the log.
- **A conformance verdict with GAPs is not CONFORMS.** q-050 scored 1 DIVERGED · 1 PARTIAL · 4 GAP
  of 18 and was titled CONFORMS by amending the spec to match the code. **The pseudocode wins; if
  code disagrees with the North Star, the code is wrong** — amending the spec to agree is the lane
  rule inverted.
- **Deferred is not defined.** A current limitation carries `SCOPE-v1(<ticket>)` in the law layer, so
  a reader can tell a limitation from a requirement and knows where the work is tracked.
- **Cite what the entry says.** A claim citing `q-xxx`/`D-Qxx` must be checkable against that entry's
  actual text and its confidence tier. "GENUINE (q-050)" for a ban q-050 never stated is how this
  survived.
- **Publish the reachable space.** The capability boundary is a DISCLOSED OUTPUT, not an internal
  fact — you cannot quietly narrow what you must state on every candidate. This is the only one of
  these that enforces itself.

## Load-bearing invariants (honor exactly)
- **HARD-FAIL, never silent-fail.** An engine ERROR crashes loudly. A valid *infeasible candidate* is a status output,
  not an error — keep that distinction sharp. Never return a silently-wrong number; no `except: pass`; never clamp or
  coerce bad input into a result that looks fine.
- **The keystone (CORE-CONCEPT.md, validated 0% in `prototype/influence_validation.py`).** Structural mechanics done
  ONCE at pack time as influence matrices (`K_c → G`); the hot loop is branch-free `resp = base + G @ P` vs baked
  allowables. **Accuracy grows by enriching the precomputed tables, never by complicating the kernel.**
- **Conservative under uncertainty.** When a modeling choice is ambiguous, take the safe side (load-up / capacity-down)
  and flag it.
- **Provable.** Favor designs with a free correctness test — Betti/Maxwell `G`-symmetry, exact superposition, parity vs
  PyNite/CalculiX. A claim without a check is a liability, not a feature.

## How to work
- **Verify before editing.** Check repo state, target files, and assumptions first.
- **The decision-lock is law.** `specs/decision-lock-2026-06-21.md` (D-Q1…D-Q86+) is authoritative and supersedes older
  docs where they conflict. Don't re-litigate ratified decisions — conform to them.
- **Stay in scope.** Do the chunk; surface out-of-scope findings for the backlog, don't silently expand.
- **Surface blockers, don't work around them.** A sandbox/permission/auth/write block returns `BLOCKED:` — you do not
  invent an alternate delivery path.
- **Infeasible is an owner verdict, not a workaround (owner ruling 2026-07-16).** Never bless a bug-caused site
  failure as "expected infeasible": a failing must-pass site is RED with the bug named (`RED-BUG(b-xxx)` strict-xfail
  that flips loudly when fixed). If you believe a case is GENUINELY physics-infeasible, you do not encode that verdict
  yourself — list it as `OWNER-REVIEW` and surface it; only an owner adjudication (cited `q-xxx`) may bless
  infeasibility. Silently institutionalizing infeasibility is a P0 defect.
- **NEVER iterate procedurally over candidates (owner rule 2026-07-31).** Do not loop in Python over candidate
  girder/structure designs, structure mutates, screens, or placement mutates/evals — the MHz hotpath and its queues.
  The engine's speed IS one big batched PyTorch operation over the whole candidate population; a Python loop breaks
  it into thousands-to-millions of tiny tensor ops, and per-op dispatch/kernel-launch overhead then swamps the
  compute — a breaking perf regression (MHz → kHz), even when each iteration "looks cheap." Batch across candidates;
  per-candidate work runs only on gate survivors. This holds regardless of file or tier label (work that scales with
  candidate count is hot-tier wherever it sits — D-Q77). Hotpath dispatches get the full protocol + guard recipes in
  `docs-private/rag/PLACEMENT-HOTPATH-CONTEXT.md` (forbidden moves #1 and #13).
- **Self-review before handoff against this mandate:** is every number physical, provable, conservative, and hard-failing?

> The reviewer is P.Eng-level and values first-principles truth over agreeableness. Disagree with evidence when the
> facts warrant it; never agree just to be agreeable. The goal is the most correct, buildable answer — not the most pleasing one.

---


---

# YOUR TASK — verify 14 already-closed rows that were never reviewed

Each row is marked **done** in the store but `done_reviewed` is false. Its `resolution`
field records what someone claimed was done. **You are verifying that specific claim against
the current release tip** — not re-auditing the row from scratch. That is what makes this
cheaper than a fresh audit and it is also the trap: it is very easy to read a confident
resolution and agree with it.

## Verdicts

| verdict | when | required evidence |
|---|---|---|
| `CONFIRMED-CLOSED` | the defect genuinely cannot occur at the tip | **quote the CURRENT construct and say why the defect cannot occur.** Absence of a grep hit is NEVER sufficient |
| `STILL-OPEN` | the defect is still there | the current source line that exhibits it — this row gets REOPENED |
| `STRANDED` | the work exists but never reached the train | name the commit and show the content is absent from the tip |
| `UNCHECKABLE` | cannot be settled from source | what would settle it |

## The bar, and why it is this one

*"I could not find the defect"* and *"the construct now reads X, so the defect cannot occur"*
feel identical while you are writing them. Only the second survives another reader. Measured
on this very corpus: the first bar scored **5/15** on independent re-read, the second **15/15**.

## Specific traps for THIS population

- **A resolution that says "handed to X" is not a closure.** Six rows in this pile are closed
  as *"integrated on burn8/integration @f3352fce, handed to MAIN"* and that work is provably
  not on the train — the test it added does not exist at the tip. If a resolution names a
  branch or a handoff, **check the content reached the tip**, not that the commit exists.
- **Sha reachability is not the test.** Rebase and squash reland the same content under a
  different sha. Check content presence.
- **Do not trust line numbers** in the row text — they are thousands of commits stale. Grep
  for the construct.
- If the row's resolution cites no evidence at all, that is fine and common (512 of 524 have
  no sha) — go read the named surface and form the verdict yourself.

## Deliverable

`docs-private/reviews/2026-08-24-bug-triage/waves/awaiting_batch31.md`:
`| id | verdict | current-source evidence |`

Also add, per row, whether you believe it bears on **JOB SUCCESS RATE** or **MEDIAN CARDS
PER ARCHIVE** — the two metrics the fleet is now measuring. Say `neither` when it does not;
do not stretch.

Commit with an explicit pathspec.

## Self-review

Null hypothesis: *"I agreed with a confident resolution without opening the file."* For every
`CONFIRMED-CLOSED`, name the file you opened and the line you read. If you cannot, it is
`UNCHECKABLE`.

Final line: `COMPLETE: <sha> <confirmed>/<still-open>/<stranded>/<uncheckable>`


---

# Awaiting-review batch 31 — 14 rows, RANDOM (seed 20260837)

Each row is **already marked done** but was never reviewed. Verify its `resolution`
claim against the current release tip.

Prior from 378 rows across 27 batches: **29% of rows marked done were NOT closed**
(20% still-open, 9% stranded). Steady across seven aggregations. Per-batch it swings
7%-57%, so do not calibrate to your own batch.

`STRANDED` — work on a commit that is NOT an ancestor of the tip — is the most valuable
verdict here: 33 carriers so far were finished work that never got boarded, and main has
already merged four of them straight off this sweep. Name the commit AND show its
construct is absent at the tip. Both halves.

**Reachability is not the test; content is.** One row's symbol was present at the tip
with the WRONG VALUE — ancestry alone called it fixed.

**If a probe returns nothing, re-run it a different way** (`git grep` vs `git show` vs
`ls-tree`). Five times this session an empty result was a malformed command, not a
missing construct — each would have convicted a correct reader of fabricating.

### b-865

structural_semantics.py:744 _scatter_station_loads walks S-cell CSR non-zeros in Python with .item() host sync per non-zero — same class as the b-855 hotspot, same GATE-CPU-F64 tier; minor post-fix (file is 5.7% of samples) but the next one to bite; tensorize like _eval_response_rows (found by b-855 chip 2026-07-31, mailbox @917219a3)

- recorded resolution: audit-fixed-candidate

### b-1492

P0 CONTROLLER-VERIFIED: FILING_ALLOWED is derived from ABSENCE OF COMPLAINT, not from verification. application/filing_gate.py:366 'filing_allowed = gallery_eligible and not reasons'; evaluate_filing_export_gate (:198-209) takes NO oracle-result argument. Composes catastrophically with the fail_bits=0 literal: the array path cannot populate a regulatory reason, and the gate then publishes FILING_A

- recorded resolution: audit-fixed-candidate

### b-1536

girder review F5: argmin of all-inf cluster_to_primary_cost returns primary 0 — wrong-single-match would pass the count-based identity guard; make all-inf raise (REVIEW-refute finding 5)

- recorded resolution: done

### b-1590

goal-flight store tooling defect (cross-project): `new --kind decision` auto-assigns q-prefixed ids from a store-local counter (reached q-106..q-108 here) with no awareness of the project's ratified decision-lock series (specs at q-137) — collisions let pending decisions masquerade as ratified law in downstream citations (caught in operator-facing prose, b-1491 round 3 P1-1). Fix: store decision r

- recorded resolution: audit-stale

### b-1619

OWNER DECISION (bugs seq 203A): UI wallclock ceiling 18.6s vs measured 155-175s = STALE BASELINE (seeded on width-64/winner_non_filing; flow now 10k/until-dry converged). Monotone guard refuses baseline-up BY DESIGN — needs an owner call on whether 10k/until-dry is the intended flow before any rebless

- recorded resolution: audit-fixed-candidate

### b-1642

OWNER RULING 2026-08-19 (resolves store-q-108): duplicate-collision write winner = INDEX-ORDER-WINS, ratified. Deterministic, order-independent, zero hot-loop cost; all candidates scored pre-write; archive keeps best-by-objective. Owner to mint the canonical q-row in specs (next free q-138+); until then cite OWNER-ADJUDICATED 2026-08-19 index-order-wins. Closes the b-1464 F8 question.

- recorded resolution: audit-fixed-candidate

### b-1650

b-1610 Plan B (steel RESULT follow-up): sizer cannot climb MERO leaves on adapter G (gov_mero<=1 short-circuit; rails 1100@1.25 excluded per DESIGN 6.2.4) — leftover 9002/9004/9006 unconsumed at util 0.98. Plan B = rematerialize candidate G and size against it (DESIGN in bt-steel docs-private/build/2026-08-19-steel/). This is the MP-recovery path now that H1 flexure honestly kills 7+ MP on member 

- recorded resolution: done

### b-1671

ABLATION G6: under-refill on 4014760001 (the ONLY completing witness): archive ceiling independent of ablation depth — q-102 refill-to-seed fails at a constant bank. THE live q-102 wrong-number mechanism now that G1-G4 name the blockers. Receipts: runs/4014760001-*/until-dry-progress.json

- recorded resolution: done

### b-1680

OWNER-REVIEW OR-1 (G6): q-102 refill-to->=-seed is not currently a FAIR measurable on 4014760001 — INNER is a subset selector on frozen seed steel the screen honestly rejects above the hilltop (layout-led 13, spaceframe 8-harvest/6-post-H1H2). Keep the measurable, do NOT score families infeasible; re-arm after steel is a live design variable (Plan B + live OUTER debit). DIAG bt-g6.

- recorded resolution: done

### b-1685

OWNER DESIGN VERDICT 2026-08-19 (from eyeballing plan SVGs for 4014760001): "the single-chord seed dunnage network logic makes no sense and requires a COMPLETE RETHINK" — plus "the spaceframe seems to work better". This is a DESIGN-LEVEL verdict, not a bug report, and it reframes three owner-observed defects as SYMPTOMS rather than independent fixes. THE THREE (all eye-confirmed on real output): (

- recorded resolution: audit-stale

### b-1687

OWNER HEURISTIC for the single-chord dunnage rethink (b-1685/b-1686): "one beam landing between each row of windows at typical-spacing would be a good bet as a heuristic". WHY IT IS PHYSICALLY RIGHT: windows are OPENINGS in a masonry bearing wall and you cannot bear on an opening — the solid PIER between windows is what takes load. One beam per inter-window pier therefore lands load where the wall

- recorded resolution: audit-fixed-candidate

### b-1872

b-1837 [P0, taken from main seq 30]: accepted-winner oracle (runtime_gate.py:11242) stamps VALIDATED_BY_REAL_CCX (:11474) after only GLOBAL gravity/ELF/q-111 checks; build_differential_receipt per-wall reconciliation + wall-capacity rows (calculix_differential.py:765,1170) have NO non-test caller except the direct-seed diagnostic — a globally-balanced deck with one overloaded wall gets the stamp. 

- recorded resolution: triage-duplicate

### b-1986

★ FIRST REAL E2E SUCCESS 2026-08-22: BBL 4014760001 produced 2 ELITE DESIGNS through the live UI. Job job_b035f265439545baac119433fb76a232, status=succeeded, 726 s, elite_count=2, achieved_megapacks=2, filing_blocked=true, notice=PE_CLASS_ATTESTATION_PENDING. Tree: exp/218-plus-b1901 = feat/tl-218-clear (rebased on origin/main 08f8ec1b2, so it carries b-656 authoritative-until-dry-archive publishi

- recorded resolution: audit-stale

### b-2022

HARVEST codex-32098 (b-1984, q-082 shared aisle): committed 9220d3a4e on feat/q082-shared-aisle (bt-q082, 1 ahead of origin/main, clean tree). Scope is tight — core/ir/archive_mp_extent.py -14, core/oracles/expected_megapack_count.py, tests/oracles/test_expected_megapack_count.py; net +6 lines, a consolidation of duplicated shared-aisle authority. HONEST NEGATIVE RESULT: the worker MEASURED 401476

- recorded resolution: audit-fixed-candidate


---

## Base

Verify against `2cf9289ab`. State the sha you verified against — the tip has moved eight times
during this campaign and a STRANDED verdict expires with it.

**Commit your report before you finish**, explicit pathspec.

```
