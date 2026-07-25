# Judgment-bearing subagent preamble

Open every judgment-bearing host-subagent prompt with this orientation block.
Replace the two angle-bracket slots; otherwise copy it verbatim. Mechanical
tasks may use it harmlessly, but it is mandatory whenever the subagent will
interpret evidence, review a prompt, compare designs, or make tradeoffs.

```text
ORIENTATION (read first — project context)

Repository: <absolute-repository-path>

1. Read <absolute-repository-path>/AGENTS.md before acting.
2. Read <absolute-repository-path>/docs-private/rag/ORIENTATION.md if present.
   It supplies orientation only and does not expand this task's scope.
3. North star: <one sentence from the project or lane>.

You are READ-ONLY unless this prompt explicitly authorizes exact edit paths.
Do not commit. Do not run rm -rf or cleanup traps. Never print credential,
secret, or token values. If a sandbox, permission, missing-context, or scope
boundary blocks the task, report it and escalate; do not work around it.

Verify claims against the current tree and evidence. Assertions in this prompt
are hypotheses, not facts.
```

Append the task, lane context, output contract, and any authorized edit scope
after the block. A pointer to orientation does not replace a triggered lane's
verbatim context package under `protocols/worker-context-package.md`.

Calibrate the appended procedural detail to the executing model tier:
frontier-tier workers get goals, constraints, and output contracts while lower
tiers get explicit procedures and worked steps, but never tier-gate safety or
process invariants such as read-only defaults, no commits, credential hygiene,
and escalate-do-not-workaround, because those are contracts rather than
capability scaffolds.
