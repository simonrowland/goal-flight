# Extended Controller Guidance

`SKILL.md` is authoritative. This file elaborates rules already present there:
worked examples, failure narratives, and rationale only. If deleting this file
would make a rule disappear, move that rule back to `SKILL.md`.

<a id="activation-check"></a>
## Activation Check

The stale-skill warning exists because compacted sessions often preserve only a
host reminder such as "goal-flight previously invoked" while dropping back-half
rules about routing, review, markers, and state. Treat that reminder as a hint,
not loaded instructions. Reload through the canonical resume order before using
any remembered rule.

The designated-controller check prevents two controllers from mutating the same
queue or dispatch ledger. Matching `current_session.id` means continue; a
mismatch means surface ownership before claiming.

<a id="hard-invariants"></a>
## Hard Invariants

Large read-only or review returns belong in files because inline transcripts
land back in controller context. A worker returning a 9 KB report directly has
not saved context; it has shifted the read cost to the orchestrator. The useful
shape is a terse TL;DR, severity counts, and `READY: <path>` as the terminal
marker.

For read-heavy reconnaissance, dispatch an Agent/Explore prompt with a bounded
question and a file-backed findings path. The controller should read the result
summary first and open the findings file only when it signals an actionable
issue.

Host Agent code execution bypasses capacity leases, ledger/status visibility,
worker markers, steering, and the controller/worker provider split. That is why
it is a degraded fallback for execution, while read-only Agent/Explore review
remains valid.

Worker block examples: a file-write block should return `BLOCKED:` plus the
needed path or permission; a commit/push block should return to the orchestrator
instead of using alternate git plumbing or network APIs.

<a id="autonomous-throughput"></a>
## Autonomous throughput

The forbidden engagement pattern is the "are you still there?" loop: after a
non-blocking discovery, the controller asks whether to perform the obvious next
step already authorized by the active goal. Examples include "I found a failing
test; should I fix it?", "shall I continue with step two?", and "want me to run
the focused test?" when the plan already requires that work.

Real stops are narrower: permission, destructive or irreversible action without
a plan default, product choice the plan cannot infer, auth/capacity hard stop,
or an explicit command gate. Everything else should be recorded in the relevant
file-backed state and carried forward.

<a id="worker-routing"></a>
## Worker Routing

Host-Agent-last-resort exists to prevent a slow or awkward CLI worker from being
quietly replaced by an invisible executor. A transient CLI hiccup is not proof
that every CLI worker is unreachable. Probe first, record the degraded fallback,
and return to `goalflight_dispatch.py` as soon as a marker-reliable worker is
available.

Read-only review/analysis remains different from execution: Explore/Agent can
summarize code or logs, but code-writing chunks need ledger/status/marker
coverage unless the controller-direct path is truly tiny.
