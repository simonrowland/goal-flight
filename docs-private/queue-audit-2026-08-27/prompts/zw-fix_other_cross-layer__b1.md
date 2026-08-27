# Pinned prompt — `zw-fix_other_cross-layer__b1`

- source: `prompt-file`
- prompt-file: `/tmp/goal-flight-501/dispatch/zw-fix_other_cross-layer__b1.assembled.prompt` (EXISTS on disk)
- note: prompt_file taken from ledger.prompt_path
- pinned_at: 2026-08-27T14:54:06.319466+00:00

```
You have a steer mailbox at `$GOALFLIGHT_STEER_FILE`. Read it AT THE TOP OF EACH ITERATION and IMMEDIATELY BEFORE ANY git commit/push. Incorporate new messages into your plan; ack each with `!STEER-ACK: <seq>` on its own line; a steer may redirect or HALT you — honor it.

Your FULL original brief is at `$GOALFLIGHT_PROMPT_FILE`. Re-read it after any internal compaction/summarization, at the start of each long-run goal-loop iteration, and before final commit/exit; the disk file is authoritative over summarized memory.

Terminal evidence identity contract:
- Every terminal marker payload starts with the exact dispatch id `zw-fix_other_cross-layer__b1`.
- Successful final shape: `!COMPLETE: zw-fix_other_cross-layer__b1 — <summary>`.
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

# YOUR TASK — ZERO campaign, FIX WAVE 2 (pilot batch): fix_other_cross-layer__b1 batch

Wave 1 triaged the whole open backlog and asked one question per row: *does this
defect still reproduce on origin/main?* Your 16 rows all answered **yes**, each
verified by an independent worker who quoted the current source or the command
output that shows the defect. Those observations are in your manifest.

**You are fixing these.** This is real production work, not triage.

## The bar

Every fix lands **RED-first**:

1. Write a test that FAILS on current `origin/main` and NAMES the row id (e.g.
   `tests/.../test_b563_ccx_spring_support.py`, or a `b-563` test inside an
   existing module). Run it. **Paste the failing output into your report.**
2. Then make the change that turns it green. Run the test again; paste the pass.
3. Run the focused surrounding tests for the files you touched. Do not run the
   full suite — it is long and another controller's gate owns it.

A fix with no failing-test-first evidence is not accepted here. "I read the code
and it looks right now" is how a defect gets closed while staying live.

## Scope discipline — this is the part most likely to go wrong

Your lane is the files named by YOUR rows.  If fixing a row correctly requires editing
`core/search/*`, `core/struct_pack/*`, `core/hotpath/*`, or
`application/ui_backend/*`, **STOP on that row.** Those belong to battery-engine
and battery-webui, who have live work in them right now; editing them from here
causes a merge collision at best and silently reverts a peer's fix at worst.

Bucket such a row `DEFER-ROUTE`, name the file and the controller it belongs to,
and describe the fix you would have made. That is a complete, useful answer.

Do not widen a row's scope. Fix the defect that was reproduced, not the
neighbourhood it lives in. If you find an adjacent defect, write it down in a
`## Adjacent findings` section — do not fix it.

## Physics and honesty rules that bind these specific rows

- Several rows touch **structural load path** (`b-563` is a CCX deck emitting
  rigid `*BOUNDARY` where the support is not rigid). For any change to
  structural behaviour, the code comment must carry the DERIVATION — premise,
  the algebra, a unit/dimension check, and a sanity check against a known value
  or limiting case. A bare formula hides whether the arithmetic is right.
- **Hard-fail, never silent-fail.** Where a row is about a silent acceptance
  (`b-579`-shaped defects: accepting any bytes, recording a placeholder state),
  the fix is to FAIL LOUDLY with a named error, not to improve the placeholder.
- **Never bless infeasibility.** If a row turns out to be genuinely
  physics-infeasible, bucket it `OWNER-REVIEW` — only the owner adjudicates that.
- **No megapack-count or coverage figures** in your report (b-2131 suspension).
- If a row does NOT actually reproduce for you, say so plainly and bucket it
  `NOT-REPRODUCED` with what you observed. Wave 1 could be wrong; contradicting
  it with evidence is a valid and valuable outcome, and is not a failure.

## Commits

One commit per row (or per coherent group of rows sharing one root surface),
with an explicit pathspec: `git commit -- <files>`. Never a bare `git commit`.
The message states the row id, the defect, and the test that now covers it.

## Deliverable

`docs-private/reviews/2026-08-24-bug-triage/waves/fix_other_cross-layer__b1.md`:

``​`
# Fix wave 2 pilot — adapters/exporters

Baseline: origin/main <sha>.  Rows: 16.  Fixed: <n>.

| id | outcome | commit | test | evidence |
|---|---|---|---|---|
| b-563 | FIXED | <sha> | tests/... | RED output ... -> GREEN output ... |
``​`

Outcomes: `FIXED` · `DEFER-ROUTE` · `NOT-REPRODUCED` · `OWNER-REVIEW` · `BLOCKED`.

## Self-review before handoff

Null hypothesis: *"these fixes are wrong because ___"*. Then verify each way and
show the evidence:
- Did every FIXED row get a test that actually failed first? Quote the red.
- Did any change touch `core/` or `application/ui_backend/`? That is a scope
  breach — revert it and re-bucket as `DEFER-ROUTE`.
- Did any structural change land without its derivation in the comment?
- Did any fix make a silent failure quieter rather than louder?
- Do the focused tests around every touched file still pass?

Final line: `COMPLETE: <branch> <sha> <fixed>/<total>` — nothing after it.

## Your 13 rows

# Fix batch: fix_other_cross-layer__b1

Cluster region `other/cross-layer` · 13 rows.

Every row below was REPRODUCED on origin/main `0bcbebe2a` during wave 1 by an independent worker. The evidence line is that worker's own observation, quoted verbatim — current source or command output, not a guess.

### b-035  ·  other/cross-layer / SC-08

SC-08: NYC CLI e2e tests omit explicit --device cpu, so post-merge CI is device-heterogeneous [tests/e2e/test_cli_hourglass.py:441]

- reproduced as: Current `tests/e2e/test_cli_hourglass.py:117-130` builds `exe = ... [sys.executable, "-m", "application.cli.main"]` and then `argv = list(logical) + ["--out", str(out_dir)]`; the real invocation at `:368-370` is `shared_cli_run("--fixture", "hourglass")`. No explicit device is supplied.

### b-211  ·  other/cross-layer / workflow-tooling-gap

IF-STORM P1/P2 backlog umbrella: 17 P1 + 12 P2 deduped tickets (plus 16 likely-superseded to verify against train2) live in docs-private/reviews/2026-07-10-interface-storm/DEDUPED-TICKETS.md — promote individually as the P0 wave (b-194..b-210) drains; do not let the file rot un-triaged.

- reproduced as: A Python exact-title comparison of `origin/main:docs-private/reviews/2026-07-10-interface-storm/DEDUPED-TICKETS.md` against `origin/main:docs-private/tasks.jsonl` observed `dedup_capture_rows 29 P1 17 P2 12`, `exact_title_promotions 0`, and `b-211_done False`. The named individual promotion backlog therefore remains.

### b-234  ·  other/cross-layer / provenance-drift

PLAN SVG POLISH (P2, owner-eyeball on the styled artifact): (1) y-axis is mirrored vs north-up plan convention (exporter maps site-local y directly into SVG y-down) — add the y-flip transform + a north arrow derived from dominant_wall_bearing provenance; (2) identify/label the orange keepout regions' semantic kind in a legend (courtyard setback vs fire tower vs obstruction). Presentation only — geometry/provenance verified correct.

- reproduced as: `adapters/exporters/dxf_svg.py:607-669` emits the site-local view box and layer groups without a coordinate transform, and `:677-699` writes every raw `(x, y)` directly into SVG points/`y1`/`y2`/text coordinates. The current renderer therefore remains y-down and contains neither a north-arrow nor semantic exclusion legend emission.

### b-259  ·  other/cross-layer / procedural-throughput

Extend the b-245 static creep-lint forbidden constructs (hot/warm tiers): torch.unique / np.unique / sort-based dedupe on candidate/config axes — variable-length output = data-dependent shapes = procedural trapdoor (owner b-258). One-line lint addition + a synthetic offender case in the meta-test; land with or right after the guards batch

- reproduced as: The requested guard extension is absent while the forbidden class remains live. Current `core/hotpath/mutation/structure_ops.py:433-459` marks `_resident_ids_for_legal_tuples` `TIER: WARM-PACK` and calls `np.unique(legal_positions)`; `:465-509` marks the descriptor builder WARM-PACK and uses per-axis `np.unique`. The former b-245 guard file is absent from origin/main, and no replacement guard rejects these calls.

### b-273  ·  other/cross-layer / presentation-contract-gap

[WAVE2] support-combination-coherence — brief docs-private/build/2026-07-14-storm-flight/WAVE2-support-combination-coherence.md (routing+deps per CAMPAIGN-ORG wave-2 roster; dual-leg acceptance)

- reproduced as: `core/struct_pack/stiffness.py:3908-3917` constructs several load-combination responses, but `:3943-3952` computes `winner = np.argmax(score, axis=0)` and applies that winner independently by response row. The packed base, scale, and combination IDs can therefore form a rowwise hybrid rather than one coherent governing combination.

### b-274  ·  other/cross-layer / presentation-contract-gap

[WAVE2] engaged-bracket-reactions — brief docs-private/build/2026-07-14-storm-flight/WAVE2-engaged-bracket-reactions.md (routing+deps per CAMPAIGN-ORG wave-2 roster; dual-leg acceptance)

- reproduced as: Current catalogue derivation remains `equal-stiffness rigid-base statics` (`data/catalog/equipment_seed_v1.yaml:250-254`). `core/struct_pack/pack_arrays.py:4054-4060` explicitly derives densified transverse shares from the same equal-stiffness rigid-base premise, not the actually engaged rail/support stiffness set.

### b-275  ·  other/cross-layer / presentation-contract-gap

[WAVE2] gp-dimension-hardfail — brief docs-private/build/2026-07-14-storm-flight/WAVE2-gp-dimension-hardfail.md (routing+deps per CAMPAIGN-ORG wave-2 roster; dual-leg acceptance)

- reproduced as: `core/struct_pack/stiffness.py:7838-7847` permits independent `n_load` and `n_station`, yet `:7981-7987` emits identity only when they match and otherwise silently emits `np.zeros((n_station, n_load))`. The mismatched dimension remains a zero influence surface rather than a named hard-fail.

### b-276  ·  other/cross-layer / physics-model-gap

[WAVE2] wall-influence-direct-map — brief docs-private/build/2026-07-14-storm-flight/WAVE2-wall-influence-direct-map.md (routing+deps per CAMPAIGN-ORG wave-2 roster; dual-leg acceptance)

- reproduced as: `core/struct_pack/pack_executor.py:3852-3859` reconstructs support response rows through `target_g @ np.linalg.pinv(basis_g)` and `row_coeff @ basis_g`. This is still an inferred pseudoinverse map instead of the canonical direct map `G_wall = M @ G_R` implemented at `core/struct_pack/bank_account.py:229-230`.

### b-277  ·  other/cross-layer / presentation-contract-gap

[WAVE2] finalizer-preserve-repack-bits — brief docs-private/build/2026-07-14-storm-flight/WAVE2-finalizer-preserve-repack-bits.md (routing+deps per CAMPAIGN-ORG wave-2 roster; dual-leg acceptance)

- reproduced as: `_evaluate_rung` starts from `belt_controls["structural_fail_bits"]` at `core/finalizers/winner_belt_finalization.py:1292-1298`. The repack overlay at `:2055-2063` copies IDs, reactions, belt demands, and heatmap inputs but never copies the repack `structural_fail_bits`, so non-belt repack failures are lost.

### b-279  ·  other/cross-layer / presentation-contract-gap

[WAVE2] archive-niche-authority — brief docs-private/build/2026-07-14-storm-flight/WAVE2-archive-niche-authority.md (routing+deps per CAMPAIGN-ORG wave-2 roster; dual-leg acceptance)

- reproduced as: `core/search/live_archive.py:386-390` copies both `descriptor_bin` and caller-supplied `archive_cell_id` directly from the trace, and `:173-179` indexes the incumbent map by that supplied cell. The accepted-row validator does not derive the flat cell from the descriptor and compare it, so the caller remains niche authority.

### b-280  ·  other/cross-layer / presentation-contract-gap

[WAVE2] immutable-generation-handoff — brief docs-private/build/2026-07-14-storm-flight/WAVE2-immutable-generation-handoff.md (routing+deps per CAMPAIGN-ORG wave-2 roster; dual-leg acceptance)

- reproduced as: `core/search/queue_manager.py:2766-2773` uses `.detach().to(device="cpu", dtype=...).numpy()` without `clone()` or a read-only boundary, then passes those arrays to downstream callbacks at `:2779-2786`. When input already has the target device/dtype, the writable NumPy view can alias generation storage.

### b-282  ·  other/cross-layer / presentation-contract-gap

[WAVE2] response-family-canonical-range — brief docs-private/build/2026-07-14-storm-flight/WAVE2-response-family-canonical-range.md (routing+deps per CAMPAIGN-ORG wave-2 roster; dual-leg acceptance)

- reproduced as: Canonical IR still restricts `resp_family_id` to family 0–9 at `specs/structural-ir-canonical-build-spec-v1.md:206`, while `core/structural_response_registry.py:44-45` assigns live post families 10 and 11 and `:78-88` labels both `canonical_spec_family=False`. The canonical range and production registry remain divergent.

### b-283  ·  other/cross-layer / presentation-contract-gap

[WAVE2] gravity-support-fail-bit — brief docs-private/build/2026-07-14-storm-flight/WAVE2-gravity-support-fail-bit.md (routing+deps per CAMPAIGN-ORG wave-2 roster; dual-leg acceptance)

- reproduced as: Ground-post gravity reaction rows use family `RESP_FAMILY_R` but assign `STRUCT_FAIL_INTERFACE_ANCHORAGE` at `core/struct_pack/stiffness.py:6961-6970`; generic bearing reaction rows repeat that assignment at `:8894-8902`. Gravity support failure is still misclassified as anchorage.


Worktree `/Users/simonrowland/Repos/battery-tool-v2/.cache/worktrees/bt-zw-fix_other_cross-layer__b1`, DETACHED at origin/main 0bcbebe2a.

```
