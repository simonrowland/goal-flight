---
description: "Show provider headroom and the soonest upcoming reset."
---

# usage

Render one normalized table of provider/account headroom, local reset times,
health flags, and the soonest upcoming reset across all rows.

Run:

```shell
python3 <skill-root>/scripts/goalflight_usage.py
```

Use `--json` for the merged normalized rows. Provider readers under
`scripts/ext/` are optional local tooling; an absent or unhealthy reader
degrades to one `unavailable` row without making the readout fail.
