# Chunk Review Protocol

Per-chunk independent review before commit during `execute`. Milestone-scale
review lives in `protocols/milestone-review.md` (separate protocol; do not fold
chunk-level wording into milestone docs or vice versa).

## When

After a chunk's implementation and focused tests pass, before the orchestrator
commits. Per-ticket gate: on receipt, the controller re-takes the
null-hypothesis stance itself, not the worker's claim — assume the change did
NOT achieve its stated purpose, is a no-op, or introduced a regression. The
patch is not done until controller-held evidence rejects that null and shows a
neighbor did not break. The controller review is **mandatory for each returning
chunk** and is distinct from the milestone sweep. At least **two independent,
concern-diverse reviews** per commit-worthy chunk is the floor, not the target —
executor self-review alone is **not** sufficient, and neither is a single reviewer.

**The norm is a parallel review flight, not a single pass.** For a commit-worthy
chunk, run **≥2 concern-diverse reviewers in parallel** (e.g. gstack `/review` +
`./scripts/autoreview.sh`, or two concern-diverse engines), and add **model
diversity** when the change is subtle, security-/contract-bearing, or a fix
closure. **The floor is ≥2, not the target** — the parallel concern-diverse flight is the mandatory
minimum, not merely the norm; a single review does not satisfy it. The failure mode
is too FEW reviews, never too many (and "too few" now means fewer than two). Dispatch
the legs in parallel (backgrounded), not serially, so review breadth costs wall-clock
once, not N times.

**And review each patch TO CONVERGENCE, not one-and-done.** A single parallel pass
that surfaces findings hasn't *reviewed* the patch — it has *started*. Loop
review → fix → re-review on the SAME patch until a review round comes back CLEAN.
**Convergence is DEFINED by that clean round — a review pass that finds zero
P0/P1/P2 — NOT by a round count.** It may take one round (the first pass is already
clean) or several; running N rounds is not "converged", a clean round is, and many
rounds without a clean one is explicitly NOT converged. The two pillars are
**parallel breadth** (concern-diverse reviewers at once) and **per-patch
convergence** (a clean — zero-P0/P1/P2 — round before the patch is done).

## Review axes

Concern diversity is the universal floor, not the target: every non-trivial
review uses at least two lenses, and complicated or high-risk work scales above
two. Engine diversity is a second axis that escalates with stakes: optional for
trivial/mechanical changes, expected when abundant for non-trivial changes, and
strongly expected for complicated optimizer, search, numeric, or
objective-bearing paths. The null-hypothesis stance is universal for every
patch, including trivial ones; it is not a complexity tier. If only one engine
is abundant, run the multi-angle lenses on it and record that engine diversity
was unavailable; never skip review or strand budget to chase another engine.

When a scout names documentation-upkeep rows, chunk review checks only those
rows:

- every named `DESCRIPTIVE` path is in the same changeset and acceptance scope,
  and its patch matches the coupled code change;
- every named `NORMATIVE` path remains unchanged while its `PROPOSED` diff is
  attached to the handoff with the project's decision/review gate recorded;
  and
- no registry row outside the scout's named set was swept into worker scope.

## How the review runs (bash-tail subprocess, not nested ACP tool call)

**gstack `/review` is read-only — invoke it as a bash-tail subprocess with
codex's own read-only sandbox + non-interactive approval policy, NOT as a
nested ACP tool-call inside the worker's shim.** Read-only sandbox enforces
the safety property the ACP permission gate was protecting;
`-c approval_policy=never` removes the asking flow that's redundant when the
inner sandbox is already constraining the subprocess.

**Run it CONTROLLER-SIDE. A dispatched worker cannot review its own work.**
This paragraph used to claim the two flags let a goal-mode worker run its own
review; they do not, and no flag can. Measured 2026-08-02: the worker's
command-execution sandbox denies DNS to **every child process** -- python
`socket.gaierror`, `curl: (6) Could not resolve host`, and `codex exec` failing
identically under `workspace-write` and `read-only` alike. The review needs the
network to reach a model, so it dies before it starts. Changing the INNER
sandbox profile cannot grant connectivity the PARENT denied.

