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
- dispatch hint: `controller-direct`, `acp`, `goal-mode`, or `bash-tail`

Keep the row compact. Detailed execution rules live in
`protocols/dispatch-routing.md`.
