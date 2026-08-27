# Pinned prompt — `t748-stage-pressures`

- source: `prompt-file`
- prompt-file: `/private/tmp/claude-501/-Users-simonrowland-Library-CloudStorage-Dropbox-Starship-Mission-Design-Regolith-Processing-regolith-pyrolysis-simulator/70f1d3c1-4b7e-4455-bf2d-31eabd2ee767/scratchpad/t748.prompt` (EXISTS on disk)
- pinned_at: 2026-08-27T14:54:06.319466+00:00

```
Derive the per-species STAGE partial pressure in this project's condensation train, then settle whether each condenser actually captures its species. Repo root: /Users/simonrowland/Repos/rps-b236 (branch work-b236). Read AGENTS.md and CLAUDE.md at that root FIRST — CLAUDE.md is the physics mandate: maximum truth-seeking, first-principles accuracy, no tuning to taste.

READ THIS FIRST, it is half your job already done and it tells you where the difficulty is:
  /Users/simonrowland/Repos/rps-b236/docs-private/research/2026-08-26-condensation-grounding/findings.md
  /Users/simonrowland/Repos/rps-b236/docs-private/research/2026-08-26-wall-temperature-table/   (the GATED method + its script)

STATE OF PLAY. Eleven condensation-train routing setpoints (data/setpoints.yaml condensation_train.condensation_temperatures_C: Fe 1250, SiO 1050, CrO2 1250, Mg 580, Na 480, K 420, Ca 780, Mn 1000, Cr 1280, Al 1180, Ti 1500) are all uncited operator estimates — the data file says so in its own source strings. The controller computed dew points against them and found the result is DOMINATED by the assumed partial pressure:

  at 1 mbar        : K +88, Na -6, Ca -81, Al -83, Fe -525, Mn -224, Cr -311, Ti -656
  at 0.038 mbar    : K +189, Na +64, Ca +25, Al +129, Fe -237, Mn -28, Cr -69, Ti -337
  (delta = setpoint - computed dew point; NEGATIVE = setpoint below dew point = supersaturated = captures.
   POSITIVE = setpoint above dew point = too hot to condense on = passes through.)

Across 1.4 decades FOUR of eight computable species CHANGE SIGN on the capture question. Neither pressure is the answer: 1 mbar is a reference convention, and 0.038 mbar is ONE species' partial in ONE campaign applied uniformly, which is the same error at a different value.

★★ THE HARD PART, AND THE TRAP. Reading wall_species_partial_pressures_pa_by_segment out of a run is NOT sufficient. Those are partials on the PIPE SEGMENTS. A species condenses at its CONDENSER STAGE, which is a different location with its own pressure. Establishing the stage -> partial-pressure mapping IS the task. If you cannot derive it for a species, say so explicitly — an honest "not derivable, here is what blocks it" is worth far more than a plausible number, because a confident wrong pressure here produces a confident wrong verdict about whether the mandate's product classes are captured at all.

YOUR TASK.

STEP 1 — DERIVE. For each of the eleven species, the partial pressure it actually sees AT ITS OWN CONDENSER STAGE in a representative run. Show the call chain from run to number. Mark clearly: derived / assumed / not-derivable. Run the simulator; do not reason about it from the source alone.

STEP 2 — RE-INVERT at those pressures. ★ REUSE THE GATED METHOD, do not rebuild it: docs-private/research/2026-08-26-wall-temperature-table/compute_wall_table.py. Its invert_wall_T returns a DICT with T_K and status (not a float) and its DATA path must be rebound to this worktree's data/vapor_pressures.yaml. Reproduce the published SiO figure (1409.5 vs 1409 C at 0.1 ubar, pO2 1e-9 bar) as your own method gate before trusting a single new number; if it does not reproduce, stop and report that.

STEP 3 — SETTLE b-239 (the K finding) and say which of Na/Ca/Al join it, if any, at the DERIVED pressures. State the capture verdict per species with its pressure attached, because a verdict without its pressure is meaningless.

STEP 4 — SETTLE THE ORDERING QUESTION, which is currently degenerate. At a COMMON pressure it is vacuous: Fe's T_cond at 1 mbar is 1775.1 C, IDENTICAL to its t-736 bake-off wall minimum, because they are the same dew point. The ordering "condenser must sit below the duct wall feeding it" only has content when wall and condenser see DIFFERENT partial pressures — which is what buffer-gas dilution provides, and which b-232 found is ABSENT from the deposition path. So either answer it with the two actual pressures, or state plainly that the model cannot currently distinguish them and why.

CONSTRAINTS. Do NOT edit data/setpoints.yaml — a deliberate offset for capture efficiency or tap purity is legitimate engineering; the point is to know its size. Do NOT edit simulator/ or tests/. Do NOT run git checkout, git stash, git commit or git add. Show derivations: premise, algebra, unit check, sanity check.

Write your report to /Users/simonrowland/Repos/rps-b236/docs-private/research/2026-08-26-condensation-grounding/step1-stage-pressures.md, leading with the eleven-row table: species, stage partial pressure, derived/assumed/not-derivable, dew point at that pressure, setpoint, delta, capture verdict.
End your final message with COMPLETE: and a one-line count of derived / assumed / not-derivable, plus the capture verdict for K.

```
