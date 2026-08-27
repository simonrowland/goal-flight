# Pinned prompt — `status-r2-closure`

- source: `prompt-file`
- prompt-file: `/tmp/goal-flight-501/dispatch/status-r2-closure.assembled.prompt` (EXISTS on disk)
- inline prompt present: yes
- note: prompt_file taken from ledger.prompt_path
- note: inline prompt also present and DIFFERS from prompt-file; file used as pin
- pinned_at: 2026-08-27T14:54:06.319466+00:00

```
You have a steer mailbox at `$GOALFLIGHT_STEER_FILE`. Read it AT THE TOP OF EACH ITERATION and IMMEDIATELY BEFORE ANY git commit/push. Incorporate new messages into your plan; ack each with `!STEER-ACK: <seq>` on its own line; a steer may redirect or HALT you — honor it. If `$GOALFLIGHT_DISPATCH_SCRIPT` is set and you have nothing to do until the controller answers, use `python3 "$GOALFLIGHT_DISPATCH_SCRIPT" steer "$GOALFLIGHT_DISPATCH_ID" --wait --question-kind USER-NEED --timeout-secs <seconds> '<question>'` (or USER-CONFIRM) to emit the question and wait under a separate bounded deadline.

Your FULL original brief is at `$GOALFLIGHT_PROMPT_FILE`. Re-read it after any internal compaction/summarization, at the start of each long-run goal-loop iteration, and before final commit/exit; the disk file is authoritative over summarized memory.

Terminal evidence identity contract:
- Every terminal marker payload starts with the exact dispatch id `status-r2-closure`.
- Successful final shape: `!COMPLETE: status-r2-closure — <summary>`.
- Use the same id prefix for READY, RESULT, FAILED, USER-NEED, USER-CONFIRM, or BLOCKED. A generic or foreign marker is ignored.

CLOSURE REVIEW of a round-2 bugfix. Repo: /Users/simonrowland/Library/CloudStorage/Dropbox/Starship Mission Design/Regolith Processing/regolith-pyrolysis-simulator, branch engine-2026-08-16.

DO NOT run git checkout, git stash or git restore - the working tree holds the unstaged changeset and a checkout would DESTROY it. git diff, git show and plain file reads only.

SCOPE: the uncommitted working-tree diff PLUS the untracked file tests/chemistry/test_provider_status_vocabulary_guard.py, which git diff does NOT show. Run git status and read it explicitly.

YOUR SPECIFIC JOB is closure, not re-litigation. A previous review of round 1 returned CHANGES REQUIRED with four items. Round 2 claims to address all four. For EACH, decide whether it is CLOSED, PARTIALLY CLOSED, or NOT CLOSED, and say what remains:

1. P1 - AlphaMELTS turned an absent adapter status into ok, at engines/alphamelts/parser.py and the equilibrium-crystallization path in engines/alphamelts/provider.py. Round 2 makes absent or empty mean unavailable at both. Required change also said an authoritative gate-path regression test was needed. Is one present, and does it exercise the AUTHORITATIVE gate path rather than only the projection?

2. P1 - the redox liquidus caller in simulator/core.py caught every Exception from the freeze-gate curve, classified by substring match on the message, and returned a floor fallback declaring the melt fully liquid above 1200 C, which can commit an fe_redox_respeciation transition. Required change: restrict the fallback to expected availability and convergence failures, re-raising contract and programming errors. Round 2 re-raises IntentResultStatusError specifically. IS THAT ENOUGH, or do other contract or programming errors - TypeError, AttributeError, KeyError from a malformed provider result - still reach the fallback and authorise physics? Name them concretely if so.

3. P2 - VapoRock never routed its engine-outcome token through the validated vocabulary because it always constructs IntentResult(status=non_authoritative). Round 2 validates the token in the diagnostics projection before wrapping. Check that this is the ONLY seam where an external VapoRock token enters, and that the early-return path for a missing equilibrium is correctly exempt rather than an accidental bypass.

4. P2 - the round-1 tests pinned the provider mechanism but not the stated contract or the consequence. Round 2 adds a None/empty projection test, a consequence-level test in tests/chemistry/test_evaporation_freeze_gate.py asserting no ledger transition and no fallback record when the status is unrecognised, VapoRock validation tests, and an AST class guard. Is the required integration test genuinely at the consequence level, and does the class guard actually catch the historical defect - it claims to test itself against the verbatim pre-fix expression, so verify that self-test is real and not circular.

THEN, INDEPENDENTLY OF THOSE FOUR: is there any REMAINING path by which an engine status this system cannot interpret still ends up authorising physics or being recorded as evidence? That is the actual invariant. The four items are the route someone else found to it, not the invariant itself.

METHOD: verify against code on disk, not the diff hunk. Every finding needs a concrete failure scenario with inputs and the wrong result. Distinguish structural from behavioural evidence and say which you have. A clean verdict is acceptable and useful. P0 wrong physics or silent corruption, P1 wrong behaviour, P2 bounded defect, P3 hygiene.

DELIVERABLE: docs-private/reviews/2026-08-26-milestone-engine/status-round2-closure.md. Lead with a per-item CLOSED / PARTIALLY CLOSED / NOT CLOSED table, then the independent invariant question, then what you did not check.

Finish your final message with the single line COMPLETE: status-r2-closure
```
