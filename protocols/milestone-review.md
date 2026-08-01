# Milestone Review Protocol

Milestone-scale review flights at default cadence, before push, or on
`[milestone]` queue chunks. **Separate protocol** from per-chunk pre-commit review
(`protocols/chunk-review.md`).

## When — mandatory cadence, NOT optional (the most-forgotten review layer)

Three triggers, **mandatory at each** — milestone sweeps are routinely forgotten, so treat
them as a controller-enforced gate, not a nicety:
- **K-commit cadence** — default K=5 commit-worthy chunks since the last clean
  milestone sweep unless the active plan configures another positive K.
- **`[milestone]`-tagged chunks** — run the sweep as soon as that chunk lands.
- **Before any push** — if the cadence state has not been swept clean for the
  outgoing delta, run and record the sweep before publish.

`goalflight_status.py` surfaces the forcing nudge in routine status from
commit-count and `[milestone]` tag state:
`chunks since last milestone sweep = M (sweep due at K)`. It does not infer push
intent and does not block dispatch/drain by itself. Record a clean sweep with
`goalflight_status.py --record-milestone-sweep` after the review converges; the
marker lives under the Goal Flight state dir. When the chunk count reaches K, a
`[milestone]` chunk lands, or push is next, the sweep is **DUE**. A due sweep is
an **open liability, not a clean state** (same rule as an unswept bug class): do
not dispatch new implementation chunks or push while due. The controller enforces
that obligation by checking the status nudge and this protocol before launch or
publish; "I'll do it later" is a skip. The sweep is an aggressive
concern-diverse flight with at least two concern-diverse reviewers/lenses as the
floor: whole-delta walk, cross-lane-seam pass, and backwards look over every
chunk landed since the last milestone.

## Seams are the point of a milestone sweep — brief for them explicitly

A milestone sweep exists to catch what per-chunk review is **structurally unable
to see**. Per-chunk review is scoped to one chunk's diff, so it is blind by
construction to defects living BETWEEN chunks. Field evidence from a multi-chunk
milestone: per-chunk output graded well and nearly every escaped defect was
cross-chunk. **More reviews of the same shape do not help — the scope is what
misses, not the effort.** A sweep that runs another per-chunk-shaped pass has
spent the tokens and not looked where the defects are.

The full doctrine, including the integrated-tip gate, is in
`protocols/chunk-review.md` §Seams. Both of its gates are **standing
requirements at a milestone**, not optional extras:

1. **Integrated-tip gate** — run the full gate against all chunks landed
   together, never against each chunk in isolation. A suite that passed
   per-chunk and fails integrated **is** the seam; that failure is the finding.
2. **Cross-chunk pass** — one review whose unit is the milestone. Give it the
   chunk list and the shared surfaces, not the concatenated diffs. The question
   is consistency, not correctness-in-detail.

### Name these in the sweep brief — do not hope a reviewer looks

Reviewers find what they are pointed at. Every milestone brief should ask, in
these words:

- **Where do two chunks answer the same question differently?** A shared schema
  each side implements slightly differently; two chunks that each decide a
  default and disagree; a catalog entry nobody owns.
- **Which invariant does every chunk half-maintain?** An invariant upheld at each
  site individually and by nothing at the chokepoint is upheld nowhere.
- **What changed shape underneath a consumer that was not re-read?** A field, a
  state name, or an error payload whose meaning moved while its readers did not.
- **Which claim is asserted by a state that never measured it?** A verdict
  derived from a proxy — a marker standing in for liveness, a flag standing in
  for an authorization, a count standing in for enforcement.
- **Architecture landing ahead of vertical slices** — scaffolding accreting
  before any end-to-end path exercises it, so nothing disproves the design.
- **Tests proving the implementation rather than the invariant** — a test
  restating what the code does passes forever and catches nothing. Ask what
  observable property would break if the implementation were wrong, then break it
  and watch.
- **Docs lagging schema growth** — the schema moved, the document describing it
  did not, and the drift stays invisible until someone trusts the doc.

A milestone that ran two concern-diverse reviews per chunk and never asked a
cross-chunk question has satisfied the floor and still not looked where the
defects were.

Run reviewers as file-backed jobs:

```bash
python3 <skill-root>/scripts/goalflight_review_job.py \
  --agent codex \
  --name <name> \
  --repo "$PWD" \
  --prompt <prompt.md> \
  --output-dir <review-dir> \
  --timeout-s 1800 \
  --max-quiet-s 3600
```

`--timeout-s` is a soft wall for status visibility, not an automatic kill.
The runner keeps a review alive past that wall while stdout/final output or
process-group CPU shows progress. `--max-quiet-s` classifies a quiet, idle
worker as `inconclusive_timeout`; `--max-total-s` is an optional hard wall.
Status heartbeats report `stdout_bytes`, `events_seen`, `last_event_kind`,
`final_detected`, `quiet_for_s`, and process-group CPU.

For a dirty-tree review, start from `prompts/dirty-tree-review.md` and replace
`<repo-root>` plus the focus bullets. Write the rendered prompt to
`docs-private/reviews/<date-topic>/<reviewer>.prompt.md` before dispatch. Keep
rendered prompts, stdout/stderr, status files, and final review outputs under
`docs-private/`; keep reusable prompt skeletons in tracked `prompts/`.

Review states:

- `pending`
- `running`
- `blocked_session_limit`
- `blocked_auth`
- `inconclusive_timeout`
- `complete`
- `failed`
- `superseded`

Convergence rule:

1. Run concern-diverse review flights.
2. Fix confirmed P0/P1/P2 issues.
3. Repeat until a review round returns **zero P0/P1/P2** — that clean round IS
   convergence (NOT a round count; remaining P3s or explicitly-accepted backlog
   aside). One clean round suffices; many rounds without a clean one is NOT converged.
4. Do not treat a missing/stalled review as clean — and do not treat a round count as
   a clean round.

**Default reviewer routing**: lean on gstack's `/review` skill (codex-side
install at `~/.codex/skills/gstack/`). That gives a structured findings-
first review with explicit severity tagging. Pair with a concern-diverse
sweep — grok or cursor in parallel against the same diff — to catch what
codex misses. Claude Agent reviewers are the third option, used only
when codex AND the sweep tool are both unreachable. Codex / grok /
cursor consume their own provider budgets; Claude Agent consumes the
orchestrator's session budget.

Cursor's 2026-05-19 model update brought its coding benchmark on par
with Claude Opus, so cursor is now also a viable reviewer for the
concern-diverse sweep — not just a code-writing worker.

Claude session limits create capacity cooldowns. Fallback reviewers should be
concern-diverse Codex/Grok/Cursor jobs or scheduled retry, not silent omission.
