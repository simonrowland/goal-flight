# Pinned prompt — `t750-r2-notfixed`

- source: `inline`
- inline prompt present: yes
- pinned_at: 2026-08-27T14:54:06.319466+00:00

```
You are reviewing an UNCOMMITTED changeset in the regolith-pyrolysis-simulator repo at /Users/simonrowland/Library/CloudStorage/Dropbox/Starship Mission Design/Regolith Processing/regolith-pyrolysis-simulator.

ORIENTATION FIRST: read AGENTS.md at the repo root and CLAUDE.md (Simon pyrolysis mandate) before judging anything.

THE CHANGESET: a unified diff plus the two new files is written to docs-private/review/t750/changeset.diff. The live working tree has the changes applied. Use git diff and git show to inspect history. Do NOT run git checkout or git stash - other controllers share this repo and a checkout will disturb their state.

YOUR LENS: NOT-FIXED. Adopt the null hypothesis that this changeset does NOT actually fix what it claims to fix, and try hard to establish that. The stated intent is that a duplicated rule now has exactly one owner and one definition. Attack that claim directly.

Specific things worth trying to prove: that a copy of the rule still survives somewhere the author did not look, in any form - a different spelling, an inlined loop, a comprehension, a constant defined elsewhere, a test helper, a serialized artifact, a config file, anything outside the directories the author searched. That the extraction silently CHANGED behaviour somewhere, because a moved function is only equivalent if every caller resolved the same names - check the moved code against what it used to close over. That the new guard tests would still pass if someone reintroduced the defect in a slightly different shape, which would make them decorative.

METHOD: reproduce every concern against the current tree before reporting it. A concern you cannot reproduce is a non-finding; report it as one. The venv is at .venv/bin/python. Running the tests is encouraged, including deliberately breaking something to check the guard actually catches it.

Write your review to docs-private/review/t750/r2-notfixed.md. Distinguish CONFIRMED from PLAUSIBLE. If the fix holds up, say so plainly and name what you attacked - a clean verdict that lists the attacks that failed is the useful output, not an invented finding.

End your final message with COMPLETE: followed by a one-line verdict.
```
