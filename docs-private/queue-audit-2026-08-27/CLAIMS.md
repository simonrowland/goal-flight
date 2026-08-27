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
| battery-tool-v2 | battery-bugs | **KEEP 2 · ABANDON 8 + 12 retired slices** | 2026-08-27 | Answered ONLY for rows carrying their own label; explicitly declined to speak for the other 34 rows. ★ KEEP = `codex-60942-1787841540` and `grok-code-70915-1787842258` — **LIVE AND RUNNING** at reply time. 2 more (`codex-69648`, `grok-code-84519`) already COMPLETE and harvested. Three abandons confirmed the thesis by measurement: the work LANDED after the record was queued. |
| battery-tool-v2 | battery-main | — | | awaiting |
| battery-tool-v2 | battery-engine | — | | awaiting (notified late, see correction) |
| battery-tool-v2 | battery-webui | — | | awaiting (notified late) |
| pm2 | pm2-engine | **KEEP 1 · ABANDON 2** | 2026-08-27 | ★ KEEP = `t801-fix1` — LIVE AND RUNNING at reply time (do not drain; it is mid-fix-round on commit b1b0a9c). ABANDON `t800-pulse` (superseded id; the work ran as `t800-pulse2`, converged through a 3-round review arc, and MERGED to main at afdd67d — nothing lost) and `t702-rev-seam` (t-702 shipped in the engine-lane merge f2aa933 with its review set converged; the missing prompt file confirms it predates the current arc; re-derive from the store if ever wanted, never re-fire). |
| pm2 | pm2-reports | **NOTHING TO CLAIM** | 2026-08-27 | Zero of the 16 pm2 rows carry their label. Verified their own live work by `ps` rather than by the status file, per the caveat. A clean nil return — distinct from silence, and recorded as an answer. |
| regolith | regolith-engine | — | | awaiting (notified late) |
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


## ★ PROCESS CORRECTION 2026-08-27 — I used the wrong unit of consent

I sent the audit to one or two controllers PER PROJECT and wrote purge rule 1 as
"purgeable once every controller notified for that project has responded". But
the rows carry PER-CONTROLLER ownership: `battery-tool-v2.md` alone spans four
controllers (main, engine, webui, bugs), and 34 of its 46 rows did not belong to
either controller I notified. Under my own rule those rows would have expired on
a sibling's silence — for work whose premise the responder could not even see.

battery-bugs caught it by answering only for their own label and saying so.
CORRECTED: battery-engine, battery-webui, pm2-engine, pm2-reports and
regolith-engine were notified (5/5 delivered) with the correction stated.
**Purge rule 1 is amended: the unit of consent is the OWNING CONTROLLER LABEL,
not the project.** A label that was never notified cannot expire by silence.

## ★★ THE LIVENESS CAVEAT EARNED ITS KEEP — A NEAR MISS

battery-bugs reports that BOTH entries they kept (`codex-60942-1787841540`,
`grok-code-70915-1787842258`) were inventoried with **claim-pid dead or none
while the WORKER WAS ACTIVELY RUNNING** — one carrying ~390KB of tail and a
steered lead, the other with a tail that moved 12s before they checked. Exactly
caveat (2): filename-pid liveness is not worker liveness. Had we purged on the
inventory's apparent liveness, we would have destroyed live work mid-flight.
They add a durable operational fact: in their fleet the status file routinely
reads `queued`/`pid=None` for a worker's ENTIRE run, and the only reliable check
they have found is walking `ps` for the codex/grok `--cwd` argument and counting
distinct PGIDs. (That status-file unreliability is another instance of the
duplicated-authority class, t-373.)


## ★★ SECOND NEAR MISS — the consent correction was not theoretical

pm2-bugs answered **ALL ABANDON** for pm2. Under my ORIGINAL project-level rule
that reply, plus pm2-main's silence, could have expired every pm2 row — and
pm2-engine has since claimed `t801-fix1` as **LIVE work running right now**.
So the project-as-unit-of-consent rule would have destroyed a live dispatch on
the say-so of a controller who did not own it and could not see it.

pm2-engine was only asked because battery-bugs pointed out the flaw. Two
independent near misses in one hour, both in the same direction: **a
sibling's answer is not consent for your rows.** The amended rule (consent is
the owning controller label) is load-bearing, not bookkeeping.

Corollary worth keeping: the two ALL-ABANDON replies received so far are
honest for their OWN labels and must NOT be read as project-wide verdicts,
however they are phrased. When recording a reply, record WHICH LABEL it speaks
for, not which project it came from.


## Note on nil returns

`pm2-reports` answered NOTHING TO CLAIM after checking. That is an ANSWER, not
silence, and it counts as consent for their label. Record nil returns
explicitly — otherwise a later reader cannot distinguish "checked, owns none"
from "never replied", and those have opposite implications for whether the
window may close.
