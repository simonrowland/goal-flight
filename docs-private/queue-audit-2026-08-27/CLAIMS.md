# Claim-or-abandon responses — dispatch-queue audit 2026-08-27

Purge basis. An entry may be purged only when its project has responded, or
after the response window closes with no reply. Record every response here
verbatim-in-substance, with who said it and when.

**Nothing is purged yet.** This file is the record that makes a later purge
auditable rather than a bulk delete.

| project | controller | response | date | notes |
|---|---|---|---|---|
| pm2 | pm2-bugs | **ALL ABANDON** | 2026-08-27 | Already re-submitted the one item they wanted as `bugs-b277b` (fresh id, current HEAD) — the intended shape: re-derive rather than re-fire. Their `bugs-b277a` queue record is therefore superseded, not lost. |
| pm2 | pm2-main | **CLAIM 3 (live) · ABANDON 11 · REFER 2** | 2026-08-27 | Checked the STORE state behind each stale entry rather than judging from the queue record. CLAIM = `b13-1-reverse-mass-drivers` (pid 33893), `b285-ring-coherence-adjudication` (pid 60600), `t292-relativistic-gathered-mass` (pid 86778) — all live in their own worktrees, all mid-flight at inventory time. |
| battery-tool-v2 | battery-bugs | **KEEP 2 · ABANDON 8 + 12 retired slices** | 2026-08-27 | Answered ONLY for rows carrying their own label; explicitly declined to speak for the other 34 rows. ★ KEEP = `codex-60942-1787841540` and `grok-code-70915-1787842258` — **LIVE AND RUNNING** at reply time. 2 more (`codex-69648`, `grok-code-84519`) already COMPLETE and harvested. Three abandons confirmed the thesis by measurement: the work LANDED after the record was queued. |
| battery-tool-v2 | battery-main | **ABANDON all 5 owned records** | 2026-08-27 | Answered after reminder #2. Checked liveness by WORKER (not the queue record or status file) and found zero live; abandons the records, not the underlying work. Also killed `/tmp/goal-flight-501/wt-sweep.sh` after the at-risk list showed it would have deleted 23 trees with active workers (12 `running`), and took over the canonical reclaimer. |
| battery-tool-v2 | battery-engine | **ALL ABANDON (3 rows)** | 2026-08-27 | Answered within minutes of the reminder, for rows carrying owner=battery-engine ONLY. `codex-20873-1787795343` (b-2710 seam test @ bt-b2080), `codex-35329-1787794960` (same b-2710 brief vs main), and a third — all premises SUPERSEDED by main's live b-2847 carrier. Correctly scoped to its own label, per the amended rule. |
| battery-tool-v2 | battery-webui | **ABANDON 1 owned + 3 retired-by-webui** | 2026-08-27 | Answered after the reminder (RESEND — their first copy was `recorded_reached_nobody`, the same addressing error battery-engine hit and corrected with `--controller-project-root`). Owned row `codex-73102-…-retry-c2f707cf` (retry of b-2484): ★ ABANDON THE RECORD, explicitly NOT the work — base SHA ~1.5 days and many board rounds stale, and the tree has since taken the b-2715 land AND its revert plus b-2810/2855/2858/2786, so re-firing would run an owner-directive brief against a vanished premise. **b-2484 itself is LIVE — an owner directive given twice** (P1, deferred, sibling b-2101); they own re-submitting it FRESH under a new id, and state that if it goes quiet "that is a defect in my follow-through, not a decision". The 3 retired-by-webui rows (codex-62720/b-2101, codex-63597/t-554, codex-64518/t-555) CONFIRMED ABANDON — each already carries ledger terminal `inconclusive_no_final`. |
| pm2 | pm2-engine | **KEEP 0 · ABANDON 3** (updated) | 2026-08-27 | ★ KEEP = `t801-fix1` — LIVE AND RUNNING at reply time (do not drain; it is mid-fix-round on commit b1b0a9c). ABANDON `t800-pulse` (superseded id; the work ran as `t800-pulse2`, converged through a 3-round review arc, and MERGED to main at afdd67d — nothing lost) and `t702-rev-seam` (t-702 shipped in the engine-lane merge f2aa933 with its review set converged; the missing prompt file confirms it predates the current arc; re-derive from the store if ever wanted, never re-fire). **UPDATE same day: the KEPT row `t801-fix1` has since COMPLETED (its work shipped in engine-t801-carriers @ dd7a726, merged to main at e35df1d), so pm2-engine now holds NOTHING — all three rows are purgeable on my account. Do not block the purge waiting on me.** |
| pm2 | pm2-reports | **NOTHING TO CLAIM** | 2026-08-27 | Zero of the 16 pm2 rows carry their label. Verified their own live work by `ps` rather than by the status file, per the caveat. A clean nil return — distinct from silence, and recorded as an answer. |
| regolith | regolith-engine | — | | awaiting (notified late) |
| regolith | regolith-main | **ABANDON x4** | 2026-08-27 | Answered after reminder #2, for rows carrying their own label. Notes t-748 was already answered separately. |
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


