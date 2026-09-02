---
description: "Use when the user invokes /goal-flight goal during an active Goal Flight run to append one compact goal to the queue."
argument-hint: "[SLUG]"
disable-model-invocation: true
---

# goal <SLUG>

Append one goal to the active goal queue.

**When:** during an active goal-flight run (execute, decompose, or ongoing
queue), append here whenever the user adds a new request or scope — do not track
only in chat. If no queue file exists yet, create
`docs-private/goal-queue-<topic>-<date>.md` using the shape in
`commands/decompose-plan.md` step 3 (Progress table + chunk skeleton).

Required fields:

- slug
- intent
- scope
- expected files
- verification command
- dispatch hint: iteration + comms (e.g. `one-shot/acp`, `goal-mode/acp`,
  `goal-mode/bash-tail` for codex `/goal`, or `controller-direct` when context
  is held without fleet stall) — see `protocols/dispatch-routing.md`

Keep the row compact. Detailed execution rules live in
`protocols/dispatch-routing.md`.