The failure surfaces as a bare `Operation not permitted` or a DNS lookup error,
neither of which names the cause -- so briefing a worker to "run your mandatory
independent review" produces a worker that correctly refuses to commit and
escalates `BLOCKED:` against an instruction that was never satisfiable. Three
workers did exactly that in one day, and their finished work sat staged for
hours. Review is the controller's job on receipt (see the null-hypothesis gate
above); a worker's own pass is the executor SELF-review, which is reasoning over
its diff, not a nested model call.

Do NOT use `--dangerously-bypass-approvals-and-sandbox` — it is
rejected by classifiers and forbidden in adapter manifests
(`adapters/*.json` `forbidden_args`); `-c approval_policy=never` paired with
`--sandbox read-only` is the canonical non-interactive form.

Canonical invocation (controller-side; see above for why not worker-internal):

```bash
mkdir -p docs-private/reviews/<date>-<slug>   # the redirects below do NOT create it
codex exec --sandbox read-only -c approval_policy=never \
  -c 'model_reasoning_effort="xhigh"' \
  --enable web_search_cached \
  "$REVIEW_PROMPT" \
  < /dev/null \
  > docs-private/reviews/<date>-<slug>/codex-review.final.md \
  2> docs-private/reviews/<date>-<slug>/codex-review.stderr.log
```

**Critical: `< /dev/null`.** `codex exec` reads stdin to EOF even when the
prompt is passed positionally. Without an explicit stdin close (or pipe), the
process inherits the parent shell's stdin and blocks waiting for EOF — the
observable symptom is 0 bytes of stdout for hours with near-zero CPU. Every
bash-tail review invocation MUST redirect stdin from `/dev/null` (or pipe the
prompt into stdin instead of passing it positionally).

**Critical: create the review dir first.** The `>` / `2>` redirects above — and
any `cat > <dir>/brief.md <<EOF` heredoc you write to scaffold a review prompt —
do **not** create parent directories (`cat` and shell redirects never `mkdir`).
A missing `docs-private/reviews/<date>-<slug>/` makes the redirect fail with a
bare exit 1 that is easy to miss mid-script: the review then produces no output,
or dispatches with an empty/absent brief, **silently**. `mkdir -p` the dir first
(as shown), or write briefs/outputs with the path-creating **Write tool** instead
of `cat >`. Verify the brief is a non-empty file (`test -s <brief>`) before
launching the reviewer — never confirm it via `git status` (findings/review dirs
are under gitignored `docs-private/`, so git is blind to them). This prevents a
missing parent directory from silently turning a required review into an empty
or absent artifact.

**`--enable web_search_cached` note.** A deprecation warning may name a
similarly spelled configuration key rather than the CLI flag. Keep the
adapter-declared CLI flag when the invocation succeeds. Do NOT substitute a
different string-valued configuration key for the boolean feature flag; the
shapes are not interchangeable.

Parse the captured stdout (`codex-review.final.md`) for severity-tagged
findings (P0/P1/P2/P3) and apply per the chunk-review policy below.

Read-only dispatch workers must not be asked to write the review file
themselves. If the worker is launched with `goalflight_dispatch.py --read-only`,
use an inline-return prompt: ask for severity-tagged findings in the final
response, with `RESULT:`/`READY:` as the final marker, and have the controller
capture that response into `docs-private/reviews/...` afterward. If the review
must create files directly, dispatch it in a writable worktree/sandbox instead
of pairing `--read-only` with a write-artifact prompt.

**Why this works:** the codex-acp shim's permission gate triggers on
worker-issued ACP tool calls (e.g., the worker invoking `codex exec` as a
structured `execute_command` tool, which is what nested ACP-routed
review-dispatches do). A bash-tail subprocess spawned with the inner sandbox
flag set is a different path — the inner codex's sandbox is the safety
boundary, and the worker's outer permission gate doesn't intercept the
already-sandboxed read-only operation. Nesting the review as a tool call
without the read-only sandbox, non-interactive approval policy, and closed
stdin shape lets the outer gate classify it as a write-grade execute; the
canonical subprocess shape avoids that ambiguity.

## Where the review runs

**Independent review is controller-side.** The landing chain runs at least two
independent, concern-diverse review legs to convergence after the worker hands
off; findings return as inline fixes (P3-safe-easy), follow-up commits, or —
on checkpointed dispatches — as a session-resume revision prompt (see
`protocols/dispatch-routing.md` §Checkpointed dispatches).

