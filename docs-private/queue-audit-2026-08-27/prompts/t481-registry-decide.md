# Pinned prompt — `t481-registry-decide`

- source: `prompt-file`
- prompt-file: `/private/tmp/claude-501/-Users-simonrowland-Library-CloudStorage-Dropbox-Starship-Mission-Design-Regolith-Processing-regolith-pyrolysis-simulator/70f1d3c1-4b7e-4455-bf2d-31eabd2ee767/scratchpad/t481.prompt` (EXISTS on disk)
- pinned_at: 2026-08-27T14:54:06.319466+00:00

```
Decide the fate of a registry this project built completely and then never read. Repo root: /Users/simonrowland/Repos/rps-t481 (branch work-t481, HEAD 236553f9). Read AGENTS.md and CLAUDE.md FIRST.

t-481 is a scout finding: the project has a PHASE-AWARE VOLATILE-PROPERTY REGISTRY that is FULLY BUILT AND COMPLETELY EMPTY. This is the SC-50 consumption lens — produced-but-never-read is a dead feature, and a dead feature is worse than a missing one because it looks like coverage.

THE DECISION IS FILL OR RETIRE, and it must be made on evidence, not preference.

STEP 1 — FIND IT AND ESTABLISH THE FACTS. Locate the registry (schema, storage, accessors). Then answer precisely: is it empty of DATA, empty of CONSUMERS, or both? Those are different diseases. Trace every reader. If something reads it and gets nothing, that consumer is currently running on a silent default — name it and say what the default is, because that is a live correctness question, not a cleanup one.

STEP 2 — ESTABLISH WHAT IT WOULD BE FOR. Read the design intent from wherever it is recorded. A phase-aware volatile-property registry in THIS project would plausibly serve the condensation train (which phase a species is in when it condenses), the wall-deposit model, or the rail's condensed-form gate. Determine which, if any, actually has a hole the registry would fill. ★ If the need it was built for has since been met by something else, that is the retire case and it is a perfectly good answer.

STEP 3 — RECOMMEND, WITH THE COST. If FILL: what data, from where, and how much work — and is the data even available, or would filling it require measurements nobody has? If RETIRE: what breaks, what silently changes, and what should be recorded so the next person does not rebuild it in ignorance. ★ A registry retired without a note explaining WHY gets rebuilt within a year.

DO NOT fill it with plausible values to make it look alive. An empty registry is honest; a registry full of invented numbers is the worst outcome available here and would poison anything that later starts reading it.
DO NOT delete anything in this task — recommend, and implement only if the answer is unambiguous AND golden-neutral. Anything that changes a number or a valid-input behaviour is batched for the gated regrind instead.

TEST: cd /Users/simonrowland/Repos/rps-t481 && "/Users/simonrowland/Library/CloudStorage/Dropbox/Starship Mission Design/Regolith Processing/regolith-pyrolysis-simulator/.venv/bin/python" -m pytest tests/ -q -p no:randomly -n0 -k "volatile or registry or phase"
Never pipe pytest through head or tail. Do NOT run git checkout, git stash, git commit or git add.
Report to /Users/simonrowland/Repos/rps-t481/docs-private/research/2026-08-26-t481/findings.md.
End with COMPLETE: and one line: FILL or RETIRE, plus the single strongest reason.

```
