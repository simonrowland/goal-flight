# Milestone Review Protocol

Use at configured review cadence or user-requested review flights.

Run reviewers as file-backed jobs:

```bash
python3 <skill-root>/scripts/goalflight_review_job.py \
  --agent codex \
  --name <name> \
  --repo "$PWD" \
  --prompt <prompt.md> \
  --output-dir <review-dir> \
  --timeout-s 1800
```

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
3. Repeat until no new material findings appear or remaining findings are
   explicitly accepted as backlog.
4. Do not treat a missing/stalled review as clean.

**Default reviewer routing**: lean on gstack's `/review` skill (codex-side
install at `~/.codex/skills/gstack/`). That gives a structured findings-
first review with explicit severity tagging. Pair with a concern-diverse
sweep — grok or cursor in parallel against the same diff — to catch what
codex misses. Claude Agent reviewers are the third option, used only
when codex AND the sweep tool are both unreachable. Codex / grok /
cursor consume their own provider budgets; Claude Agent consumes the
controller's session budget.

Cursor's 2026-05-19 model update brought its coding benchmark on par
with Claude Opus, so cursor is now also a viable reviewer for the
concern-diverse sweep — not just a code-writing worker.

Claude session limits create capacity cooldowns. Fallback reviewers should be
concern-diverse Codex/Grok/Cursor jobs or scheduled retry, not silent omission.
