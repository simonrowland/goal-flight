# Decisions — {{TOPIC}}

<!-- Slice: docs-private/rag/decisions.md
     Word budget: ~1500 words (upper bound; under is better).
     Scope: chronological cross-goal decision log. The ONLY slice where
     opinions live; each opinion must cite the goal/commit that produced
     it. Splitting loses the chronological view — for projects exceeding
     1500 words, split by EPOCH (decisions-pre-v1.md, decisions-v1.md, ...)
     rather than by topic. -->

## Source pins

- `git log --oneline` on `{{BRANCH}}` (since `{{EPOCH_START_HASH}}`)
- `{{GOAL_QUEUE_PATH}}` — STATUS lines, DEFERRED entries, Reviewer notes.
- `{{PLAN_PATH}}` — section calling out adopted/rejected approaches.
- `{{RESUME_NOTES_PATH}}` — "Open follow-ups (deferred, not blocking)".

## How to read this slice

Decisions are listed oldest-first within each epoch (append-only chronological
order — new entries go to the bottom of the current epoch) and oldest-epoch-first
top-to-bottom (epochs progress earliest → latest down the file). Each entry is
self-contained: rationale + trip-wire so a future executor knows when to revisit.

## Decisions

<!-- Format per entry:
     ### {{DATE}} — {{DECISION_SHORT}}
     {{One-sentence rationale.}}
     **Trip-wire to revisit:** {{condition that would force us to reopen this}}.
     **Source:** {{goal-slug | commit-hash | session-date}}.
     -->

### {{DATE_1}} — {{DECISION_1_SHORT}}

{{DECISION_1_RATIONALE_ONE_SENTENCE}}

**Trip-wire to revisit:** {{DECISION_1_TRIPWIRE}}.

**Source:** {{DECISION_1_SOURCE}}.

---

### {{DATE_2}} — {{DECISION_2_SHORT}}

{{DECISION_2_RATIONALE_ONE_SENTENCE}}

**Trip-wire to revisit:** {{DECISION_2_TRIPWIRE}}.

**Source:** {{DECISION_2_SOURCE}}.

---

### {{DATE_3}} — {{DECISION_3_SHORT}}

{{DECISION_3_RATIONALE_ONE_SENTENCE}}

**Trip-wire to revisit:** {{DECISION_3_TRIPWIRE}}.

**Source:** {{DECISION_3_SOURCE}}.

---

<!-- example:
### 2026-04-22 — FactSage deferred indefinitely

License procurement stalled and the open-source alphaMELTS path covers the
required intent set within tolerance on the calibration cohort.

**Trip-wire to revisit:** alphaMELTS parity falls below 95% on any new
feedstock cohort, or the license becomes free of charge.

**Source:** goal `factsage-integration` set to STATUS: DEFERRED INDEFINITELY
in goal-queue on 2026-04-22; user call recorded in resume notes rev 7.

---

### 2026-04-10 — Single-writer invariant for canonical_store

Concurrent writers produced silent corruption during the cohort-3 backfill;
serializing through `engines/commit.py` made the bug class impossible.

**Trip-wire to revisit:** a future requirement for streaming writes
> 100 MB/s — at which point single-writer becomes the bottleneck and a
WAL design is needed.

**Source:** commit `a4f7c21`; postmortem in `docs-private/postmortem-2026-04-10.md`.
-->

## Self-check before reporting done

- [ ] Every entry has all four fields: date, decision, rationale, trip-wire, source.
- [ ] Rationale is one sentence. Not a paragraph. If you need more, the decision is too compound; split it.
- [ ] Trip-wire is a concrete condition (a test result, a metric crossing a threshold, a deadline) — not "if things change".
- [ ] Source cites a goal-slug, commit hash, or dated session reference. Verified the citation resolves.
- [ ] No opinion appears without a source. This is the ONLY slice where editorial drift is permitted; even here, every drift is anchored.
- [ ] Chronological order preserved within each epoch.
- [ ] Word count under budget. If over: do NOT split by topic. Split by epoch:
      create `decisions-pre-{{EPOCH}}.md` for older entries, leave current epoch here.
- [ ] Voice matches AGENTS.md: terse, technical. File:line and commit refs.
