# Pinned prompt — `b234-mre-scope`

- source: `prompt-file`
- prompt-file: `/private/tmp/claude-501/-Users-simonrowland-Library-CloudStorage-Dropbox-Starship-Mission-Design-Regolith-Processing-regolith-pyrolysis-simulator/70f1d3c1-4b7e-4455-bf2d-31eabd2ee767/scratchpad/b234.prompt` (EXISTS on disk)
- pinned_at: 2026-08-27T14:54:06.319466+00:00

```
Scope a fail-open in the MRE charge accounting: find out whether it actually fires on any golden feedstock. Repo root: /Users/simonrowland/Repos/rps-b234 (branch work-b234, HEAD 236553f9). Read AGENTS.md and CLAUDE.md FIRST.

THE DEFECT (b-234). simulator/extraction.py around 2113-2118, inside the MRE interval commit:
    n_e = charge_electrons.get(oxide, ELECTRONS_PER_OXIDE.get(oxide, 2))
    total_charge_C += moles_ox * n_e * FARADAY
ELECTRONS_PER_OXIDE covers exactly twelve oxides (NiO, Na2O, K2O, FeO 2; Fe2O3, Cr2O3, Al2O3 6; MnO 2; SiO2, TiO2 4; MgO, CaO 2). The loop iterates lot.species_moles_for(...) over EVERY oxide in the cleaned-melt lot, so anything outside those twelve silently contributes moles_ox * 2 * FARADAY. That total sets _mre_effective_current_A, which gates C5 / MRE_BASELINE rung advancement.

Assuming n=2 for an unknown oxide is not a conservative default — it is a fabricated measurement in an accounting channel, and it is wrong in both directions (6 for sesquioxides, 4 for dioxides).

★ THE SAME EXPRESSION EXISTS IN simulator/electrolysis.py AND WAS CORRECTLY FIXED THERE BY RAISING. DO NOT COPY THAT FIX HERE. There the oxide is CALLER-CHOSEN so an unlisted one is a programming error (missing input, refuse). Here it comes from the MELT INVENTORY, which legitimately carries species outside the table — P2O5 is ratified at 0.5 wt%, plus traces — so raising would break valid runs. The two sites read identically and are not the same; that is why this is filed separately.

YOUR TASK IS SCOPE FIRST, FIX SECOND.
1. ★ ENUMERATE WHICH OXIDES ACTUALLY REACH THIS LOOP on the golden feedstocks — lunar_eac_1a, lunar_mare_low_ti, mars_basalt, ci_carbonaceous_chondrite. Run it; do not reason from the YAML. If every oxide on every golden feedstock is inside the twelve, the fix is golden-NEUTRAL and cheap. If any golden feedstock carries an unlisted oxide, the fix MOVES NUMBERS and belongs in the gated regrind batch. That determination is the main deliverable and it decides everything downstream.
2. QUANTIFY THE ERROR where it fires: for each unlisted oxide found, how much fabricated charge, and does it move _mre_effective_current_A across either rung threshold (C5_LIMITED_MRE_CURRENT_A * 0.05, or 3000.0 * 0.05)? A fabrication that never crosses a threshold is a different severity from one that flips a rung.
3. PROPOSE THE FIX SHAPE. The preferred direction: EXCLUDE unknown oxides from total_charge_C and REPORT the exclusion, the way thermal_train.py reports excluded_species with a typed reason rather than a silent zero. The electron count is a MISSING INPUT and must not be guessed — but the surrounding computation stays meaningful over the species we do know. Whatever is chosen, the excluded inventory must be VISIBLE in the report, neither silently dropped nor silently assumed.
4. Apply ONLY if step 1 proves golden-neutral. Otherwise write the proposal.

TEST: cd /Users/simonrowland/Repos/rps-b234 && "/Users/simonrowland/Library/CloudStorage/Dropbox/Starship Mission Design/Regolith Processing/regolith-pyrolysis-simulator/.venv/bin/python" -m pytest tests/ -q -p no:randomly -n0 -k "mre or extraction or electrolysis"
Never pipe pytest through head or tail. Do NOT run git checkout, git stash, git commit or git add.
Report to /Users/simonrowland/Repos/rps-b234/docs-private/research/2026-08-26-b234/findings.md, leading with the per-feedstock oxide enumeration.
End with COMPLETE: and one line: golden-neutral or golden-affecting, plus the unlisted oxides found.

```
