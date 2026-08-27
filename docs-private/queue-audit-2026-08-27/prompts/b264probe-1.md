# Pinned prompt — `b264probe-1`

- source: `prompt-file`
- prompt-file: `/private/tmp/claude-501/-Users-simonrowland-Repos-pm2/d9989eb0-0cdd-4ccc-be80-cc86a4d2a55f/scratchpad/layermap-harvest.md` (EXISTS on disk)
- pinned_at: 2026-08-27T14:54:06.319466+00:00

```
# layermap-harvest — collect every owed LAYER-MAP declaration-need (t-763) — id layermap-harvest

Repo /Users/simonrowland/Repos/pm2, HEAD 590b1ae, tree CLEAN. GOAL-LOOP apply; NO stage/commit/push.
The repo root IS the `pm2` package. This repo's pytest has NO `--timeout` plugin.

## THIS IS A HARVEST, NOT AN EDIT
**You must NOT edit `docs/LAYER-MAP.md`.** That rule is why this backlog exists and it still binds:
LAYER-MAP is declared canon, changed deliberately in a credit-rich window, never by accretion. The
controller lands every map edit. Your product is a single consolidated, verified, deduplicated LIST
that the controller can land in one sitting. A worker who "helpfully" edits the map has destroyed the
one property the rule protects.

## WHY THIS IS OWED
Every worker brief this arc said "do NOT edit LAYER-MAP — state the declaration-need in your notes;
the controller lands it." The workers complied. The controller never did the pass. So the needs are
scattered across review notes, and nobody can currently answer "what does the map still owe?"

## WHERE THEY ARE (t-763 names these; VERIFY rather than trust the list)
- `docs-private/reviews/2026-08-24-ceremony-recheck/pb-laws-notes.md`
- `docs-private/reviews/2026-08-24-forcerail/x0-notes.md`, `x0-design-contract.md`, `x2a-notes.md`
- `docs-private/reviews/2026-08-25-forcerail/e1-notes.md`, `i1-notes.md`
- the t745 note asking to register the v4.1 PANCAKE current-capacity authority/consumer edge
- the t745-fix note asking to declare the config-carried specific-ionisation-energy sidecar and the
  `ionisation_W` / `pancake_cryo_W` shared steady lane
- t-763's own appended note: (1) `UnresolvedDerivedProvenanceV1` and the UNRESOLVED DERIVED census-row
  variant; (2) the completeness predicate now including the enumerated U set, i.e.
  `C == canonicalize(D union I union G union U union M)` — from e1-notes Round-6 "Declaration-needs".

**The list above is a starting point and is probably incomplete.** Sweep `docs-private/reviews/` for
the phrases workers actually used — "declaration-need", "Declaration-need", "LAYER-MAP", "layer
proposal", "do not edit the map", "controller lands" — and also sweep SOURCE files, because several
modules carry the need in their own docstring instead (`tube_reducer.py`,
`rf/coupling/intercept_bearing.py` and `rf/coupling/ladder_epsilon_ledger.py` each do; find the rest).

## WHAT TO PRODUCE, PER NEED
One row each, and every field verified against the CURRENT tree rather than copied from the note:
1. **Subject** — the exact module path, or the exact edge (`source` -> `target`), or the named type.
2. **What the note asked for**, quoted or tightly paraphrased, with `file:line`.
3. **Proposed layer or map change**, with the ONE-LINE reason. If the note proposed a layer, say
   whether its module-level imports still support that layer TODAY.
4. **STILL OWED / ALREADY LANDED / SUPERSEDED / WRONG.** This is the highest-value column. Several
   of these are months old and the tree has moved. `5b2588d` just classified the whole nine-module
   force-rail family at L1 and moved `OptimizerRole` to a new L0 `optimizer_roles.py`, so any
   force-rail or `OptimizerRole` declaration-need is very likely ALREADY LANDED — check, do not
   assume in either direction.
5. **Conflicts.** If two notes ask for incompatible placements, say so explicitly and do not pick a
   winner; that is a controller decision.

## HOW TO CHECK "does this placement still hold"
For a proposed layer, list the module's MODULE-LEVEL `pm2` imports (direct children of the module
body — NOT imports inside functions, which are deliberate cycle-breakers here) and check every one
is at or below the proposed layer, and that every importer of the module is at or above it. An AST
walk is the honest way; a grep will conflate lazy imports with module-level ones and give you a
wrong answer. `tools/architecture_scoreboard.py` already parses LAYER-MAP into a layer mapping —
read it and reuse it rather than re-deriving the table by hand.

## STANDING RULES
Verification first — every claim above, including t-763's own file list, is a hypothesis. No
ceremony: nothing gets proposed that cannot name the failure it prevents. pm2 and kiln are the only
consumers, on the operator's own machines — own-bugs and optimizer-valid inputs are the threat model.
Behaviour must never be selected by a NAME. Typed refusals over silent repair. Never extend a frozen
closed set. No RAG refresh tool.
**SEARCH TOOLING:** `grep -r` from the repo root SILENTLY skips gitignored paths — which includes ALL
of `docs-private/`, i.e. EXACTLY where every one of these notes lives. Measured: 9 hits under
`command grep` vs 1 under the shim. Use `command grep -r` or a Python walk. If you use the shim you
will report an empty backlog and be confidently wrong.
**b-236:** emit periodic `STATUS:` lines during anything long — the watcher reaps on event idle and
the give-up path SIGKILLs.

## GATES
You are changing no code, so there is nothing to gate beyond your own reading. Do NOT run the
construction set. If you want to confirm a placement, run `tools/architecture_scoreboard.py` and read
its Q9 output. Note for context: `tests/test_architecture_scoreboard_certres.py::test_current_tree_has_no_exact_match_new_q9_violation`
is RED at HEAD and is NOT yours — the grandfather list in `docs/Q9-BASELINE.json` is keyed by line
number and two entries drifted by four lines.

## NOTES
`docs-private/reviews/2026-08-26-layermap/layermap-harvest-notes.md`: the table described above, one
row per need, ordered STILL OWED first; then a short section listing anything you found that the
t-763 list did NOT name; then the conflicts.
Terminal: !COMPLETE: layermap-harvest — <N still owed, N already landed, N superseded, N conflicts>

```
