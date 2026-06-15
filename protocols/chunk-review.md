# Chunk Review Protocol

Per-chunk independent review before commit during `execute`. Milestone-scale
review lives in `protocols/milestone-review.md` (separate protocol; do not fold
chunk-level wording into milestone docs or vice versa).

## When

After a chunk's implementation and focused tests pass, before the orchestrator
commits. At least one independent review per chunk — executor self-review
alone is **not** sufficient.

**The norm is a parallel review flight, not a single pass.** For a commit-worthy
chunk, run **≥2 concern-diverse reviewers in parallel** (e.g. gstack `/review` +
`./scripts/autoreview.sh`, or two concern-diverse engines), and add **model
diversity** when the change is subtle, security-/contract-bearing, or a fix
closure. The floor is ≥1; **routinely running only the floor is the under-review
pathology this protocol exists to prevent** — the failure mode is too FEW passes,
never too many. Dispatch the legs in parallel (backgrounded), not serially, so
review breadth costs wall-clock once, not N times.

**And review each patch TO CONVERGENCE, not one-and-done.** A single parallel pass
that surfaces findings hasn't *reviewed* the patch — it has *started*. Loop
review → fix → re-review on the SAME patch until a pass comes back CLEAN (no new
P0/P1/P2). The two pillars are **parallel breadth** (concern-diverse reviewers at
once) and **per-patch convergence** (iterate to clean before the patch is done).

## How the review runs (bash-tail subprocess, not nested ACP tool call)

**gstack `/review` is read-only — invoke it as a bash-tail subprocess with
codex's own read-only sandbox + non-interactive approval policy, NOT as a
nested ACP tool-call inside the worker's shim.** Read-only sandbox enforces
the safety property the ACP permission gate was protecting;
`-c approval_policy=never` removes the asking flow that's redundant when the
inner sandbox is already constraining the subprocess. The two together let
the goal-mode worker run its own review without triggering ACP permission
elicitation. Do NOT use `--dangerously-bypass-approvals-and-sandbox` — it is
rejected by classifiers and forbidden in adapter manifests
(`adapters/*.json` `forbidden_args`); `-c approval_policy=never` paired with
`--sandbox read-only` is the canonical non-interactive form.

Canonical invocation (worker-internal or controller-side, same shape):

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
are under gitignored `docs-private/`, so git is blind to them). (Observed: pm2 F2
+ Chunk B review briefs both failed silently this way, 2026-06-13.)

**`--enable web_search_cached` note.** As of codex v0.131 stderr emits a
deprecation warning about a `[features].web_search_cached` config-toml key
(not the CLI flag) — web search is now enabled by default. The CLI flag
`--enable web_search_cached` itself still works and is the supported way to
trust hook execution for this invocation. Do NOT replace it with
`-c features.web_search="cached"` — that is a different (string-vs-boolean)
key shape and codex rejects it as a config type error.

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
already-sandboxed read-only operation. The 2026-05-27 chunk-2/3a/12 blocking
class came from nesting `codex exec /review` as a tool call without the
read-only sandbox, non-interactive approval policy, and closed stdin shape, so
the gate intercepted it as a write-grade execute.

## Where the review runs

Both worker and orchestrator can run the same bash-tail shape:

1. **Worker phase — review ENCLOSED in the goal loop (required whenever the
   worker can spawn subprocesses)**: the goal-mode worker runs a
   **review-to-green pass inside its own loop before handoff** — it does NOT
   emit `COMPLETE` until its enclosed review is green (P0/P1/P2 resolved or
   explicitly held). It spawns the bash-tail subprocess above, reads the
   findings, applies P3-safe-easy inline, fixes/holds P0/P1/P2, and loops until
   clean. Worker commit includes review evidence (the `codex-review.final.md`
   path) in the commit message. Enclosing review in the loop is the NORM, not an
   optimization — a converged chunk is a *reviewed* converged chunk.
2. **Orchestrator phase (fallback / second-opinion)**: if the worker can't
   run the review (e.g., the chunk's authorized scope doesn't include it,
   or the dispatch transport doesn't let the worker spawn subprocesses
   cleanly), the orchestrator runs the same bash-tail invocation
   controller-direct after the worker's commit. Findings flow back as
   inline fixes (P3-safe-easy) or follow-up commits (P0/P1/P2).

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
| Milestone review | `protocols/milestone-review.md` (gstack `/review` + concern-diverse sweep) | At K-commit cadence or `[milestone]` queue chunks |

Minimum before commit: focused tests green **and** at least one independent
review (the FLOOR). The gstack path satisfies the floor; the NORM is the parallel
flight above. **Every new bug class a review surfaces triggers the
MINT-generalize loop** (`protocols/review-mining.md`): record the class predicate,
backwards-sweep code + the saved review archive for more instances, and re-audit
for it at milestones. Recording new bug shapes and re-auditing for them is part
of the review cadence — not a separate optional step that lapses.

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
  autoreview to default — that displaces the canonical structural reviewer
  and is the regression class R9 in the 2026-05-24 handoff backlog.
- **Hand-rolling review prompts when gstack is installed.** Do not write a
  custom "please review this diff for bugs" prompt and dispatch it directly
  against a worker via `goalflight_acp_run.py --agent <x> --prompt <custom>`
  or equivalent. Use `gstack /review` and `gstack /challenge` as the
  canonical interfaces, or the bundled `prompts/gstack-*.md` fallbacks when
  gstack is absent. This is the R19 regression class.
- **Folding milestone-review semantics into this protocol.** Milestone
  reviews live in `protocols/milestone-review.md` and follow a separate
  cadence (K commits or `[milestone]` queue chunks). Do not cross-reference
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
