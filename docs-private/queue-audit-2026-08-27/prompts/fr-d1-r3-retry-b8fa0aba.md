# Pinned prompt — `fr-d1-r3-retry-b8fa0aba`

- source: `prompt-file`
- prompt-file: `/private/tmp/claude-501/-Users-simonrowland-Repos-pm2/d9989eb0-0cdd-4ccc-be80-cc86a4d2a55f/scratchpad/fr-d1-r3.md` (EXISTS on disk)
- pinned_at: 2026-08-27T14:54:06.319466+00:00

```
# fr-d1 — force-rail carrier D1 (device-plasma carrier) — id fr-d1-r3

Repo /Users/simonrowland/Repos/pm2, HEAD 10d2a00, tree CLEAN. GOAL-LOOP apply; NO stage/commit/push.
Env: UV_CACHE_DIR=/tmp/pm2-d1-uv PYTHONPATH=/Users/simonrowland/Repos uv run --with numpy --with scipy --with pytest --with numba --with matplotlib --with pydantic python -m pytest ...

AUTHORITY: the D1 atom in docs-private/design/FORCE-RAIL-CHUNKS-2026-08-24.md (read YOUR atom
section in full, plus its acceptance clause), and the structures it references in the companion
docs-private/design/FORCE-RAIL-REPRESENTATION-2026-08-24.md. The design is FROZEN (03ccb2e plus
four committed amendments). X0 has LANDED at 49995a3 — read force_rail_envelope.py and its guards
first; you build on that spine and do not re-litigate it.

FOUR SIBLINGS RUN CONCURRENTLY on the other carrier lanes (A1 field, E1 encounter, P1 power,
I1 injection, D1 device-plasma). Lanes are disjoint BY DESIGN: each carrier lands as its own
module and NO carrier edits shared aggregate declarations — X1 later owns public traversal. If
you find yourself needing to edit a shared aggregate declaration or another lane's file, STOP and
report it as an out-of-lane need; do not reach.

STOP-AND-REPORT DISCIPLINE (this program's most valuable habit): X0 halted three times on real
frozen-design gaps — a missing executable wire contract, an unsatisfiable consumer census, and
unsatisfiable ordering claims — and was right all three times. If your atom hits a contradiction,
an unsatisfiable ordering, or a clause that cannot be implemented as frozen, STOP and report it
as a design finding. Never improvise a different design, even when the fix looks obvious.

LESSONS TO APPLY PRE-EMPTIVELY (each cost this program a review cycle):
- Enumerated-seam conformance is NOT the property holding. X0 satisfied every seam its census
  listed while its sealed-authority fence was still merely ADVISORY — a config could claim one
  arm while ordinary production read the other. Where your atom claims a structural guarantee,
  PROVE it by driving a real specimen through the actual production consumers.
- A typed carrier's EXISTENCE asserts it was validated, so never mint one from unvalidated input
  (validate shape/version/digest first). Ordering requirements otherwise bind the CONSUMPTION
  boundary, not the construction boundary.
- Copy-aside REDs must break PRODUCTION behaviour. A RED that mutates a newly-added test value
  and watches the new assertion fail is circular and has already fooled two waves here.
- Prefer the simplest strict check: unhashed field-tuple comparison (it names WHICH field
  drifted) or a distinct type, over a digest. Hashing earns its place only where the value set is
  too large to compare directly or must cross a serialization boundary.

GATE RULE (from a controller regression this cycle): if your atom adds or TIGHTENS any validation
rule, your gate is NOT the owning suites of edited files — it is every suite that CONSTRUCTS the
validated object, plus a full-tree added-red diff against a clean HEAD archive. The tree is clean
at 35dbc92, so there is no pre-existing red population to hide a new break inside; any new red is
yours.

Also standing: verification first; no ceremony beyond the frozen clauses (each guard names the
own-bug class it catches); derivation comments for new arithmetic; typed refusals over silent
repair; both PM2_INVARIANT_TIER tiers via git ls-files; operator mechanical + Python gates; honest
red ledger; no docs/LAYER-MAP.md edits; no RAG refresh tool.
OPERATOR DECISIONS 1 and 2 remain unresolved: any arm they gate lands INERT behind its stable
refusal (POWER_TIME_AUTHORITY_UNRESOLVED / AMBIENT_FIELD_AUTHORITY_UNRESOLVED). Do not select an
arm.
Notes: docs-private/reviews/2026-08-25-forcerail/d1-notes.md.
Terminal: !COMPLETE: fr-d1-r3 — <acceptance-clause results>

CONTRACTS ARE NOW FROZEN AT 10d2a00 — read your atom's CONTRACT APPENDIX in
FORCE-RAIL-REPRESENTATION-2026-08-24.md before anything else. Since your lane last ran, the design
gained: your carrier's executable contract (schema id, version literal, canonical byte grammar,
codec mapping, digest recipe + domain, ordering rule, exact typed field representations, refusal
tokens); the consumer-fence witnesses TRANSFERRED to X1 so your acceptance is what you can prove
LOCALLY; a shared materializable census definition; and refusal TOTALITY (every reachable
malformed input selects exactly one token).
POSE CANONICALIZATION CHANGED and this is the important one: canonical form is now the binary64
FIXED POINT of normalization (bounded, with a typed refusal if not reached), NOT the old
"correctly round once" rule — that rule was mathematically unverifiable on the wire and a norm
envelope was tried and refuted. Mint the fixed point; the wire checks q == normalize(q)
byte-exactly with NO tolerance, envelope, repair, or component search anywhere. The controller
independently verified convergence in 0-2 iterations and that 2x, 0.5x and even 1.0000000001x
rescalings all correctly fail the test.
Any DRAFT work your lane left uncommitted in the tree is yours to reconcile against the frozen
contract — read it, keep what is right, and say what you changed and why.
Everything else in this brief stands, including STOP-and-report if you hit a further contradiction.

```
