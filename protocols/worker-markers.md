# Worker Marker Protocol

New workers emit sigiled, parseable markers on their own lines. `STATUS` is
the progress verb. `READY` and `COMPLETE` are terminal-only, and `FAILED` is a
failure terminal. `RESULT` is a completed-work summary that may precede the
terminal marker:

- `!STATUS: <current activity>`
- `!STEER-ACK: <seq>` — steer mailbox message acknowledged
- `!RESULT: <dispatch-id> — <summary of completed work>`
- `!USER-NEED: <dispatch-id> — <specific blocker requiring user input>`
- `!USER-CONFIRM: <dispatch-id> — <specific confirmation needed before risky action>`
- `!BLOCKED: <dispatch-id> — <blocker and evidence>`
- `!FAILED: <dispatch-id> — <failure and evidence>`
- `!COMPLETE: <dispatch-id> — <finished state>`
- `!READY: <dispatch-id> — <findings-path>` — Investigator file-backed findings

The leading `!` is optional to parsers for backward compatibility: unprefixed
markers from deployed skills and older workers still work unchanged. New worker
instructions and emissions use the prefixed form.

ACP transport also recognizes `!PERMISSION-OK-PROCEEDED: <reason>` as an
ACP-only non-terminal permission modifier. It is not part of the bash-tail
watcher vocabulary. Use it only when the worker knows it worked around an
auto-declined permission cleanly; otherwise a `!COMPLETE` after an auto-declined
permission downgrades to `blocked_permission_denied`.

Rules:

- Use `!STATUS:` for loading, planning, testing, and every other mid-run update.
  `!RESULT:` records a completed item or gate summary and may be followed by
  more output plus the final `!COMPLETE:` or `!READY:` marker.

- At a dispatch boundary every terminal payload begins with that exact dispatch
  id, optionally followed by an em dash and details: `!COMPLETE: <dispatch-id> —
  <summary>`. The dispatcher injects the exact id. A generic marker, bare
  sign-off, prefix collision, or another dispatch's id is diagnostic prose and
  cannot complete this dispatch. Parser-only calls without an expected id stay
  permissive for offline text analysis.

- Recognized terminal vocabulary: `RESULT`, `COMPLETE`, `READY`, `FAILED`,
  `USER-NEED`, `USER-CONFIRM`, `BLOCKED`. Transport policy decides whether a
  recognized marker stops the current loop.
- The live watcher recognizes a terminal marker only as the worker's **final** non-empty line (mid-output / code-fence markers are ignored — the injection guard).
- A success-terminal candidate does not become irrevocable while its worker is
  still live. If the identity-checked worker keeps producing output after the
  candidate, the watcher logs and discards that false positive after its short
  exit grace, then resumes watching. A later legitimate terminal marker still
  completes normally when the worker exits or reaches the no-growth idle path.
- Dead/stale reconciliation may promote the last valid terminal marker from anywhere in the completed post-prompt tail. This handles workers that emit `!READY:` and then a trailing TL;DR after the marker.
- `COMPLETE` and `READY` are success terminals; `FAILED` is a failure terminal.
  `RESULT` is a success candidate, but watchers terminalize it only after worker
  exit or the no-growth idle rule. Live growth after `RESULT` discards that
  candidate and keeps watching for the actual terminal marker.
- `USER-NEED`, `BLOCKED`, and `FAILED` stop the dispatch loop and surface to the orchestrator.
- A bash-tail worker that has nothing else to do while awaiting a
  `USER-NEED`/`USER-CONFIRM` reply may atomically arm a bounded mailbox wait and
  emit its question with
  `python3 "$GOALFLIGHT_DISPATCH_SCRIPT" steer "$GOALFLIGHT_DISPATCH_ID" --wait --question-kind USER-NEED --timeout-secs <seconds> '<question>'` (substitute
  `USER-CONFIRM` when appropriate). The watcher reports
  `awaiting_steer_reply`, bridges the question to controller mail without taking
  a listener slot, and suspends ordinary CPU-idle accounting only after observing
  the exact wait-id-bound marker in the tracked worker tail and validating that
  the live waiter belongs to the tracked worker process group. The suspension is
  bounded by the independent wait deadline. A controller replies with
  `python3 "$GOALFLIGHT_DISPATCH_SCRIPT" steer "$GOALFLIGHT_DISPATCH_ID" --reply-to <wait-id> [--decision yes|no] '<reply>'`; `USER-CONFIRM` requires the
  explicit decision. Generic steers, foreign wait ids, and duplicate replies do
  not settle the wait. While a correlated reply is durably pending consumption,
  the watcher preserves the validated arm across transient mailbox-lock failures
  until the waiter records its end, emits its typed `STEER-REPLY` consumption
  line (which the watcher checks against the same wait id and reply sequence),
  dies, or reaches its deadline. For an open-ended `USER-NEED`, the command may
  return unacknowledged arm-time controller backlog without arming because that
  steer can answer or redirect the need and carries no authorization. For
  `USER-CONFIRM`, it surfaces the backlog but still emits and arms the exact
  question; only a correlated typed reply with an explicit decision succeeds.
  A watched-tail typed reply receipt also settles renewal when the waiter's
  best-effort end-row append loses its short mailbox-lock race.
  An unresolved or expired arm is non-renewable; another wait is refused until a
  reply is consumed or the dispatch terminally settles. On deadline the command
  returns nonzero and ordinary idle accounting resumes.
- Bash-tail otherwise keeps `USER-CONFIRM` terminal. Unattended ACP routes it to the
  controller without cancelling the turn, preserves partial output, and waits
  at the next turn boundary for a correlated steer reply. The guarded action
  remains unauthorized: marker prose has no tool-call id, kind, or canonical
  targets, so even a correlated `yes` records consent without opening non-read
  permissions. ACK, unrelated steer, timeout, and silence are never approval.
  The question deadline is measured from routing; timeout supplies
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
  non-authorizing for the already-denied ACP call. Restart tombstones retain
  their old cohort, while a fresh post-restart question receives a new one. A
  correlated marker affirmative keeps `controller_decision=yes`, generation,
  scope, and reply audit fields, but status remains
  `guarded_action_authorized=false` and the worker receives
  `recorded-yes-not-authorized`. Use inline permission mode or a new explicitly
  authorized dispatch for that action. Quoted instances of the
  authorize grammar in ordinary steer text or controller notes are rewritten
  to `quoted-yes-not-authorization` before delivery. An affirmative remains
  provisional through the question deadline. Its durable arrival timestamp,
  not a delayed mailbox read, determines the cutoff for a deny-biased `no`.
  Once a per-question answer is finalized and exposed to the worker, its audit
  decision is immutable. A later denial in the same generation is tracked as a
  future-action denial and cannot rewrite the finalized row; it only bounds
  what may happen next.
  A completed safe-work continuation after a denial records
  `blocked_user_confirm_denied`; a denied ACP permission records
  `blocked_permission_denied`. Neither is an unqualified `complete`/`ok=true`.
  Routing any worker marker closes non-read permissions for the remainder of
  the ACP connection. An explicit or synthesized `no` additionally closes the
  generation going forward. Every marker `yes` uses
  `recorded-yes-not-authorized` and cannot reopen non-read permissions; use
  inline permission mode or a new explicitly authorized dispatch for that
  action.
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
