---
description: "Append one compact goal to the active queue."
---

# goal <SLUG>

Append one goal to the active goal queue.

Required fields:

- slug
- intent
- scope
- expected files
- verification command
- dispatch hint: iteration + comms (e.g. `one-shot/acp`, `goal-mode/acp`,
  `goal-mode/bash-tail` for codex `/goal`, or `controller-direct` for tiny
  inline edits) — see `protocols/dispatch-routing.md`

Keep the row compact. Detailed execution rules live in
`protocols/dispatch-routing.md`.