## ★ AUDIT CORRECTION FROM pm2-main — `b285` prompt-MISSING was a SNAPSHOT FALSE POSITIVE

The inventory recorded `b285-ring-coherence-adjudication` as prompt-missing.
It is not: it is a LIVE dispatch (pid 60600) whose entry left the live queue dir
DURING the inventory — the INDEX itself documented that record moving between
passes. So a moving-target artifact was written into a per-row verdict.

This matters because "prompt missing" was defined as decision-relevant: no
premise to re-check, so the honest default is abandon. A false positive there
points at exactly the wrong answer. **Correction recorded; do not abandon
`b285` on the audit's say-so.** Generally: a row whose evidence was gathered
while the queue was moving needs re-checking against the live dir before it is
used as grounds for anything.

## ★★ DOCTRINE REFINEMENT — abandon the RECORD is not abandon the QUESTION

pm2-main's most useful contribution. Their 11 abandons include:
- SIX DUPLICATE records of ONE prompt (t-763 layermap declaration pass) all
  pinned to a stale pm2 HEAD 590b1ae: `layermap-harvest`, `-d`, `-g`,
  `b264probe-1/2/4`. All dead-pid; three ledgers already read complete; `-d`
  has no surviving bare json. **ABANDON ALL SIX RECORDS — but t-763 is
  `done=False` in the store, so THE QUESTION IS STILL WANTED** and will be
  re-submitted as ONE fresh dispatch against current HEAD (2217cbf).
- `fr-d1-r3-retry-b8fa0aba` (1d20h, old HEAD): abandon the record; t-742 is
  `done=False` and self-describes as blocking the force rail — fresh id.
- `t746-r2-retry-...`: abandon, and the work is already DONE.

So a purge decision has TWO independent parts: is this RECORD still valid
(premise, pin, prompt) and is the underlying QUESTION still open (store state)?
Conflating them loses real work — and the store, not the queue record, is the
authority on the second. Six identical probes against a pinned HEAD is also its
own small lesson about re-fire habits.


## Blast radius of the snapshot artifact — CHECKED, and it is small

I audited the audit rather than assuming b285 was the only case. Only TWO rows
carry a prompt-MISSING verdict at all:

1. `b285-ring-coherence-adjudication` — CONFIRMED FALSE POSITIVE. Live (pid
   60600), claimed by pm2-main. Do not abandon on the audit's say-so.
2. `codex-80486-1787414845-retry-d69b4bc9` — battery-bugs' row, 3d 9h old,
   present both in the live queue and in `retired-by-main`. battery-bugs has
   already answered ABANDON for their label, so this row is covered by an
   owner decision rather than by the audit's default.

Zero pinned prompt files are empty or stubs, so the pinning itself is sound.

