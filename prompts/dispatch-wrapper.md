# Dispatch Wrapper — the 5-layer briefing

When the controller dispatches an Executor (or Planner) subagent, raw goal
text from the queue (600–1200 chars) is not enough. Field-validated practice:
real dispatches that produce clean first-shot completions wrap the goal text
in **five distinct context layers**. Render this checklist into every
executor dispatch.

The audit substrate: 55 Agent dispatches across goals #4–#10 + milestone
cluster fixes + planning subagents. Dispatch size 6–11 KB. Goal text alone
was always insufficient.

## Layer 0 — Base-verification pre-flight (MANDATORY for worktree-isolated dispatches)

**Failure mode this catches**: the Agent tool's `isolation: "worktree"` creates a worktree branched off the controller's current working-directory HEAD, NOT necessarily off `main`. If the controller is running in a sibling worktree (e.g., `.claude/worktrees/<controller-workspace>/`) whose branch lags main, the executor will silently build against the wrong substrate and the work won't cherry-pick cleanly. **This bit us on goal #18 JSON-RUNNER-HARNESS** — the worktree branched off a base 33 commits behind main; the agent built against pre-kernel APIs; commits conflicted at cherry-pick time. Captured in `docs-private/future-work.md` (skill-side bug).

Include this assertion at the TOP of every dispatch prompt when worktree isolation is used:

```
PRE-FLIGHT (do this before reading the rest of the prompt):

1. Run: `git rev-parse HEAD` in your working directory.
2. Compare to the expected base: <PASTE EXPECTED SHA HERE — typically main HEAD>.
3. If they DO NOT match: STOP. Do not proceed with the goal. Report back:
   "Base mismatch: my worktree HEAD is <actual>, expected <expected>.
    Aborting — please dispatch me with the correct base."
4. If they match: continue with Layer 1 onwards.
```

The controller's responsibility before dispatching: capture `git rev-parse main` (or whatever the canonical base branch is) from the MAIN worktree path, NOT from controller `cwd` (which may be a sibling worktree). Paste that SHA into the prompt as the expected base.

## Layer 1 — Situational frame

State where main is, what just happened, and what this dispatch's role is in
the larger sequence. ~50–100 words.

**Worked example (goal #9 MAGEMIN-SHADOW-PARITY dispatch):**

> Main is at `322f57d` (goal #8 ALPHAMELTS-DIAGNOSTIC-GATE just landed:
> `AlphaMELTSProvider` is registered as kernel `ChemistryProvider` for
> `SILICATE_LIQUIDUS`/`SILICATE_EQUILIBRIUM`, authoritative + diagnostic-
> only). Your task: promote the already-scaffolded `MAGEMinShadowProvider`
> at `engines/magemin/provider.py` from "stub returns is_available()==False"
> to a kernel-registered SHADOW provider that runs alongside the AlphaMELTS
> authoritative path for the same two intents.

Why: the executor lands in your task cold; without orientation, it spends
its first 5 minutes (and ~5 KB of its context) re-deriving "where am I?"

## Layer 2 — Template-provider pointer

When the work mirrors an existing pattern, name the canonical example and
the differences. ~50–150 words.

**Worked example:**

> Your template is the MAGEMin provider at `engines/magemin/provider.py` —
> already implements shadow-mode kernel provider for the same two intents.
> Read it carefully. Don't slavishly copy (you're authoritative, not shadow),
> but follow its declared_accounts pattern, ControlAudit shape, intent
> capability set.

Why: prevents re-derivation. "Mirror this exact shape" is a much tighter
constraint than "implement a kernel provider."

## Layer 3 — File-path-and-line anchors

Concrete paths, commit hashes, class names, line numbers. No abstraction.
A flat list is fine.

**Worked example:**

> - Adapter at `simulator/melt_backend/alphamelts.py` (hardened by goal #1,
>   commit `153d8a7`).
> - Kernel intents enumerated at `simulator/chemistry/kernel/capabilities.py:
>   24-25`.
> - MAGEMin provider's intent set at `engines/magemin/provider.py:68` is the
>   shape to mirror.
> - Cluster B's `_dispatch_and_commit` helper landed at `simulator/core.py`
>   in commit `8ba7079`; you should adopt it at new call sites.

Why: the executor's Read calls are expensive; pre-pasting the navigation
saves ~3-5 round-trips of "where does X live?"

## Layer 4 — Environment caveats

Optional dependencies, install state, test-skip patterns, anything in the
environment the executor might assume incorrectly.

**Worked example:**

> AlphaMELTS optional dependency may not be importable in the test env. The
> PetThermoTools path may have it; the subprocess path is the alphamelts
> binary at `engines/alphamelts/alphamelts-app-2.3.1-macos-arm64/`. Tests
> should gracefully skip if neither is available (existing tests at
> `tests/test_alphamelts_backend.py` show the pattern with `pytest.skip`).

Why: failure to flag environment caveats produces dispatches that "look
right" but explode on first test run. The executor's fix path is much
longer if it has to discover the missing dep.

## Layer 5 — Goal-specific self-review specialization

Take `prompts/executor-self-review.md`'s seven abstract categories and
rewrite each with this goal's actual grep patterns, line numbers, and
project nouns. Don't paste the abstract version raw.

