# `tail -f` observation (legacy)

The `tail -f` shape watches a worker's stdout/stderr file in real time
without owning the worker's lifecycle. Use only when:

- the worker was started outside goal-flight (a long-running background job,
  a separate user-invoked command, an external process you didn't spawn),
- you need a quick human-readable look at progress and don't have an ACP
  session to query,
- you're debugging a wedged or stuck worker whose ACP stream stalled.

For workers goal-flight owns, use `scripts/goalflight_watch.py` instead —
it extracts markers, updates the status JSON, and respects the dispatch
ledger.

## Recipe

```bash
tail -f /tmp/<agent>-<slug>.txt
```

**Do not** run this from the controller's conversation. `tail -f` blocks
the foreground forever; the controller has no way to time-bound it. Run it
from a separate shell or use the structured watcher instead.

Flat marker probe for procedural code (no follow loop):

```bash
grep -E '^(COMPLETE|BLOCKED|USER-NEED|USER-CONFIRM|STATUS|RESULT):' \
  /tmp/<agent>-<slug>.txt | tail -5
```

This returns the most recent marker lines without watching. For an owned
worker dispatch, prefer `scripts/goalflight_watch.py` (requires `--pid`,
`--tail`, `--status-json`) — it updates status JSON and writes a ledger
entry. Run `python3 <skill-root>/scripts/goalflight_watch.py --help` for
the actual argument list.

## Why this is legacy

`tail -f` carries no signal about:

- worker liveness (PID may be gone while tail still has bytes to flush),
- turn boundaries (worker may have completed and exited mid-line),
- structured events (every tool call is just text),
- idle-timeout (you'd have to time it yourself).

The `goalflight_watch.py` helper handles all four. Reach for `tail -f` only
when you genuinely don't own the worker lifecycle.