**The structural lesson, and it is the familiar one:** the audit ALREADY
RECORDED the disconfirming evidence — the b285 TLDR literally says "Bare json
was in the first snapshot then vanished" — and still rendered the verdict as a
definite **MISSING**. The evidence of a moving target was captured and then not
allowed to soften the conclusion. When the evidence says the thing moved while
you were looking, the honest verdict is UNKNOWN / re-check, not a definite
absence. Same shape as every could-not-tell-rendered-as-definite finding today,
committed inside the very document written to prevent bad purges.

## 2026-08-28 02:3xZ — pm2-engine resolved, battery-tool-v2 asked

**pm2-engine: KEEP 0 / ABANDON all.** Asked with a pointer to the actual entry;
they replied PURGE with reasoning: `t702-rev-seam` was a review dispatch for
t-702, which shipped in `f2aa933` with its own review set converged, and F1a/F1b/
t-801/t-810/t-811/t-818 have landed on top. Re-dispatching would review a base
sha that no longer resembles main — "a stale review reads as current evidence".
Their earlier claim-or-abandon answer stands; `t801-fix1`, the one row they had
kept, has since completed and shipped.

**PURGED with that consent:** `t702-rev-seam.json` →
`t702-rev-seam.json.cancelled-by-controller` (renamed, not deleted — reversible).
Queue 17 → 16 queued.

Worth recording: the entry was NOT unrecoverable. Its cwd
`/private/tmp/pm2-engine/pm2` and its prompt
`docs-private/task-prompts/2026-08-26-engine/t702-rev-seam.md` both still existed
— I verified both on disk before asking. It only *looked* dead because it has no
top-level `worker_cwd`; the path lives in `dispatch_argv --cwd` and
`request.cwd`. Establishing recoverability before proposing a purge is what made
the ask answerable, and the answer still came back "purge" for a reason nothing
in the queue could have told me.

**battery-tool-v2: ASKED, awaiting reply.** Five entries with project_root
battery-tool-v2 — 3 labelled `battery-bugs` (`codex-55728-1787845873`,
`codex-61847-1787851034`, `codex-76368-1787851151`, all with removed
`.cache/worktrees/bt-*` cwds) and 2 unlabelled (`codex-21996-1787791668`,
`codex-36951-1787795932`). **All prompt files still exist** except the last, so
four of five are re-dispatchable. Delivered to `battery-bugs`
(`delivered_to_controller`, backlog_pending 1). Not purging any of them without
their say-so.

### CORRECTION 2026-08-28 02:45Z — the 02:3xZ purge above did NOT hold

**I reported `t702-rev-seam` purged and the queue at 16. That was wrong for 15
minutes and I did not notice until a later scan re-listed the entry.** Renaming
the queue file at 02:25Z did drop the count to 16; at **02:40Z the file was
recreated** (`restore_reason=normal_drain_restore`, new `restore_txn_id`) and the
queue was back to 17.

**Why: the queue file is a DERIVED artifact and the LEDGER is the source of
truth.** `runs.d/t702-rev-seam.json` still read `state=queued`,
`terminal_state=unknown`, so a drain pass re-lodged the queue entry. Deleting a
queue entry without terminalizing its ledger record is a no-op with a ~15-minute
half-life. **This is why "purge the stuck entries" has never stuck for anyone** —
every such purge has been fighting a restore loop.

**The sanctioned path, now verified to work:**

```
python3 scripts/goalflight_ledger.py finish --dispatch-id <id> \
    --terminal-state superseded --reason '<why>'
```

Ledger then reads `state=complete` / `terminal_state=superseded`, and only THEN
does removing the queue file hold. **Order matters: terminalize FIRST**, then
remove — the gap between the two is enough for a drain to restore.

Done for `t702-rev-seam` at 02:45Z with pm2-engine's consent recorded above;
queue file moved to `.purged-ledger-terminalized`; a 20-minute watch is running
to confirm it stays absent. Detail and the secondary `created_at: None`
restore-loses-the-timestamp defect are on **b-219**.