The worker's own end-of-attempt self-review stays mandatory and uncapped, but
it is **not a leg of the independent floor** — it is the worker checking
itself, and the floor exists precisely because self-checks share the author's
blind spots. Workers do not additionally spawn independent review subprocesses
in-loop: that duplicated the controller chain at frontier-worker prices while
counting toward nothing (an in-loop pass and a landing pass of the same
reviewer find the same defects twice). The only exception is a brief that
explicitly orders an enclosed review for a security/credential-class chunk;
such an ordered pass supplements the floor, never replaces it.

**Landing requires both**: (a) the controller-side full gate green (see §The
landing gate is controller-side and quiet), and (b) the independent floor —
two or more concern-diverse legs, converged. Worker self-review evidence and a
`gate=deferred` marker satisfy neither.

The non-canonical path (nested `codex exec` as ACP tool call without sandbox
flags) is the one to avoid — that's what triggers the chunk-2/3a/12 blocking
pattern. See `protocols/dispatched-worker-recovery.md` for the recovery
protocol when a worker blocks on this path before the fix lands.

## Default — `gstack /review`

`gstack /review` is the canonical chunk-level pre-commit reviewer. It applies
structured severity-tagged findings (P0/P1/P2/P3) against the chunk diff and
is the reference framing this skill is built around.

