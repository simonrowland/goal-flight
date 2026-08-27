# Claim-or-abandon responses — dispatch-queue audit 2026-08-27

Purge basis. An entry may be purged only when its project has responded, or
after the response window closes with no reply. Record every response here
verbatim-in-substance, with who said it and when.

**Nothing is purged yet.** This file is the record that makes a later purge
auditable rather than a bulk delete.

| project | controller | response | date | notes |
|---|---|---|---|---|
| pm2 | pm2-bugs | **ALL ABANDON** | 2026-08-27 | Already re-submitted the one item they wanted as `bugs-b277b` (fresh id, current HEAD) — the intended shape: re-derive rather than re-fire. Their `bugs-b277a` queue record is therefore superseded, not lost. |
| pm2 | pm2-main | — | | awaiting |
| battery-tool-v2 | battery-main | — | | awaiting |
| battery-tool-v2 | battery-bugs | — | | awaiting |
| regolith | regolith-main | — | | awaiting |
| goal-flight/kiln | kiln | — | | awaiting |

## Purge rules (decide before acting, not during)

1. A project's entries are purgeable once EVERY controller notified for that
   project has responded, or the window closes.
2. "No reply" is expiry, not consent to re-fire — the entry is dropped, never
   drained.
3. An entry a controller CLAIMS is not drained either: they re-submit it fresh
   under a new id. The queue record is dropped once they confirm the
   re-submission exists.
4. Claim markers (`*.claimed-<pid>-<ts>`) follow their bare `.json`. The 11
   claimed-only records have no bare entry and no owner — they are expired with
   the rest, and their pinned prompts remain in `prompts/` if anyone ever wants
   the text.
5. The `_raw-snapshot/` copy is retained after the purge. It is the only
   durable record once `/tmp` is cleared, and it costs nothing to keep.
