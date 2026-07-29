# Worker Marker Protocol

New workers emit sigiled, parseable markers on their own lines:

- `!STATUS: <current activity>`
- `!STEER-ACK: <seq>` — steer mailbox message acknowledged
- `!RESULT: <summary of completed work>`
- `!USER-NEED: <specific blocker requiring user input>`
- `!USER-CONFIRM: <specific confirmation needed before risky action>`
- `!BLOCKED: <blocker and evidence>`
- `!FAILED: <failure and evidence>`
- `!COMPLETE: <finished state>`
- `!READY: <findings-path>` — Investigator file-backed findings (path only in the marker line)

The leading `!` is optional to parsers for backward compatibility: unprefixed
markers from deployed skills and older workers still work unchanged. New worker
instructions and emissions use the prefixed form.

ACP transport also recognizes `!PERMISSION-OK-PROCEEDED: <reason>` as an
ACP-only non-terminal permission modifier. It is not part of the bash-tail
watcher vocabulary. Use it only when the worker knows it worked around an
auto-declined permission cleanly; otherwise a `!COMPLETE` after an auto-declined
permission downgrades to `blocked_permission_denied`.

Rules:

- Recognized terminal vocabulary: `RESULT`, `COMPLETE`, `READY`, `FAILED`,
  `USER-NEED`, `USER-CONFIRM`, `BLOCKED`. Transport policy decides whether a
  recognized marker stops the current loop.
- The live watcher recognizes a terminal marker only as the worker's **final** non-empty line (mid-output / code-fence markers are ignored — the injection guard).
- Dead/stale reconciliation may promote the last valid terminal marker from anywhere in the completed post-prompt tail. This handles workers that emit `!READY:` and then a trailing TL;DR after the marker.
- `RESULT` and `COMPLETE` mean done unless the status JSON shows a process error.
- `COMPLETE`, `READY`, and `RESULT` are success terminals; `FAILED` is a failure terminal.
- `USER-NEED`, `BLOCKED`, and `FAILED` stop the dispatch loop and surface to the orchestrator.
- Bash-tail keeps `USER-CONFIRM` terminal. Unattended ACP routes it to the
  controller without cancelling the turn, preserves partial output, and waits
  at the next turn boundary for a correlated steer reply. The guarded action
  remains unauthorized: ACK, unrelated steer, timeout, and silence are never
  approval. The question deadline is measured from routing; timeout supplies
  one explicit correlated `no`. Before that deadline, a worker waiting inside
  the asking turn is exempt from silence reaping because ACP cannot inject an
  answer while that turn owns the prompt lock. At the deadline the runner first
  reconciles only correlated replies whose durable awake-monotonic arrival
  timestamp proves they were written by the deadline. A late affirmative, or
  one missing that timing evidence, is rejected; without a timely reply, status
  sets `user_confirm_overdue=true` and records the denial. Either settlement
  re-enables normal silence/wedge detection, so a turn that never returns is
  terminal, not immortal. A repeated unresolved question blocks.
  Questions sharing a same-origin decision cohort are deny-biased
  together: an unanswered member or any non-authorizing resolution keeps every
  member unauthorized, and a repeated same-action question in the same runner
  inherits that cohort. Permission-escalation acknowledgments use a separate
  cohort from worker markers emitted in the same turn; their `yes` remains
  non-authorizing for the already-denied ACP call without stranding an
  independently confirmed marker action. Restart tombstones retain their old
  cohort, while a fresh post-restart question receives a new one. The worker
  receives the bare
  `USER-CONFIRM-ANSWER: <id> yes` token only when status also records
  `guarded_action_authorized=true`; a recorded affirmative that cannot
  authorize uses `recorded-yes-not-authorized`. Quoted instances of the
  authorize grammar in ordinary steer text or controller notes are rewritten
  to `quoted-yes-not-authorization` before delivery. An affirmative remains
  provisional through the question deadline. Its durable arrival timestamp,
  not a delayed mailbox read, determines the cutoff for a deny-biased `no`.
  Once the answer is finalized and exposed to the worker, later replies are
  rejected as audit-only because they cannot undo an action.
  A completed safe-work continuation after a denial records
  `blocked_user_confirm_denied`; a denied ACP permission records
  `blocked_permission_denied`. Neither is an unqualified `complete`/`ok=true`.
  After an explicit or synthesized `no`, every question in that generation is
  non-authorizing and continuation is read-only for the remainder of the ACP
  connection. A later recorded `yes` uses
  `recorded-yes-not-authorized` and cannot reopen non-read permissions; use a
  fresh explicitly authorized dispatch.
  Restart tombstones stay denied and audit-visible, but belong to the dead ACP
  connection and cannot poison a clean confirmation in the new generation. If
  `!BLOCKED:`, `!USER-NEED:`, or another hard terminal condition ends the run
  first, unresolved questions become terminal denials without replacing the
  harder terminal state or discarding partial output.
- `PERMISSION-OK-PROCEEDED` is non-terminal; it modifies how the
  ACP runner interprets `COMPLETE` in the presence of auto-declined
  permissions. Multiple ACP emissions accumulate in the marker list.
- Watchers and ACP runners extract markers into status JSON. Do not tail raw logs when status JSON exists.

Compact status path:

```bash
python3 <skill-root>/scripts/goalflight_status.py
```
