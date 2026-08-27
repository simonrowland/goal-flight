# Pinned prompt — `t746-r2-retry-15cc828e`

- source: `prompt-file`
- prompt-file: `/private/tmp/claude-501/-Users-simonrowland-Repos-pm2/d9989eb0-0cdd-4ccc-be80-cc86a4d2a55f/scratchpad/t746.md` (EXISTS on disk)
- pinned_at: 2026-08-27T14:54:06.319466+00:00

```
# t746 — the lossy numeric environment vector — id t746

Repo /Users/simonrowland/Repos/pm2, HEAD 06e62d2. GOAL-LOOP apply; NO stage/commit/push.
Env: UV_CACHE_DIR=/tmp/pm2-t746-uv PYTHONPATH=/Users/simonrowland/Repos uv run --with numpy --with scipy --with pytest --with numba --with matplotlib --with pydantic python -m pytest ...
CONCURRENT LANES — do not touch: docs-private/design/FORCE-RAIL-*.md (two design workers),
the untracked force_rail_*.py carrier modules and their tests (blocked carrier lanes),
force_rail_envelope.py and the X0 guard surfaces, and the t-752/755/757 red nodes (a cleanup
worker owns those). Your lane is environment.py's numeric vector and its consumers.

MANDATE: store ticket t-746. environment.py:638-681 serializes EncounterEnvironment to a numeric
vector that DROPS information the object carries — regime_class and body, some neutral/collision
overrides, and species labels/roles.

WHY THIS IS CORRECTNESS, NOT TIDINESS (the operator's framing, and the reason this is worth
doing): the whole claim-reproduction design pins a case by its INPUT columns and expects two rows
with identical inputs to MEAN the same thing. If the numeric vector is lossy against the object,
a pin round-tripped through the vector is NOT the same environment — two rows can agree on every
stored number while differing in regime_class, species identity, or an override. That silently
breaks the flat-table comparison premise: "select * where vector-dof = x" would group rows that
are not actually the same case.

DO THIS:
1. Establish the loss precisely by EXECUTION: build environments that differ ONLY in each
   dropped field, encode them, and show the vectors are identical. That is the demonstrated
   failure; quote it. Enumerate every field the object carries and the vector does not.
2. Choose and state the fix. The two honest shapes are (a) make the vector LOSSLESS for identity
   purposes — carry the dropped fields (encoded, e.g. categorical codes for regime_class/body and
   a canonical species-identity encoding), or (b) keep the vector numeric-only but make the
   IDENTITY of a case explicitly the object, with the vector demoted to a derived projection that
   may never stand in for identity — which requires finding every consumer that currently treats
   the vector AS identity and refusing or fixing it. Prefer whichever is simplest-correct given
   the real consumers; say why, and name the consumers either way.
3. Whichever you choose, the acceptance is the same: two environments differing in ANY carried
   field must not be able to present as the same case to any identity/comparison/pin consumer.
   Prove it by execution per dropped field.
CAUTION: the 85D vector width and existing hashes are load-bearing elsewhere (config-vector
receipts, surrogate rows, kiln contract). If your fix moves the width or any pinned hash, that is
a CONTRACT CHANGE — stop and report it with the blast radius rather than landing it; a lossless
fix that silently rewidens the vector would break the bilateral contract we just verified
byte-identical with kiln.
GATES: owning suites of every edited file; both PM2_INVARIANT_TIER tiers via git ls-files;
operator mechanical + Python gates; full-tree added-red diff against a clean HEAD archive.
Notes: docs-private/reviews/2026-08-25-t746/t746-notes.md.
Terminal: !COMPLETE: t746 — <loss enumerated + fix shape + per-field proof>

```
