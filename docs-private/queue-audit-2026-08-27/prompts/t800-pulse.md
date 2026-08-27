# Pinned prompt — `t800-pulse`

- source: `prompt-file`
- prompt-file: `/private/tmp/pm2-engine-t800/BRIEF-t800.md` (EXISTS on disk)
- pinned_at: 2026-08-27T14:54:06.319466+00:00

```
# t-800: pulse/reactive adequacy — the store can be joule-rich and still unable to source the chirp edge

## Who/where
You are a codex implementation worker for controller pm2-engine.
Worktree: /private/tmp/pm2-engine-t800/pm2  (branch engine-t800-pulse, base origin/main 51c0ad1).
The repo root IS the `pm2` package. NEVER cd out; NEVER touch /Users/simonrowland/Repos/pm2
or other /private/tmp/pm2-engine-* trees.

## Gate command (the ONLY test form — no `uv`, no bare pytest)
PM2_KILN_REPO=/Users/simonrowland/Repos/kiln PYTHONPATH=/private/tmp/pm2-engine-t800 \
  /Users/simonrowland/Repos/pm2/.venv/bin/python -u -m pytest -q -p no:randomly <targets>

## Required reading (in worktree, before coding)
- docs-private/SIMONS_PM2_MANDATE.md + RF-APWP-COUPLING-FRAMEWORK-PLAN-v2-2026-06-15.md
  + AMENDMENT (copied in; gitignored, do NOT commit them).
- rf/coupling/force_rail_power.py — the WHOLE lifecycle P(t) machinery (Operator Decision 1
  = Option A is LANDED: LifecyclePowerTimeAuthority is the sole P(t); laws CONSTANT /
  LINEAR / NORMALIZED_WAVEFORM; peak/avg/energy DERIVED; SCALAR poisoned;
  segment_law_slope_W_s exists — consume it, do not re-derive slopes).
  ResolvedStorageAuthority is at ~line 957-963.
- rf/coupling/force_rail_attachment.py (carrier shapes), tests/rf/test_force_rail_power*.py
  (existing contract-test idiom — follow it).

## The operator's requirement (verbatim intent, 2026-08-26 — BINDING)
Budget BOTH volumetric power AND spiky bus demand (chirping, pulsed power) with SMES as the
main operating store, including while harvesters run. If volumetric power is satisfied but
the reactive / capacitor-type pulsed power is NOT, the model must ELUCIDATE that rather
than pass silently. Pulse adequacy is a SEPARATE NAMED EXTENSION with a first-class,
VISIBLE verdict — not an internal check that only surfaces on failure.

SIMPLIFYING ASSUMPTION (granted, and NARROW): power delivery is an adequate BUS-BAR from
store or generation — do NOT model distribution conductor R/L as a limiter. **The BUS-BAR
is ideal; the STORE IS NOT.** Assuming the source adequate too would assume away the exact
question being asked.

## Measured gap
ResolvedStorageAuthority carries ONLY capacity_J, initial_energy_J, recharge_power_W,
recharge_path_id, provenance — a RECHARGE limit and NO discharge power/current limit, no
terminal voltage, no dI/dt, no ESR, no C. Inductance exists only on the A1 FIELD carrier
(coil circuit gate), not as a source transient constraint. So an energy-only budget passes
exactly where the operator needs a refusal.

## Scope (from store item t-800 + operator refinement)
1. Extend the P1 storage/source carrier with source-shape capability: discharge power
   limit AND current limit, terminal voltage, source L (or an explicit dP/dt / dI/dt
   ceiling); C and ESR only if capacitor pulse-forming is real in the design (check the
   design docs — if it is not real, do NOT add C/ESR fields; no ceremony).
2. ONE derived check, per-subsystem per-segment: does the demanded segment slope
   (LINEAR start_W/end_W; NORMALIZED_WAVEFORM scale_W × shape slope — Option A states
   these directly) exceed what the source can deliver (V/L bound for SMES;
   generator response time if modelled)? Emit a per-segment pulse-adequacy RECEIPT that
   DISTINGUISHES volumetric-adequate-but-rate-inadequate — that named verdict is the
   operator's failure case.
3. FAIL-CLOSED: unresolved pulse capability REFUSES (typed refusal token, follow the
   existing force_rail_power refusal idiom). An unstated dI/dt ceiling must NOT read as
   infinite.
4. recharge_power_W (harvester-fed) counts toward the ENERGY budget but does NOT relieve
   a dI/dt/slew limit — recharge rate and discharge slew are different quantities. Make a
   test prove this distinction (recharge high, slew low → still refuses the edge).

## Physics (derivation comments REQUIRED, premise → algebra → units → sanity)
SMES: E = (1/2) L I², so P = V·I and dI/dt = V/L. Sourcing a demanded dP/dt at operating
point (V, I): dP/dt = V·dI/dt + I·dV/dt; with terminal voltage bounded by V_max, the
sustainable slope bound must be derived and commented in-line, including the unit check
(W/s) and a limiting-case sanity (I→0 and V→V_max). Do not bury a bare formula.

## Anti-ceremony / anti-false-green
- Every new field must name the demonstrated failure it prevents and its consumer (the
  derived check + receipt). No speculative knobs. Bound + cost every new DOF.
- The verdict must be reachable and TESTED both ways: a config that passes volumetric AND
  pulse; a config that passes volumetric but FAILS pulse (the named case); an unresolved
  config that refuses.
- Do not weaken or bypass existing refusal chains (FIELD_SOURCE_JOIN_UNRESOLVED comes
  first on unresolved configs — assert what you observe at the layer you test).

## Discipline
- Owning-suite closure: gate the FULL owning suites of every edited subject (at minimum
  tests/rf/test_force_rail_power*.py and every suite whose subject you touch).
- Self-review adversarially before COMPLETE (exploit: can an optimizer chirp for free?
  honesty: does any test pass vacuously?). Fix and re-gate until clean.
- Commit locally on the branch with explicit pathspecs, message = the NET diff. Do NOT push.
- Markers to stdout: STATUS: lines as you go; BLOCKED: <reason>; COMPLETE: <summary +
  gate counts + sha>.

```
