# Pinned prompt — `t743-exclusion-audit`

- source: `prompt-file`
- prompt-file: `/private/tmp/claude-501/-Users-simonrowland-Library-CloudStorage-Dropbox-Starship-Mission-Design-Regolith-Processing-regolith-pyrolysis-simulator/70f1d3c1-4b7e-4455-bf2d-31eabd2ee767/scratchpad/briefs/t743-selection.md` (EXISTS on disk)
- pinned_at: 2026-08-27T14:54:06.319466+00:00

```
# EXCLUSION AUDIT — does our validation battery only ever discard data in the direction that flatters the model?

Repo: /Users/simonrowland/Library/CloudStorage/Dropbox/Starship Mission Design/Regolith Processing/regolith-pyrolysis-simulator
Read AGENTS.md and CLAUDE.md (the pyrolysis mandate) first. The relevant clause: "silent suppression
of literature disagreement" is named as corruption, on a par with parameter-tuning.

## Why this audit exists

We are about to consider excluding a KEMS dataset (Apollo sample 12065) from the observations that
score our Na vapour-pressure rail, on the argument that its disagreement with us is an apparatus /
sample-split artifact rather than a model error. That may well be correct -- a separate worker is
attacking that argument on its own merits, and you should NOT re-litigate it.

Your question is structural and you can answer it without deciding whether 12065 is right:

  ★ ACROSS THE WHOLE VALIDATION BATTERY, IS THE EXCLUSION MACHINERY SYMMETRIC?

A principled filter removes bad evidence regardless of which way it cuts. A fitted filter -- one that
has been grown, one exclusion at a time, each locally defensible -- removes only evidence that makes
the model look bad. From the inside these are indistinguishable at any single site, which is exactly
why the audit has to be done over ALL of them at once. This is the only lens that can catch it.

## What to establish, with counts and file:line evidence

1. ENUMERATE EVERY EXCLUSION, SKIP, DOWNWEIGHT, GUARD OR CARVE-OUT that removes an observation, or a
   class of observations, from being scored against the engine. Start from
   simulator/diagnostic_helpers/extract_reproduction.py (it already contains at least a
   self-agreement guard and a source-priority mechanism), then sweep outward: the extract loader,
   data/literature/extracts/_source_priority.yaml, the comparison harness, the tests that pin
   coverage counts, and any per-observation status/flag fields in data/literature/extracts/*.yaml.
   Do not stop at the first mechanism you find; there are several and they compose.

2. FOR EACH ONE, DETERMINE ITS DIRECTION. For every excluded or skipped observation, would including
   it have made the engine's residual BIGGER or SMALLER? This is the core measurement of the audit,
   so compute it, do not estimate it: for each exclusion, actually score the observation both ways
   and report the residual delta. Where an exclusion covers a class, report the class's aggregate.

3. THE HEADLINE NUMBER: how many exclusions cut in the model-flattering direction, versus how many
   cut against the model, versus how many are direction-neutral? A battery where 100% of exclusions
   flatter the model is a finding on its own -- and note carefully that it is NOT automatically
   damning, because some exclusions are legitimately one-directional by construction (a
   self-agreement guard can only ever remove agreement, so it should REDUCE apparent quality, not
   improve it -- check that it actually does). Classify each exclusion as:
     STRUCTURAL   -- one-directional by its own logic, and pointing the honest way
     PRINCIPLED   -- stated criterion, applied before the residual was known, would fire either way
     POST-HOC     -- criterion discovered or refined AFTER seeing that this datum disagreed
   The POST-HOC count is what we most need to know. Use git history (git log -p, git blame) on the
   guard code and the extract files to establish WHEN each criterion was written relative to when the
   disagreement it excludes was first observed. That ordering is the evidence; assertions about
   intent are not.

4. THE COVERAGE ARITHMETIC. The battery currently reports on the order of 299 observations with 42
   comparable and 90 comparable points, the rest skipped. That is a large skip fraction. Break the
   skipped population down by REASON and report the table. Distinguish "skipped because the engine
   cannot yet produce a comparable quantity" (an honest gap, and a to-do) from "skipped because the
   observation was judged unsound" (an exclusion, and this audit's subject). If the skip reasons are
   not recorded in a machine-readable way, that itself is the finding -- an unauditable skip is
   indistinguishable from a fitted one.

5. THE ASYMMETRY TEST, which is the sharpest single check: has ANY observation ever been added,
   re-included, or promoted specifically BECAUSE it disagreed with the engine? Has any been excluded
   for a reason that made the engine look WORSE? Search the git history and docs-private/research/
   for instances. If the answer is zero in both directions across the battery's whole history, say
   so plainly with the evidence -- silence in one direction only is the signature we are hunting.

## Method notes

Use the repo venv (.venv/bin/python). Prefer counting with code over reading by eye; write a throwaway
script into your scratch area rather than eyeballing YAML. Do NOT modify any extract, pin, coefficient
or guard -- this is a read-and-count audit and its value depends on changing nothing.

Do not treat the number of exclusions as inherently bad, and do not pad the report with
recommendations. We want the census and the direction table. If the battery turns out to be clean and
symmetric, that is a genuinely useful result and should be reported as confidently as a problem would
be.

## Deliver

docs-private/reviews/2026-08-26-t743-adversarial/exclusion-audit.md, with the direction table and the
STRUCTURAL/PRINCIPLED/POST-HOC classification as the two headline artifacts. Return a TLDR with the
three counts, then COMPLETE: on the last non-empty line.

---

## ★ ADDED 2026-08-26 — a LIVE decision now depends on this audit, so answer it explicitly

A second controller (regolith-engine) has just established, from primary sources, that many of the
battery's anchor temperatures evaluate thermodynamic rows BELOW the temperature at which that
pure-composition liquid physically exists — i.e. against a metastable / supercooled liquid standard
state. Their per-row melting points: KAlSiO4 2033 K, KAlSi2O6 1959 K, NaAlSiO4 1799 K, KAlSi3O8
fully liquid only near 1803 K. Crossed against reference-anchor-results.csv, of 17 distinct anchor
temperatures: 1500/1522/1625/1750 K have FOUR such rows metastable; 1875-1956 K have TWO; 1961 and
2000 K have ONE; only 2125/2250/2500 K have none. Their write-up is
docs-private/research/2026-08-26-t697-na-family/fc87-sf04-k-audit.md — read it, it is primary-source
work and it is not a residual argument.

The sharp case is 1750 K: it sits ABOVE the pack's declared 1700 K floor, so the battery reports
fully in-domain and sets NO extrapolation mark, while four of nine rows are below their melting
points there. 1500-1625 K are already flagged by the floor and are therefore honest. **1750-2000 K
is a silent band.**

A decision is pending on whether to add a per-row physical-melting-point mark. That decision
interacts with your audit directly, and the interaction is the thing we need you to compute:

  ★ IF a per-row metastability mark is added, WHICH WAY DOES IT CUT? Score the affected
    observations and report whether marking them would make the engine's aggregate residual
    LOOK BETTER or LOOK WORSE.

This is the cleanest available test of the whole question you are auditing, because the mark is
being considered on PHYSICAL grounds that have nothing to do with agreement. Two outcomes, opposite
meanings:
  - marking makes the engine look WORSE (the metastable rows are ones we currently AGREE with) ->
    strong positive evidence the battery's machinery is not fitted, since here is a principled
    change that costs us
  - marking makes the engine look BETTER (the metastable rows are disproportionately ones we
    DISAGREE with) -> the mark is entangled with the residual and must be adopted with far more
    care, because a physically-motivated filter that happens to remove exactly our worst points is
    the hardest case to distinguish from a fitted one

Report the residual delta both ways, per affected anchor temperature, and say which outcome you got.
Do NOT recommend whether to adopt the mark -- that is the controller's call. Compute the direction.

Note this is the SAME structural class as the metasilicate-ceiling finding already logged as b-240:
a real physical limitation that the declared envelope cannot structurally express, and therefore
cannot mark. Count how many such unmarkable-limitation cases the battery already contains; that
count belongs in your census as its own row, distinct from exclusions, because an unmarked
limitation is not an exclusion -- it is worse, since nothing records that it happened.

```
