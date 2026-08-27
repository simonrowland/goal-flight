# Pinned prompt — `t292-relativistic-gathered-mass`

- source: `prompt-file`
- prompt-file: `/tmp/goal-flight-501/dispatch/t292-relativistic-gathered-mass.assembled.prompt` (EXISTS on disk)
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
- Every terminal marker payload starts with the exact dispatch id `t292-relativistic-gathered-mass`.
- Successful final shape: `!COMPLETE: t292-relativistic-gathered-mass — <summary>`.
- Use the same id prefix for READY, RESULT, FAILED, USER-NEED, USER-CONFIRM, or BLOCKED. A generic or foreign marker is ignored.

# t-292 — relativistic gathered-mass energization adjudication (operator-commissioned 2026-07-22)

## MUST-READ FIRST (non-negotiable)
1. `AGENTS.md` (repo root).
2. **SIMONS_PM2_MANDATE** — the north star. Read the pinned one; do not write a new mandate.
3. **RF-APWP-COUPLING-FRAMEWORK-v2** — the 4 FOMs incl. FOM3 saturation/overpower.
4. **t-287's adjudication** — this item explicitly extends it and inherits its typing layer.
   t-287 is DONE; its two-reservoir typing is the substrate you compose through.

## The commission — read the store item t-292 verbatim, it IS the spec
`goalflight_task.py show t-292`. It states three operator hypotheses, a controller null, a required
handling list, and a two-way gate. Do not paraphrase it into something smaller.

## PREMISE ARITHMETIC — I checked it so you do not have to, and it holds
The operator's stated figures are correct. KE per unit captured mass is `(γ−1)c²`:
- β = 0.65 → γ = 1.315903 → **(γ−1) = 0.315903 c²/kg** (operator said 0.32 ✓)
- β = 0.87 → γ = 2.028185 → **(γ−1) = 1.028185 c²/kg** (operator said 1.0 ✓)

**The crossover the commission asks you to "derive honestly" has a CLOSED FORM.** (γ−1) = 1 exactly
when γ = 2, i.e. **β = √3/2 = 0.8660254**. At that speed the kinetic energy of captured mass per kg
EQUALS the full annihilation yield mc². So "antimatter-class fuel above 0.65c" is really "reaches
parity with annihilation at β = √3/2, and exceeds it above". State it that way — exactly, not as
0.87c — and give the derivation, not just the result.
**This does NOT settle hypothesis (2).** Energy *content* is not energy *availability*: the open
question is what fraction is extractable as directed exhaust after the acquisition drag is paid,
and at what cost in the two-reservoir ledger. Parity with mc² is a ceiling, not a yield.

## ATTACK THE NULL AS HARD AS THE HYPOTHESES
The commission carries a CONTROLLER NULL against hypothesis (3): pure harvest→photon is
asymptotically **drag-neutral, not thrust-positive** (windmill extraction costs D = P/v_rel via
dE/dp = v; photons return P/c; 1/c < 1/v_rel always; full capture gives T/D → η(γ−1)c/(γv) → η from
below). **This is two-way gated: either the hypotheses or the null may die.**
The failure mode I want foreclosed: a deriver finds the hypotheses more interesting and lets the
null through on a sketch. Give the null the same rigour you give hypothesis (1). If the null
survives, the honest result is the photon recycler as an **asymptotic drag-eraser** enabling
near-free mass collection at high γ, with acceleration coming from energized-gathered-mass exhaust
— which is a real and valuable result, not a negative one. Do not treat killing the null as success.

## Standing pm2 rulings you must not re-litigate
- **Relativistic everywhere, one helper.** The relativistic form wins on every surface and
  `relativity.py` is the SINGLE SOURCE. Do not hand-roll γ. (For scale: γ²−1 is 9.890% at 0.3c.)
- **The turbofan law is LOCKED.** `T/D = √(η·BR)` and `thrust = min(√(η·BR)·D, √(2P·ṁ_b))` are
  SETTLED in Paper-P; the from-rest bypass-KE form `√(2P·ṁ_b)` is CORRECT and the `ṁ_core·2v` cap
  objection was refuted 2026-06-14. **Read the locked paper before touching turbofan-adjacent
  arithmetic** — this item's harvest/drag ratios sit right next to it. If your derivation appears
  to contradict the locked law, that is a finding to REPORT, not a licence to re-derive it.
- **Analytical-model epistemology.** DERIVED standing is NEVER gated on PIC or literature.
  Comparators are prediction tests. **DIVERGENT means investigate, never fit.**
- **Zero fitted coefficients** (the commission says so explicitly). A coefficient you cannot derive
  is recorded as UNPROVEN with bounds, never tuned to make a limb close.

## Method
Per the commission: 3-independent-method + cross-review as in t-287 if resources allow, else **one
deriver + adversarial verify**. Take the latter unless the derivation splits naturally. Either way
the adversarial pass is mandatory and must attack the null and the hypotheses separately.

## Rigour requirements
- **Derivations in full**: premise → algebra → unit/dimension check → sanity check (a known value
  or limiting case) for every non-obvious step. A reader must reconstruct WHY each number is what
  it is without re-deriving it. Check limits explicitly: β→0, β→1, η→1, full-capture vs
  partial-deceleration.
- **Decompose aggregates.** If a total moves, show WHICH component moved and by what predicted
  factor at its own case speed. A total that moves "about right" can hide two compensating errors
  or a frozen component.
- Handle everything the commission lists: relativistic aberration of harvested flux, anisotropic
  emission, partial-deceleration windmill vs full capture, η chains, composition through the typed
  two-reservoir ledger with BOTH channels simultaneously, the full cycle gather→bank→energize→expel
  priced end-to-end against the rocket equation (the q-drive subexponential comparison), and the
  >0.65c regime placed in the mission band.

## Landing
You are in an isolated worktree `/private/tmp/pm2-t292/pm2` on branch
`t292-relativistic-gathered-mass`. Basename is `pm2` deliberately — the repo root IS the package and
a non-`pm2` basename breaks imports. Write the adjudication to
`docs-private/research/2026-08-27-t292-relativistic-gathered-mass/findings.md`.
Commit with explicit pathspecs. **Do NOT merge to main, do NOT push.** Report branch + SHA.
Two other workers are live on unrelated code surfaces; you should not need to touch code at all
beyond reading. If you find yourself editing `rf/`, `waves/`, `harvest/`, `config.py` or
`dof_spec.py`, STOP and report why.

## Report back
Branch + SHA; the verdict on EACH of the three hypotheses AND on the null, separately, each with its
standing (DERIVED / UNPROVEN-with-bounds / REFUTED); the exact crossover derivation; every limit you
checked; every value recorded UNPROVEN with bounds; and anything you could not close and why.

```
