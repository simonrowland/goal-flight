# Resume or redispatch?

A dead or stopped worker is not automatically a lost worker. Resuming keeps
the worker's accumulated reasoning — the files it has read, the dead ends it
already ruled out, the half-finished edit it understands. Redispatching buys
a clean slate at the price of re-deriving all of it. Choose by asking what
you want to keep, not by what killed the worker.

## Resume when the worker's context is still worth more than a fresh start

- **Provider/quota death mid-task.** The work was fine; the seat ran out.
  Resume on a seat with headroom rather than paying for the same reading
  twice.
- **Idle/quiet timeout on a worker that was genuinely working.** Reconcile
  first (`worker_still_alive`, tail growth, dirty tree): if the process is
  alive, you do not need either verb — re-arm the watcher. If it died with
  useful partial edits, resume.
- **Partial edits sitting in the tree.** A fresh worker will not understand
  half-applied changes it did not make; the original will. Resume, or revert
  the partial work first and redispatch — never leave a stranger to guess.
- **Long premise-heavy briefs** (large corpora, deep reading before the first
  edit) where the re-read cost dominates the remaining work.

## Redispatch when the premise changed or the context is spent

- **The brief materially changed** — review findings, a steer that redirects,
  a corrected policy. A resumed worker carries the old framing and argues
  with the new one; a fresh worker reads the new brief as truth.
- **Fix rounds after review.** Each round has a new authoritative findings
  list. Redispatch with the findings file named; do not resume the worker
  whose work is being corrected.
- **Context poisoned or exhausted** — the worker compacted, lost its brief,
  or is looping. A resume inherits the confusion.
- **You want independence.** Reviews, refutations, and second opinions must
  never resume the implementer's session: shared context defeats the point.
- **Cheap work.** If re-reading costs less than the ceremony of recovering a
  session, just redispatch.

## Mechanics

- `goalflight_dispatch.py resume <dispatch-id>` recovers the recorded session
  (codex-resume family) and continues under the SAME dispatch id, so ledger
  and journal history stay one story.
- A resume still needs the brief on disk: the worker re-reads
  `$GOALFLIGHT_PROMPT_FILE`, which is authoritative over its summarized
  memory. Update that file BEFORE resuming if the plan changed.
- Ownership is recorded at dispatch time; a resumed dispatch keeps its
  original owner, so wakes still route to the controller that started it.
- Never resume into a tree another worker now owns. Check for in-flight
  dispatches on the same files first.

## The honest default

When in doubt on a SHORT task, redispatch — a clean read is cheap and the
verdict is easier to trust. When in doubt on a LONG one, resume — the
re-read is the expensive part, and the original worker already paid it.
