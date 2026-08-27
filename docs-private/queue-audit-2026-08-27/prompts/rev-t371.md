# Pinned prompt — `rev-t371`

- source: `prompt-file`
- prompt-file: `/tmp/goal-flight-501/dispatch/rev-t371.assembled.prompt` (EXISTS on disk)
- note: prompt_file taken from ledger.prompt_path
- pinned_at: 2026-08-27T14:54:06.319466+00:00

```
You have a steer mailbox at `$GOALFLIGHT_STEER_FILE`. Read it AT THE TOP OF EACH ITERATION and IMMEDIATELY BEFORE ANY git commit/push. Incorporate new messages into your plan; ack each with `!STEER-ACK: <seq>` on its own line; a steer may redirect or HALT you — honor it. If `$GOALFLIGHT_DISPATCH_SCRIPT` is set and you have nothing to do until the controller answers, use `python3 "$GOALFLIGHT_DISPATCH_SCRIPT" steer "$GOALFLIGHT_DISPATCH_ID" --wait --question-kind USER-NEED --timeout-secs <seconds> '<question>'` (or USER-CONFIRM) to emit the question and wait under a separate bounded deadline.

Your FULL original brief is at `$GOALFLIGHT_PROMPT_FILE`. Re-read it after any internal compaction/summarization, at the start of each long-run goal-loop iteration, and before final commit/exit; the disk file is authoritative over summarized memory.

Worker execution contract:
- Use your available tools to actually perform the requested filesystem, shell, research, or analysis actions before answering. Do not only plan, summarize, or describe commands.
- For successful completion, emit a final line outside any Markdown fence in this exact shape supplied by the dispatch-specific identity contract.
- The `!COMPLETE:` line must be the last non-empty line of your output. Do not print anything after it.
- Legacy unprefixed marker lines remain accepted; new emissions use the `!` prefix.

Terminal evidence identity contract:
- Every terminal marker payload starts with the exact dispatch id `rev-t371`.
- Successful final shape: `!COMPLETE: rev-t371 — <summary>`.
- Use the same id prefix for READY, RESULT, FAILED, USER-NEED, USER-CONFIRM, or BLOCKED. A generic or foreign marker is ignored.

# Review — t-371 reconcile-abandoned identity probe (55bcb7f, 76942df)

Read-only review in /Users/simonrowland/Repos/goal-flight/worktrees/t371-reconcile
(base 25a9043). Only write the findings file. No sub-agents, no nested tools.

THE DEFECT: the identity-proof helper opened with
`if status.get("worker_alive") is True: return False` BEFORE any pid probe. A
worker dying abruptly never writes worker_alive:false, so the flag stayed true
forever — unfalsifiable for exactly the failure it detects. It stranded a live
controller for five hours. The docstring above it claimed to prove every
identity inactive and to fail closed on weak/unknown identities; a stale TRUE
was neither, and was accepted as authoritative.

Null stance — assume the fix traded one wrong answer for a worse one:
1. **The asymmetry must hold.** A CONFIRMED-DEAD probe may override a stale
   `worker_alive: true`. An INDETERMINATE probe (EPERM, unreadable identity,
   provider error) must still FAIL CLOSED — not abandoned. Reclaiming a LIVE
   worker's dispatch is worse than leaving a dead one stranded. Construct all
   three (alive / dead / indeterminate) for REAL — spawn, kill, reap; induce a
   genuine EPERM — and do not accept a stubbed probe return (b-235).
2. Is the docstring now TRUE of what the code does? The old one over-claimed;
   an over-claiming comment is how the next reader is misled.
3. Does the `worker_alive` indeterminate branch (values outside
   {True, False, None}) still behave as before?
4. Dry-run honesty (76942df): does the output now state plainly that NO ledger
   record was changed? Is a non-dry-run path reachable from the CLI at all —
   if not, say so; a command whose only mode is dry-run while sounding
   decisive is itself the finding.
5. **t-369 cross-check, and this is the one most likely to be missed:**
   `_claim_has_active_carrier` is a SECOND, INDEPENDENT gate that classifies
   stranded entries as `active_queue_carrier`. Fixing only the worker_alive
   gate leaves reconcile still reporting the queue healthy. Determine
   empirically whether the currently-stranded entries become visible after
   this change, or whether that second gate still masks them, and say which.
6. **Do NOT accept a second-updater fix for the ledger/status split.** The
   brief told the worker to INVESTIGATE-AND-REPORT that, not fix it; the diff
   touches only 2 files, which suggests it complied — confirm it did not
   quietly add a writer to the status file. Rationale: status.json is a
   DERIVED copy of the ledger, and adding a second updater to the terminal
   path makes a third writer to one fact. That is now minted as a class
   (duplicated authority: two records of one fact, one updated, one not, and
   the stale one is what readers use). If the worker reported findings on the
   split, judge whether they are supported by evidence or asserted.

Run tests/python/test_abandoned_dispatch_reconciliation.py under env isolation
(fresh mktemp GOALFLIGHT_JOURNAL_DIR, GOALFLIGHT_STATE_DIR,
GOALFLIGHT_WAKE_LEDGER, GOALFLIGHT_MESSAGES_DIR, GOALFLIGHT_TASK_STORE,
GOALFLIGHT_PIDFILE_DIR, GOAL_FLIGHT_PIDFILE_DIR;
GOALFLIGHT_CAPACITY_CONF=/dev/null). Reference 29 passed. Program exit codes,
never a pipeline ending in tail. Running reconcile-abandoned read-only against
the real store is encouraged; do NOT run any mutating mode against it.

Findings P0-P3 + VERIFIED CLEAN. Write
docs-private/reviews/2026-08-27-rev-t371/findings.md, then
RESULT: <P-counts + FIX|CLEAN> and READY: <path> as the last line.
BLOCKED: if stuck.

```
