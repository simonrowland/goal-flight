---
description: "Run a lane-fill bug-sweep campaign: audit → harvest → consolidate → adversarial verify → grouped fixes."
---

# bug-sweep [--mode <milestone|qa|bug-hunt|predicate>] [--anchor <ref>]

Launch a multi-worker bug-sweep over the repo without saturating controller
context. Full procedure: `protocols/lane-fill-bug-sweep.md` (read it before
running). Complements `commands/execute.md` (build) and
`protocols/review-mining.md` (mint/sweep classes).

**When:** at a milestone, before committing to a new arc, on an overdue review,
or to hunt a bug class — when you want many findings fast and trustworthy without
spending controller context on typos/noise.

## Modes (set the frame + matrix content; same plumbing)

| `--mode` | Campaign |
|----------|----------|
| `milestone` (default) | broad correctness/quality pass when a milestone is due |
| `qa` | behaviour/UX/regression sweep of the running app |
| `bug-hunt` | open-ended class discovery; new catches get minted (see `review-mining`) |
| `predicate` | hunt under-searched bug-class predicates from the shared sweep-corpus |

## Pipeline (see the protocol for detail)

1. **Frame + matrix** — write a shared `audit-frame.txt` (protocol, BLOCKING
   rule keyed to the queued backlog, finding schema) + an `audit-matrix.txt` of
   N `domain × lens` rows under `docs-private/reviews/<date>-<slug>/`. Row count
   = coverage need; lanes only set wall-clock (rows > lanes drain in waves).
2. **Lane-fill audit** — one READ-ONLY one-shot worker per row, lane-split by
   difficulty (subtle/critical → stronger engine; breadth → cheaper/wider pool).
   Findings inline to tails. **Launch via the durable queue**
   (`--submit --drain-on-submit`; use `--no-drain-on-submit` only when an
   external drainer owns launch) so workers survive; **codex
   workers: instruct "no context-mode / ctx_* tools"** to avoid the exec-mode
   wedge.
3. **Harvest** — parse the tails into append-only `BUG-LOG.jsonl` (read-only
   workers can't write the sink themselves; this step is mandatory).
4. **Consolidate** — one serial worker: dedup → BLOCKING-vs-backlog triage →
   rank → systemic clusters → `BUG-LOG-CONSOLIDATED.md`; mint `SPECULATIVE`
   class entries while context is loaded.
5. **Adversarial verify** — verifiers grouped by chunk, refute-by-default,
   classify REAL / FALSE-POSITIVE / ALREADY-FIXED / NOT-BLOCKING. Only verified
   blockers are work. (A suspiciously clean 0-FP means the refute stance was too
   soft.)
6. **Fix** — routing tiers (A autonomous / B RAG-assisted / C controller-in-loop),
   disjoint file-set groups, patch-only, serial integrator; worktrees only for
   Tier-C; poison-pair tests + in-worker falsifier.

## Re-review experiment

Record the run in `RUN-MANIFEST.md` with a single `--anchor` knob; a later
re-review changes only the anchor and diffs the verified-REAL sets
(`marginal_real_yield ≈ 0` ⇒ mined out).

## Controller-context budget

Steady state the controller reads only the verified blocker list + the
integrator's per-group pass/fail. Read the full backlog only at a milestone "bug
breather", then reload critical-path context.
