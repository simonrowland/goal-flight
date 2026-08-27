# Pinned prompt — `t697-dunite`

- source: `inline`
- inline prompt present: yes
- pinned_at: 2026-08-27T14:54:06.319466+00:00

```
DIAGNOSE AN UNEXPLAINED RESIDUAL. Repo: /Users/simonrowland/Library/CloudStorage/Dropbox/Starship Mission Design/Regolith Processing/regolith-pyrolysis-simulator.

ORIENTATION FIRST (both gitignored - read from the working tree, not git): AGENTS.md and CLAUDE.md at the repo root. Read docs-private/research/2026-08-26-t697-na-family/findings.md for context, and treat its conclusions as claims to test rather than premises to build on.

THE FACT TO EXPLAIN. In benchmarks/results/melt_activity/reference-anchor-results.csv at 2000 K, the SF04-dun sheet records imcc_residual_dex = -1.0132 for Na and -1.4649 for K, while VapoRock scores -0.1673 and +0.7921 on the same reference numbers. reference_difference_dex is essentially zero, so both engines were scored against the same references and the gap is ours.

A mechanism that accounts for tholeiite and komatiite does NOT account for dunite. Suppressing the four nu(Na2O)=0.5 Na-aluminosilicate complexes moves log10 a(Na2O) by +1.766 dex on tholeiite but only +0.405 on dunite, and on dunite the nu=1.0 group actually dominates at +0.725. Even suppressing every Na complex in the pack could not close dunite 1.013 dex. Something else is operating there and nobody has identified it.

Dunite composition (data/melt_activity/basalt-bench-set-v1.yaml, sf04_dunite, wt%): SiO2 40.52, MgO 43.54, FeO 13.72, CaO 0.81, Al2O3 0.81, TiO2 0.20, Na2O 0.30, K2O 0.10.

YOUR JOB is to find what it is. Some directions worth examining, not a checklist and not a claim that any of them is right: dunite is extraordinarily MgO-rich and alkali-poor relative to anything the model was validated on, so ask whether it is inside any declared envelope at all and what the model does when it is not; ask whether the Mg complexes dominate the free-parent balance in a way that indirectly starves the alkalis; ask whether the trace alkali levels put the solver somewhere numerically awkward; ask whether the gas-side carrier path rather than the melt side carries the error. Derive your own candidates from the code and the numbers - do not limit yourself to that list.

USEFUL ENTRY POINTS: simulator/melt_backend/imcc_sf04/ holds adapter.py, kernel.py and gas.py. The adapter evaluate() accepts allow_extrapolation and allow_out_of_envelope and returns parent_activity, parent_gamma, parent_x_star, species_x and a labels block that records domain status. Two working probe scripts sit in docs-private/research/2026-08-26-t697-na-family/ and you can copy their pattern; label_research_datapack is the sanctioned way to build a counterfactual pack, because the published loader correctly refuses to let a modified pack wear published identity. Use .venv/bin/python, and wrap any pytest in an external wall timeout.

METHOD REQUIREMENTS:
- Your null hypothesis is that dunite behaves exactly like the other sheets and the residual has the same cause. Try hard to confirm that first; if it survives, say so.
- Every candidate mechanism must be tested numerically, not argued. Report the number it moves and by how much.
- A negative result is a real result. If you eliminate four candidates and find nothing, that report is worth having - it tells the next person where not to look. Do NOT manufacture a conclusion.
- Do not propose coefficient or data edits. This is diagnosis.

DELIVERABLE: docs-private/research/2026-08-26-t697-na-family/dunite-residual.md, with what you tested, what each moved, what you ruled out and on what evidence, and your best remaining hypothesis with the experiment that would settle it.

Finish your final message with the single line COMPLETE: dunite-residual
```
