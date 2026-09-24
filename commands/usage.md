---
description: "Use when the user invokes /goal-flight usage to show Goal Flight provider headroom and the soonest upcoming reset."
---

# usage

Render one normalized table of provider/account headroom, local reset times,
health flags, and the soonest upcoming reset across all rows.

Run:

```shell
python3 <skill-root>/scripts/goalflight_usage.py
```

Use `--json` for an object containing the merged normalized `rows` and a
`live_workers_by_model` summary. The live summary groups workers with a live
worker PID by recorded model (or the tail's legacy `model:` header) and
controller label; stale/dead dispatches are excluded and unknown identities
are reported as `unverified`. Provider readers under
`scripts/ext/` are optional local tooling; an absent or unhealthy reader
degrades to one `unavailable` row without making the readout fail.

Text output includes a one-line pointer to `/goal-flight traffic` for the full
model/controller table.