Invoke gstack `/review` through the host's skill-load mechanism. The exact
invocation is host-specific (each host loads gstack skills its own way — see
the host's gstack install docs); the protocol invariant is that the gstack
`/review` skill, not a hand-rolled prompt, is what runs against the chunk
diff.

Fix P0/P1/P2 findings before commit. **P3 findings: apply the safe/easy ones
in the same review loop** (typos, missing punctuation, obvious doc cleanups,
dead-code crumbs, minor naming fixes) — the goal is high-quality software, not
minimum-strictness gating. Only the genuinely uncertain or out-of-scope P3s
may be deferred with a note in `docs-private/RESUME-NOTES*.md` or the active
goal-queue margin.

### Fix-chunk closing gate

When a FIX chunk closes substantive review findings — non-trivial closures,
oracle/tolerance arithmetic, security or contract surfaces, shared-helper
logic, or multi-round fixes — the closing independent review runs in
resolution-refutation stance. This is the same review floor, not an extra
generic gate: trivial copy/CSS/typo cleanup still gets ordinary independent
review, while closures worth attacking get attacked.

Route refutation legs through the existing sub-billed read-only review
dispatches described in Worker Routing and the bash-tail invocation above;
cost is no reason to skip a substantive refutation. The reviewer obligations
per closure are:

- (a) attempt to REFUTE the closure; default to refuted-if-uncertain.
- (b) re-derive any oracle/tolerance arithmetic INDEPENDENTLY — never trust
  the fix's own numbers.
- (c) verify each designed-red/poison test fires the PRODUCTION predicate, not
  a parallel reimplementation; check the shared helper or production call path.
- (d) treat accepted earlier-round fixes as first-class refutation targets — a
  fix can itself introduce a contract wrinkle.
- (e) poison-pair is the DEFAULT green-proof shape: the green test asserts
  success SEMANTICS, the paired poison proves the named failure category
  actually fires.

Reusable reviewer-prompt fragment:

> FIX-chunk resolution-refutation pass. This chunk closes review findings; do
> not review the code generally. For each substantive closure, attack the
> RESOLUTION: (a) attempt to REFUTE the closure and default to
> refuted-if-uncertain; (b) re-derive oracle/tolerance arithmetic
> independently, never trusting the fix's own numbers; (c) verify every
> designed-red/poison test fires the PRODUCTION predicate through the shared
> helper or production call path, not a parallel reimplementation; (d) treat
> accepted earlier-round fixes as first-class refutation targets; (e) require
> poison-pair proof by default, where the green asserts success semantics and
> the paired poison proves the named failure category actually fires. Return
> severity-tagged findings plus CLEAN only when all attacked closures survive.

When gstack is not installed locally, fall back to the bundled prompts:
`prompts/gstack-claude-review.md` and `prompts/gstack-codex-challenge.md`.
These reproduce gstack's framing for the chunk-level pre-commit gate;
dispatch them via whichever review-class subagent path the host normally
uses. Do **not** hand-roll a custom "please review this diff" prompt
invoked directly against a worker — that bypasses the canonical severity-
tagging framing and is the R19 regression class.

## Complementary — `./scripts/autoreview.sh`

`scripts/autoreview.sh` is a complementary diff-local pre-commit pass. It
runs in parallel with `gstack /review` per the orchestrator's choice for a
given chunk — does **not** replace gstack as the default. autoreview catches
diff-local issues (API footguns, missing tests on touched paths, regression
invariants) that a structural reviewer may not prioritize; the two reviewers
are concern-diverse.

```bash
# Uncommitted chunk (typical)
./scripts/autoreview.sh --mode local

# Committed chunk on branch
./scripts/autoreview.sh --mode branch --base main

# Claude reviewer routed via ACP shim (never `claude -p`)
./scripts/autoreview.sh --mode local --engine claude
```

Background long autoreview runs per `commands/execute.md` step 5 — write
output to `docs-private/reviews/<date>-chunk-<slug>/autoreview.txt` and poll;
do not block the orchestrator on streaming stdout.

**Backwards-looking cadence (standing, not milestone-only).** Run autoreview
over already-LANDED chunks *while the next chunk's executor is running* — overlap
review time with build time so review keeps pace without serializing the queue.
Don't wait for a milestone to look back: every few commits, sweep the recent
committed chunks with a backgrounded autoreview pass in parallel with in-flight
work, and fold any findings as follow-up fix-chunks. Idle-wait on a dispatch is
review time, not dead time.

## Layers

| Layer | Role | Cadence |
|-------|------|---------|
| Executor self-review | In-worker pass (`prompts/executor-self-review.md`) | Every chunk (inside worker output) |
| **Chunk review — `gstack /review` (default)** | **Pre-commit independent structural review** | **Every commit-worthy chunk** |
| `./scripts/autoreview.sh` (complementary) | Diff-local pre-commit pass, parallel with gstack | Per chunk when orchestrator chooses |
| Milestone review | `protocols/milestone-review.md` (gstack `/review` + concern-diverse sweep) | Default 5 chunks, `[milestone]`, or before push |

Minimum before commit: **the controller-side full gate green** (quiet wrapper;
a worker-claimed green does not count), focused-suite evidence from the worker,
**and** at least **two independent, concern-diverse reviews** (the FLOOR, not
the target; scale above it for complicated or high-risk work), run in parallel
and iterated to convergence.
A single reviewer no longer satisfies the floor; gstack `/review` is one leg, not the
whole gate. **Every new bug class a review surfaces triggers the
MINT-generalize loop** (`protocols/review-mining.md`): record the class predicate,
backwards-sweep code + the saved review archive for more instances, and re-audit
for it at milestones. Recording new bug shapes and re-auditing for them is part
of the review cadence — not a separate optional step that lapses.

## Seams — what per-chunk review structurally cannot see

Per-chunk review is scoped to one chunk's diff, so it is blind by construction
to defects that live BETWEEN chunks: a shared schema each side implements
slightly differently, an invariant every chunk half-maintains, two chunks that
each answer a question and disagree, a catalog entry nobody owns. Field
evidence from a multi-chunk milestone: per-chunk output graded well and nearly
every escaped defect was cross-chunk. Adding review passes of the same shape
does not help — the scope, not the effort, is what misses.

Two gates catch seams, and both are standing requirements at a milestone, not
optional extras:

1. **Integrated-tip gate.** Run the full gate against the integrated tip — all
   chunks landed together — not against each chunk in isolation. A suite that
   passed per-chunk can fail integrated, and that failure IS the seam.
2. **Cross-chunk review pass.** One review whose unit is the milestone, not the
   diff: scoped explicitly at the shared surfaces — schemas, invariants,
   catalogs, contracts touched by more than one chunk — and asked where two
   chunks disagree. Give it the chunk list and the shared surfaces, not the
   concatenated diffs; the question is consistency, not correctness-in-detail.

Neither replaces the per-chunk floor; they close a different hole. A milestone
that ran two concern-diverse reviews per chunk and never asked a cross-chunk
question has satisfied the floor and still not looked where the defects were.

### Three recurring weaknesses worth naming in review briefs

Observed repeatedly enough to be worth an explicit prompt line rather than
hoping a reviewer notices:

- **Architecture landing ahead of vertical slices** — scaffolding accretes
  before any end-to-end path exercises it, so nothing disproves the design.
- **Tests proving the implementation rather than the invariant** — a test that
  restates what the code does passes forever and catches nothing; ask what
  observable property would break if the implementation were wrong.
- **Doc upkeep lagging schema growth** — the schema moves, the document that
  describes it does not, and the drift is invisible until someone trusts the
  doc.

## The landing gate is controller-side and quiet

Workers run focused suites only and end with `GATE: DEFERRED-TO-CONTROLLER`
(see the goal-prompt template): their sandboxed full-gate runs are both
expensive (minutes of output read back into worker context per iteration) and
non-authoritative (sandbox restrictions silently skip tests). The controller
runs the authoritative full gate at landing — through the quiet wrapper so the
stream stays on disk:

```shell
python3 <skill-root>/scripts/goalflight_gate.py            # default python suite
python3 <skill-root>/scripts/goalflight_gate.py -- <cmd>   # any project gate
```

The wrapper passes the exit code through unchanged and prints only the summary,
failures, and log path. A worker-claimed green without the controller-side run
is not a landing.

## Fallback when both gstack and autoreview are absent

If doctor reports both unavailable in the host environment:

1. Require executor self-review markers in the worker transcript.
2. Orchestrator inspects diff + focused test output as a fallback gate.
3. Record WARN in `docs-private/env-caveats.md` and recommend installing
   gstack (and optionally autoreview as a complementary addon) at next init.

Do not skip review entirely when tests pass.

## FORBIDDEN

- **Inverting the default policy.** `gstack /review` is the default chunk
  reviewer; `./scripts/autoreview.sh` is the complementary parallel option.
  Do not rewrite this protocol or the surrounding doc surfaces to elevate
  autoreview to default — that displaces the canonical structural reviewer and
  violates the default-owner rule above.
- **Hand-rolling review prompts when gstack is installed.** Do not write a
  custom "please review this diff for bugs" prompt and dispatch it directly
  against a worker via `goalflight_acp_run.py --agent <x> --prompt <custom>`
  or equivalent. Use `gstack /review` and `gstack /challenge` as the
  canonical interfaces, or the bundled `prompts/gstack-*.md` fallbacks when
  gstack is absent. This is the R19 regression class.
- **Folding milestone-review semantics into this protocol.** Milestone
  reviews live in `protocols/milestone-review.md` and follow a separate
  cadence (default 5 chunks, `[milestone]`, or before push). Do not cross-reference
  milestone protocol body into this file or vice versa.

## Install

Recommended add-ons at setup/init: **gstack** (default reviewer) and
**autoreview** (complementary diff-local pass). gstack lives at
`~/.gstack/repos/gstack/.agents/skills/` (or the host-specific install path);
autoreview is vendored at `autoreview/scripts/autoreview` (override with
`AUTOREVIEW_HELPER`). Doctor reports WARN when
either is absent.

### Optional pre-commit review reminder (off by default)

A double-opt-in pre-commit nudge ships at `scripts/goalflight_review_reminder.py`, wired in
`hooks/pre-commit`. It is OFF by default and **cannot be forced on a downloader**: git hooks
aren't distributed with a clone, the repo activates hooks via local `core.hooksPath=hooks`, and
even then the reminder only fires when enabled (`git config goalflight.reviewReminder true` or
`GOALFLIGHT_REVIEW_REMINDER=1`). Enabled, it prints a reminder to run the review flight and
**exits 0 — never blocks**. Strict mode (`git config goalflight.reviewStrict true`) blocks until
acknowledged. Overrides: `GOALFLIGHT_REVIEW_OK=1` (you reviewed), or `git commit --no-verify`
(skip all hooks). It is a solo/local nudge, never an enforced gate on downstream sessions.

## Commit hygiene at chunk completion

When committing a reviewed chunk, use explicit pathspecs:

```bash
git commit -m "<scoped message>" -- <file1> <file2> ...
```

Never bare `git commit` while other goal-flight workers may have staged WIP
in the shared worktree — the commit guard
(`scripts/goalflight_commit_guard.py`) refuses to prevent bundling. See its
error message at failure time for the recovery shape.
