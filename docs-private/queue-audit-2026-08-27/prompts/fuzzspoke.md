# Pinned prompt — `fuzzspoke`

- source: `prompt-file`
- prompt-file: `/private/tmp/claude-501/-Users-simonrowland-Repos-pm2/d9989eb0-0cdd-4ccc-be80-cc86a4d2a55f/scratchpad/fuzzspoke.md` (EXISTS on disk)
- pinned_at: 2026-08-27T14:54:06.319466+00:00

```
# fuzzspoke — teach the fuzzer generator the SPOKE aggregate ceiling — id fuzzspoke

Repo /Users/simonrowland/Repos/pm2, HEAD 8f4e98d. GOAL-LOOP apply; NO stage/commit/push.
Env: UV_CACHE_DIR=/tmp/pm2-fuzz-uv PYTHONPATH=/Users/simonrowland/Repos uv run --with numpy --with scipy --with pytest --with numba --with matplotlib --with pydantic python -m pytest ...
TESTING DISCIPLINE: targeted/owning suites ONLY. Do NOT run the full tree and do NOT build a
clean-HEAD archive — several lanes are uncommitted in this shared tree so full-tree numbers are
neither attributable nor cheap. The controller measures added-red at commit time.
LANE: the fuzzer sampling path (surrogate/fuzzing/sampling.py or wherever random/LHS generation
derives its per-carrier ceilings) and surrogate/tests/test_fuzzer.py. Other lanes own
force_rail_*.py, environment.py, simulation_point.py, evaluators/batch.py — do not touch.

THE DEFECT (reproduce it first): surrogate/tests/test_fuzzer.py::TestFuzzOne::
test_single_eval_succeeds and ::test_invariants_pass_on_good_config fail with
StatorCurrentEnvelopeRefusal `carrier=I_DC_A*N_spokes requested_A=500000 limit_A=60000`.
generate_random(...) emits a SPOKE config whose aggregate ampere-turns exceed the landed cap, so
a fixture whose entire purpose is to be a GOOD config is invalid by construction.
ROOT CAUSE: an earlier fix taught the generator the RING/SLOUGH ring-current ceiling — the same
test file asserts `vec[18] <= log10(100_000)` for those two types — but SPOKE drives the field
through a DIFFERENT carrier (I_DC_A x N_spokes, an aggregate), which was never given a ceiling.
This is the guard-scope-narrower-than-the-invariant shape: the fix covered one carrier family and
the sampler kept emitting invalid rows for the other.

FIX: derive the SPOKE sampling ceiling from the operator current table exactly as the ring
carriers do, so generated configs are valid BY CONSTRUCTION. Do NOT relax the envelope, do NOT
special-case or skip the failing tests, and do NOT clamp after the fact if the ceiling can be
respected at sampling time. Then ADD the missing SPOKE assertion alongside the existing
RING/SLOUGH one, so the generator contract is pinned for EVERY carrier rather than the two that
happened to break first — that is the actual lesson here.
SWEEP WHILE YOU ARE THERE: check whether any OTHER stator carrier (PANCAKE, BACK_EMF) is sampled
without a ceiling derived from the same table, and report what you find even if you do not change
it. A per-carrier census beats fixing the one that failed.
RED-first and non-circular: the RED must break PRODUCTION generation behaviour, not mutate a test
value. GATES: surrogate/tests/ whole, plus both PM2_INVARIANT_TIER tiers via git ls-files, plus
the operator mechanical + Python gates.
Notes: docs-private/reviews/2026-08-25-fuzzspoke/notes.md.
Terminal: !COMPLETE: fuzzspoke — <fix + per-carrier census>

```
