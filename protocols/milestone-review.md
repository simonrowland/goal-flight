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

Claude session limits create capacity cooldowns. Fallback reviewers should be
concern-diverse Codex/Grok jobs or scheduled retry, not silent omission.
