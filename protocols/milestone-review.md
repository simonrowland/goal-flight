# Milestone Review Protocol

Milestone-scale review flights at configured cadence or `[milestone]` queue
chunks. **Separate protocol** from per-chunk pre-commit review
(`protocols/chunk-review.md`).

## When — mandatory cadence, NOT optional (the most-forgotten review layer)

Two triggers, **mandatory at each** — milestone sweeps are routinely forgotten, so treat
them as a gate, not a nicety:
- **K-commit cadence** — every K commit-worthy chunks since the last milestone sweep (K per
  the active plan / `commands/execute.md`; default 5 when unset).
- **`[milestone]`-tagged chunks** — run the sweep as soon as that chunk lands.

Track the commit of the last completed milestone sweep (its deposit under
`docs-private/reviews/<date>-milestone-*/`). When the chunk count since then reaches K — or a
`[milestone]` chunk lands — the sweep is **DUE**, and further chunk dispatch **waits until it
COMPLETES — i.e. converges to a clean (zero-P0/P1/P2) round**, not merely launches. A skipped or
overdue milestone sweep is an **open liability, not a clean state** (same rule as an unswept bug
class); "I'll do it later" is a skip. The sweep is a **parallel concern-diverse flight reviewed to
convergence** (the same **≥2-reviewer floor**; the milestone Convergence rule below governs what
"converged" means here) across the milestone's accumulated diff, plus a backwards-looking pass
over every chunk landed since the last milestone.

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
