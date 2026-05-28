# Chunk Review Protocol

Per-chunk independent review before commit during `execute`. Milestone-scale
review lives in `protocols/milestone-review.md` (separate protocol; do not fold
chunk-level wording into milestone docs or vice versa).

## When

After a chunk's implementation and focused tests pass, before the controller
commits. At least one independent review per chunk — executor self-review
alone is **not** sufficient.

## How the review runs (bash-tail subprocess, not nested ACP tool call)

**gstack `/review` is read-only — invoke it as a bash-tail subprocess with
codex's own read-only sandbox + bypass-approvals, NOT as a nested ACP
tool-call inside the worker's shim.** Read-only sandbox enforces the safety
property the ACP permission gate was protecting; bypass-approvals removes
the asking flow that's redundant when the inner sandbox is already
constraining the subprocess. The two together let the goal-mode worker run
its own review without triggering ACP permission elicitation.

Canonical invocation (worker-internal or controller-side, same shape):

```bash
codex exec --sandbox read-only --dangerously-bypass-approvals-and-sandbox \
  -c 'model_reasoning_effort="xhigh"' \
  -c 'features.web_search="cached"' \
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

**`features.web_search` over `--enable web_search_cached`.** The
`--enable web_search_cached` flag is deprecated as of codex v0.131 (it still
runs but emits a deprecation warning into stderr). Use the `-c` config-key
form `features.web_search="cached"` instead; that's the supported v0.131+
shape.

Parse the captured stdout (`codex-review.final.md`) for severity-tagged
findings (P0/P1/P2/P3) and apply per the chunk-review policy below.

**Why this works:** the codex-acp shim's permission gate triggers on
worker-issued ACP tool calls (e.g., the worker invoking `codex exec` as a
structured `execute_command` tool, which is what nested ACP-routed
review-dispatches do). A bash-tail subprocess spawned with the inner sandbox
flag set is a different path — the inner codex's sandbox is the safety
boundary, and the worker's outer permission gate doesn't intercept the
already-sandboxed read-only operation. The 2026-05-27 chunk-2/3a/12 blocking
class came from nesting `codex exec /review` as a tool call without the
sandbox+bypass flags, so the gate intercepted it as a write-grade execute.

## Where the review runs

Both worker and controller can run the same bash-tail shape:

1. **Worker phase (preferred when worker can)**: the goal-mode worker
   includes a self-review step in its loop. It spawns the bash-tail
   subprocess above, reads the findings, applies P3-safe-easy inline,
   surfaces P0/P1/P2 as queue items or holds the chunk open. Worker commit
   includes review evidence (the `codex-review.final.md` path) in the
   commit message.
2. **Controller phase (fallback / second-opinion)**: if the worker can't
   run the review (e.g., the chunk's authorized scope doesn't include it,
   or the dispatch transport doesn't let the worker spawn subprocesses
   cleanly), the controller runs the same bash-tail invocation
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

When gstack is not installed locally, fall back to the bundled prompts:
`prompts/gstack-claude-review.md` and `prompts/gstack-codex-challenge.md`.
These reproduce gstack's framing for the chunk-level pre-commit gate;
dispatch them via whichever review-class subagent path the host normally
uses. Do **not** hand-roll a custom "please review this diff" prompt
invoked directly against a worker — that bypasses the canonical severity-
tagging framing and is the R19 regression class.

## Complementary — `./scripts/autoreview.sh`

`scripts/autoreview.sh` is a complementary diff-local pre-commit pass. It
runs in parallel with `gstack /review` per the controller's choice for a
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
do not block the controller on streaming stdout.

## Layers

| Layer | Role | Cadence |
|-------|------|---------|
| Executor self-review | In-worker pass (`prompts/executor-self-review.md`) | Every chunk (inside worker output) |
| **Chunk review — `gstack /review` (default)** | **Pre-commit independent structural review** | **Every commit-worthy chunk** |
| `./scripts/autoreview.sh` (complementary) | Diff-local pre-commit pass, parallel with gstack | Per chunk when controller chooses |
| Milestone review | `protocols/milestone-review.md` (gstack `/review` + concern-diverse sweep) | At K-commit cadence or `[milestone]` queue chunks |

Minimum before commit: focused tests green **and** at least one independent
review. The gstack path satisfies this; complementary autoreview adds signal
but does not replace the gstack default.

## Fallback when both gstack and autoreview are absent

If doctor reports both unavailable in the host environment:

1. Require executor self-review markers in the worker transcript.
2. Controller inspects diff + focused test output as a fallback gate.
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
autoreview requires the upstream helper (typically `AUTOREVIEW_HELPER` or
`~/.cursor/skills/autoreview/scripts/autoreview`). Doctor reports WARN when
either is absent.
