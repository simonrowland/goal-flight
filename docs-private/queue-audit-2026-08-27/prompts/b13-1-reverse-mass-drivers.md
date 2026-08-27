# Pinned prompt — `b13-1-reverse-mass-drivers`

- source: `prompt-file`
- prompt-file: `/tmp/goal-flight-501/dispatch/b13-1-reverse-mass-drivers.assembled.prompt` (EXISTS on disk)
- note: prompt_file taken from ledger.prompt_path
- pinned_at: 2026-08-27T14:54:06.319466+00:00

```
You have a steer mailbox at `$GOALFLIGHT_STEER_FILE`. Read it AT THE TOP OF EACH ITERATION and IMMEDIATELY BEFORE ANY git commit/push. Incorporate new messages into your plan; ack each with `!STEER-ACK: <seq>` on its own line; a steer may redirect or HALT you — honor it. If `$GOALFLIGHT_DISPATCH_SCRIPT` is set and you have nothing to do until the controller answers, use `python3 "$GOALFLIGHT_DISPATCH_SCRIPT" steer "$GOALFLIGHT_DISPATCH_ID" --wait --question-kind USER-NEED --timeout-secs <seconds> '<question>'` (or USER-CONFIRM) to emit the question and wait under a separate bounded deadline.

Your FULL original brief is at `$GOALFLIGHT_PROMPT_FILE`. Re-read it after any internal compaction/summarization, at the start of each long-run goal-loop iteration, and before final commit/exit; the disk file is authoritative over summarized memory.

Worker execution contract:
- Use your available tools to actually perform the requested filesystem, shell, research, or analysis actions before answering. Do not only plan, summarize, or describe commands.
- For successful completion, emit a final line outside any Markdown fence in this exact shape supplied by the dispatch-specific identity contract.
- The `!COMPLETE:` line must be the last non-empty line of your output. Do not print anything after it.
- Legacy unprefixed marker lines remain accepted; new emissions use the `!` prefix.

Terminal evidence identity contract:
- Every terminal marker payload starts with the exact dispatch id `b13-1-reverse-mass-drivers`.
- Successful final shape: `!COMPLETE: b13-1-reverse-mass-drivers — <summary>`.
- Use the same id prefix for READY, RESULT, FAILED, USER-NEED, USER-CONFIRM, or BLOCKED. A generic or foreign marker is ignored.

# B13 chunk 1 — reverse mass-driver family (t-261 / BATCH-PLAN B13)

## MUST-READ FIRST (non-negotiable, in this order)
1. `AGENTS.md` (repo root) — project invariants and file map.
2. **SIMONS_PM2_MANDATE** — the north star. Read the pinned one; do not write a new mandate.
3. **RF-APWP-COUPLING-FRAMEWORK-v2** — the 4 FOMs including FOM3 saturation/overpower and the
   optimizer-exploit-proof requirement. Lift per-mechanism, never universally.
4. `docs-private/research/2026-07-17-pa-wiring-prep/BATCH-PLAN.md` §B13 (line 301).

## Scope — 5 rows ONLY, one parent plus its four folds
- `reverse_coil_mass_driver` (`t-147`) — the parent
- `sequential_coil` (`t-148`), `mpd_pit_reverse` (`t-149`), `reverse_mpd_pit` (`t-151`),
  `reverse_inductive_mass_driver` (`t-152`) — all folded under the parent

B13 has 26 rows total. The other two families (the `paper_c_dhg` closure set, and the
electrostatic-decelerator set `t-166`–`t-178`) are **OUT OF SCOPE** for this dispatch and will be
separate chunks. Do not start them. `t-164`/`t-165` additionally carry `gates: B15` and are not
available to anyone yet.

## Gates — checked for you, do not re-litigate
Gate line (A-9): **q-018 + B06 (force-interface) + B01Δ**, plus **B11b for load rows**.
- q-018 **done** (operator DECIDED 2026-07-20)
- B06 = t-250 keystone **done**
- B01Δ = t-266 **done** (landed `7478ac3`)
- **B11b (t-264) is BLOCKED** on t-259. **Therefore: any row in your set that requires load
  enrollment is OUT OF SCOPE.** Identify whether any of the five is load-posture BEFORE
  implementing it, and if one is, say so in your return and leave it — do not enroll it yourself
  and do not invent a load path.

## THE PHYSICS TRAP — this is the whole point of the chunk
B13's acceptance says, verbatim: **"real momentum sink; no sign-reversed forward-thruster
shortcut."** These are REVERSE drivers — decelerators. The cheap, plausible, wrong implementation
is to take a forward thruster's model and negate the vector.

**That is not deceleration; it is a sign flip that conserves nothing and invents a momentum sink
out of nothing.** For each of the five rows you must be able to answer, in the code and in a
comment: *what body receives the equal and opposite momentum, and where does the energy come from?*
A reverse mass driver expels reaction mass — that mass is a real reservoir with a real cost.
If a row cannot name its sink, it is NOT ready to register; report it as unresolved and fail closed.
Make your reviewers prove the sink exists per row rather than checking the sign convention.

## Requirements
- **Derivations in comments.** Non-obvious arithmetic gets premise → algebra → unit/dimension check
  → sanity check (a known value or limiting case). A reviewer must be able to reconstruct WHY a
  number is what it is from the code alone. These rows are full of coil/grid/waveform arithmetic;
  this is not optional here.
- **Waveform Parseval / history / source-debit closure** per the acceptance.
- **Reacceleration, grid, and load losses** accounted — not assumed zero.
- **Knob hygiene**: every new free DOF gets bounded, costed, and inert-detected up front. An
  unbounded or costless knob is an optimizer exploit; a knob that changes nothing is dead weight.
- Pinned B02 before/after throughput and performance-contract checks; full contract/global gate.

## Environment
- Tests: `uv run --with numba` — NOT the kiln venv. The kinetic Boris JIT (~60x) only fires when
  numba is importable.
- **You are in an isolated worktree at `/private/tmp/pm2-b13/pm2` on branch
  `b13-1-reverse-mass-drivers`.** Its basename is `pm2` deliberately: the repo root IS the `pm2`
  package and a non-`pm2` basename breaks or silently mixes imports. Do not move or rename it.
- **Another worker is live in the main checkout** on B11a (magnetopause `T(kr)` kernel extraction),
  editing `channels/propagation.py`, `rf/coupling/feature_parity_inventory.py`, `rf/link_budget.py`.
  Your anchors (`harvest/*`, `evaluators/harvest.py`, `rf/coupling/{ledger,mechanism_model,proof_gate,registry}.py`)
  are disjoint from those. **If your work starts pulling in any file on that list, STOP and report
  it** rather than editing it — that is a collision, not a merge conflict to resolve.

## Self-review to convergence — do not skip, do not cap
**At least 3 concern-diverse reviews, REPEATED to convergence**, not once. Distinct lenses:
(a) **momentum/energy conservation per row** — the sink question above, this is the load-bearing one;
(b) optimizer-exploit / unbounded-or-inert free DOF;
(c) registration-contract correctness (M104/E105/D109 cells, no default KEEP, fail-closed honesty).
Fold every finding into the same changeset. One review pass is wholly inadequate.

## Landing
Commit on `b13-1-reverse-mass-drivers` with **explicit pathspecs** (`git commit <files>`, never a
bare `git commit`). **Do NOT merge to main and do NOT push.** Report branch + commit SHA.
Do not leave a dirty worktree.

## Report back
Branch + SHA; which of the five rows you registered; **for each, the named momentum sink and where
the energy comes from**; any row you found to be load-posture and therefore left; every value you
recorded as UNPROVEN with its bounds; and anything you chose not to do and why.

```
