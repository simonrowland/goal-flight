---
description: "Use when the user invokes /goal-flight traffic to show live workers by model (luna/sol/astra/grok/...) and controller, flagging sol and astra overuse."
---

# traffic

Show live workers grouped by model and controller:

```shell
python3 <skill-root>/scripts/goalflight_traffic.py
```

Use `--json` for the `live_workers_by_model` object. Terminal dispatches and
PID-reused workers are excluded; workers whose identity cannot be verified are
reported separately as `unverified`.
