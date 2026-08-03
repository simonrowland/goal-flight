# Worker Contract

Dispatch prompts can reference or paste this file when they need the worker's
status, delivery, return-shape, marker, and no-bypass contract in one place.

## Ledger and status fields

Every spawned worker must have:

- dispatch id
- prompt path/hash
- agent/transport
- worker PID and process identity
- status path
- capacity lease id when applicable

Use `scripts/goalflight_ledger.py record` directly only when a runner did not
already record the worker.

## Pointer-only delivery

From `protocols/dispatch-routing.md`:

```
- Worker delivery stays pointer-only: workers write findings, reviews, and long
  evidence to files, then return paths plus compact status markers.
```

For Agent / Task / Explore dispatches that may produce > 5KB of findings, the
dispatch prompt MUST instruct the subagent to write findings to a file under
`docs-private/research/<date>-<slug>/` and return ONLY a one-paragraph TL;DR +
the file path + severity-tagged finding count. Do not consume the subagent's
full investigation report in conversation — that defeats the dispatch and
silently doubles the context cost (worker read + orchestrator read of same
content).

The final non-empty line for file-backed investigator findings is
`READY: <findings-path>`.

## Return contract by worker class

Dispatch prompts must specify which shape the worker should emit so the
orchestrator can parse the headline without reading the body.

*Investigator (read-only — reviewer, auditor, plan-validator):*

```
TL;DR: <≤3 lines>

Findings: <P0> P0, <P1> P1, <P2> P2, <P3> P3
Strongest concern: <one line>

READY: <findings-path>
```

*Executor (writes + commits — implementation chunk worker):*

```
COMMIT: <local sha>

TL;DR: <≤3 lines — what shipped>

DETAILED: <findings-path with diff narrative + reviewer-pass notes>
Files: <changed-file-list>
Tests: <X/Y passed>
Reviewer pass: <none | gstack-review-clean | findings-applied>
Strongest residual concern: <one line>
```

*Blocked (any worker class — sandbox / permission / hook / tool block):*

```
BLOCKED: <intended-step> blocked due to <reason>

TL;DR: <≤3 lines — what was drafted, what blocked>

Recommended orchestrator action: <one line>
```

Marker grammar and terminal-state parsing live in
`protocols/worker-markers.md`; follow that file rather than duplicating marker
rules here.

## Loop exit discipline (goal-loop workers)

- Convergence means the NAMED acceptance commands pass and review findings at
  P0–P2 are fixed. Safe/easy in-scope P3s may be applied inline when mechanical
  (`protocols/chunk-review.md`) but never drive another iteration; uncertain,
  non-mechanical, or out-of-scope P3s are recorded in the report (`Strongest
  residual concern` / findings file) for the orchestrator to capture. Never
  keep looping solely for P3 polish.
- Iteration bound: if ~3 consecutive iterations produce no new green progress,
  stop and return `BLOCKED:` with evidence — the work so far plus the next
  honest reason — instead of continuing to lap.
- In a pinned lane, re-read the lane brief and spec paths at each iteration
  start and before the commit gate (`protocols/worker-context-package.md`
  §Pin durability).

## Self-review floor and tiers

Before handoff, every worker applies the universal null-hypothesis floor and
states the null hypothesis for its patch: this change did NOT achieve its stated
purpose, is a no-op, or introduced a regression. The worker actively tries to
confirm that null by running the failing case when one exists, diffing observed
before/after behavior, and checking that the stated objective actually moved.
Handoff is allowed only when evidence rejects the null; "it should work" is not
evidence.

Then the worker self-reviews to the tier its change actually earned. The worker
trivial tier includes the seven-category self-review plus the universal
null-hypothesis floor above; it does not add the controller chunk-review layer's
separate >=2 concern-diverse floor.

A sandboxed worker cannot perform every brief step. It self-reviews in its own
context and cannot spawn a nested reviewer because child-process network access
is denied; tests may also require denied capabilities such as process inspection
or external writable roots. Run every permitted step, report the exact command,
capability, and denial for the rest, and never weaken a test or sandbox to pass.
The controller dispatches independent review and completes denied/full-gate
steps in an environment with the required capabilities before commit. A denied
validation is not `BLOCKED:`: finish the work and report its exact evidence.
Escalate `BLOCKED:` only when a denial prevents the work itself. In a worker
brief, "gate fully green" means runnable checks pass and denied checks plus
counts are reported, not zero failures from an impossible step.

- **Trivial/mechanical**: run the existing seven-category self-review plus
  universal null-hypothesis floor to green.
- **Non-trivial**: run self-review to convergence with at least two
  concern-diverse lenses. This is the floor, not the target; add perspectives
  as complexity warrants. Convergence means a clean round with zero P0/P1/P2
  findings, not a round count.
- **Complicated** (optimizer, search, numeric, objective-bearing paths): run
  more than two in-context concern-diverse perspectives and add deeper
  domain/adversarial checks.

Axis rule for non-trivial worker self-review: in-context perspectives are the
floor and primary axis; scale their number and depth with stakes. Engine
diversity belongs to controller chunk and milestone review, never a nested
worker subprocess. Controller chunk review keeps a universal >=2
concern-diverse floor for every commit-worthy chunk, and milestone sweeps keep
at least two concern-diverse reviewers/lenses as their floor.

## Worker workaround prohibition

Workers DO NOT execute workarounds (alternate APIs, git plumbing, inline
content dumps when file-write was blocked) — they return BLOCKED and the
orchestrator decides. Push is NEVER worker-authorized; commit-and-push is a
two-step gate where the worker commits locally (if its envelope permits)
and the orchestrator pushes (only with explicit user permission per the
push-discipline invariant).

## No-bypass clauses

Every dispatch prompt that defines a file-backed return contract MUST include
verbatim:

```
If the file-write path is blocked (sandbox, permission, hook), return
exactly:

  BLOCKED: <intended-path> not writable due to <reason>

  TL;DR: <what was drafted; ≤3 lines>

  Recommended orchestrator action: <one line>

Do NOT inline the drafted content. Do NOT use alternate APIs (REST,
git plumbing) to bypass the standard path. The orchestrator decides.
```

Every dispatch prompt that involves git operations MUST include verbatim:

```
Commits use the standard `git add` / `git commit` path or
`scripts/goalflight_commit.sh`. Do NOT use GitHub REST API,
`git update-ref`, or other plumbing to construct commits. If those fail,
return BLOCKED with the failure trace; do not bypass.

Push is NEVER authorized in a dispatched worker prompt unless this prompt
explicitly says "push permitted". Push requires orchestrator verification +
user authorization.
```

## Verify-survival clause

From `protocols/dispatch-routing.md`:

```
- Failure mode: a worker may complete code edits, emit its terminal marker, then
  lose a long low-output verify run. Treat that as idempotent. Worker prompts
  should make code completion independent of verify survival: if verify is
  killed, return the marker with enough detail for the controller to re-run the
  focused or full verify itself.
```
