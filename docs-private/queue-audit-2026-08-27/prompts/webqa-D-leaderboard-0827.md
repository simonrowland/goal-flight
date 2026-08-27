# Pinned prompt — `webqa-D-leaderboard-0827`

- source: `prompt-file`
- prompt-file: `/tmp/goal-flight-501/dispatch/webqa-D-leaderboard-0827.assembled.prompt` (EXISTS on disk)
- inline prompt present: yes
- note: prompt_file taken from ledger.prompt_path
- note: inline prompt also present and DIFFERS from prompt-file; file used as pin
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
- Every terminal marker payload starts with the exact dispatch id `webqa-D-leaderboard-0827`.
- Successful final shape: `!COMPLETE: webqa-D-leaderboard-0827 — <summary>`.
- Use the same id prefix for READY, RESULT, FAILED, USER-NEED, USER-CONFIRM, or BLOCKED. A generic or foreign marker is ignored.

WEB QA, SURFACE D: the optimizer leaderboard. Read AGENTS.md and CLAUDE.md at the repo root first - they carry the project invariants and the honesty doctrine this QA is calibrated against.

WHAT YOU ARE DOING. Re-running the surface-D QA pass whose findings were never written to disk. Ticket b-089 points at docs-private/reviews/2026-07-21-webqa/optimizer-leaderboard.md, which does not exist; the webqa directory holds dispatch briefs only. Your job is to regenerate real findings.

RUN IT. Repo root is the cwd. Start the app with .venv/bin/python app.py - it defaults to port 3000, so set REGOLITH_PORT to something unused (8600+) and use that, because 3000 and 8476 may be taken. Drive the browser yourself. Kill whatever you start before you finish; do not leave orphaned server or worker processes behind.

SURFACES IN SCOPE:
  /optimizer                       the page
  /partials/optimizer-table        the HTMX partial
  /api/optimizer/leaderboard       the JSON, including the excluded_ counters

DO NOT RE-REPORT THESE - both are settled and re-reporting them costs us a triage cycle:
  1. The legacy-token 500. A stored row carrying an evidence token the build does not know used to abort the whole request. FIXED at 088807f1: the row is now contained AND counted through a new excluded_unreadable counter beside excluded_infeasible and excluded_nonfinite. If you can still 500 the leaderboard, that IS reportable and is a different defect - say so explicitly and give the reproduction.
  2. The page is not broken at page level. All three surfaces return 200 against a populated store.

KNOWN CONTEXT THAT WILL SHAPE WHAT YOU SEE, so you do not file it as a bug: the only backend this project ships is internal-analytical, and it is in CERTIFICATION_DENYLIST, so the result cache cannot hold an honest feasible row. An EMPTY or very short leaderboard on a default run is a known open defect (b-271) and is NOT yours to file. What IS yours: whether the surface tells the operator the truth about that emptiness, or whether it renders as though nothing was dropped.

SEVERITY, calibrated to this project:
  P0  the surface asserts something FALSE about authority, provenance or certification
  P1  an emitted authority or uncertainty flag is DROPPED on the way to the operator; a control is unusable
  P2  misleading or ambiguous presentation that a careful operator could still read correctly
  P3  cosmetic

The project cares most about the flattering direction: anything that makes a result look MORE certain, MORE authoritative or MORE complete than the data supports. A number rendered without its uncertainty or authority marking is a finding even when the number is right.

DISCIPLINE, and this is the part that decides whether the report is worth reading:
  - REPRODUCE every finding before you write it. Default assumption is FALSE POSITIVE.
  - Give an exact reproduction: URL, what you clicked, what you expected, what you saw.
  - Distinguish "the surface is wrong" from "the underlying data is wrong". The second is usually not a UI bug.
  - A finding you cannot reproduce twice does not go in the report. Say you saw it once and could not reproduce, in a separate section.

DELIVERABLE: write docs-private/reviews/2026-08-27-webqa-D/optimizer-leaderboard.md with your findings, each with severity, reproduction and the file or endpoint anchor. Create the directory. Do NOT commit - the controller integrates. End your final message with the literal line COMPLETE: webqa-D followed by your counts, for example COMPLETE: webqa-D 0 P0, 2 P1, 3 P2, 1 P3.
```