**Worked example (goal #10 VAPOROCK-AUTHORITY-PROMOTION):**

> §7 amended self-review (P0/P1/P2/P3):
> - **Authority enforcement** — only one authoritative for `VAPOR_PRESSURE`
>   at a time; registering both as authoritative raises (mirror MAGEMin
>   shadow's `register(shadow=False)` check).
> - **No silent fallback** — flag default is `false`; missing-VapoRock + no
>   flag = exception, NOT a return.
> - **commit_batch purity** — `VAPOR_PRESSURE` is a diagnostic intent (no
>   ledger_transition). `VapoRockProvider` mirrors `BuiltinVaporPressureProvider`'s
>   diagnostic-only return shape.
> - **declared_accounts** — `VapoRockProvider` declares the same accounts
>   as Builtin did (likely `{process.cleaned_melt}`).
> - **ControlAudit populated** — `VapoRockProvider` returns `ControlAudit`
>   per cluster A's pattern.
> - **Existing 6 builtin authoritative intents unchanged** — grep
>   `_build_chemistry_kernel` to verify the 6 other registrations still
>   authoritative.
> - **AST writer-purity** — `engines/vaporock/provider.py` does NOT import
>   `LedgerTransition` / `LedgerTransitionProposal` (it's diagnostic).

Why: the abstract self-review categories are correct but unactionable until
specialized. A category like "atom-balance gaps" means nothing to an
executor unless you tell them which file, which proposal, which element.

## Triviality bypass

For trivial single-file goals (likely LoC delta < 50, no new public surface,
no cross-module coupling): layers 1 + 5 alone suffice. Skip 2/3/4. Example:
a one-line bug fix dispatch doesn't need the full briefing.

## When the corpus exists — slice-to-layer mapping

If `docs-private/rag/` was built by init's corpus pipeline, the controller selects slices for layers 2–4 instead of hand-composing. Use this mapping:

| Layer | Source from corpus | Selection criterion |
|-------|--------------------|--------------------|
| **2 — Template-provider pointer** | One `patterns/<pattern>.md` slice | The pattern this chunk's executor should mirror (named in the goal's REFERENCE section or inferred from CHECKLIST item 1's verb-object). |
| **3 — File-path-and-line anchors** | `file-map.md` (or, if split, every `file-map/<dir>.md`) + relevant `binding-spec/<intent>.md` slices | The file-map content is universal context — if the project's file-map exceeds 800 words and was split per schema, paste ALL `file-map/<dir>.md` slices (split was a packaging concern, not a relevance filter). Plus zero-or-more `binding-spec/<intent>.md` slices matching the intents this chunk touches (named in the goal's SCOPE). |
| **4 — Environment caveats** | `verification.md` (or, if split, both `verification/tests.md` and `verification/grep-invariants.md`) + topic-filtered entries from `decisions.md` | Verification content is universal — paste either the single file or both halves of the split. `decisions.md` filter mechanism: controller scans the chronological entries' DECISION-short lines for keywords matching this chunk's SCOPE/REFERENCE area; pastes matching entries. The decisions template (`templates/rag-slice-decisions.md.tpl`) doesn't carry an explicit tag field; scan by date + decision-short text. |

**Split-slice contract**: when a slice splits per schema (file-map, verification, decisions-by-epoch), the dispatch always pastes the entirety of the split set — never a subset. The split is for word-budget management; relevance filtering happens at the slice-selection step (whether to paste this slice family at all), not within a split family.

**`invariants.md` is appended to EVERY dispatch as a tail** (Executor / Reviewer / Planner alike — universal precondition, independent of wrapper layer).

**`decisions.md` is also available to Reviewer and Planner dispatches** (in addition to its layer-4 role for Executors). Reviewers benefit because they should know what was deliberately rejected; Planners benefit because they should not re-open closed decisions. For Reviewer / Planner: paste topic-filtered `decisions.md` entries alongside layer 3 (file-anchors).

Layer 1 (situational frame) and Layer 5 (goal-specific self-review specialization) are STILL per-dispatch composition. The corpus never replaces them — those layers exist because they're inherently per-chunk and per-state.

If a slice the mapping calls for doesn't exist in the corpus (e.g., the `patterns/<X>.md` you want was skipped at init because no canonical implementation existed yet): fall back to hand-composing that one layer for this dispatch. Don't block the dispatch.

## Quality check: every dispatch when corpus exists

Before sending the dispatch, the controller should verify:
- Layer 2: did you paste a `patterns/*.md` slice? (Or note "no canonical pattern yet" if intentional.)
- Layer 3: did you paste `file-map.md` + at least one `binding-spec/*.md` slice (if any binding-spec slices exist)?
- Layer 4: did you paste `verification.md` + decisions filtered to this area?
- Tail: did you paste `invariants.md`?

If the corpus exists and the dispatch doesn't paste from it, the controller has regressed to hand-composition. Surface as a self-check; not load-bearing but flags drift.

## Dispatch-shape checklist

```
\goal <SLUG>

[Layer 1: situational frame — where main is, what this dispatch role is]

[Goal text from queue — SCOPE / CHECKLIST / ACCEPTANCE / FORBIDDEN]

[Layer 2: template-provider pointer — what canonical example to mirror]

[Layer 3: file-path-and-line anchors — flat list]

[Layer 4: environment caveats — optional deps, skip patterns]

[Layer 5: goal-specific self-review — §7 categories specialized to this
 goal's grep patterns + nouns + line numbers]

Report format: see prompts/executor-self-review.md.
Read worker-context.md (if exists) or AGENTS.md before starting.
```

Render this every time. The first-shot completion rate of dispatches owes
more to this wrapper than to the goal text itself.
