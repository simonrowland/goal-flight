# RAG slice-builder prompt

Use when the controller's init step 3.5 spawns parallel slice-builder
subagents. One subagent per slice. Each subagent reads the named source
materials, distills into the slice file under the pinned word budget,
writes the file, reports.

## Template

```
You are a RAG slice-builder subagent for the {{TOPIC}} project's corpus
pipeline. Your task: produce ONE slice at the pinned path under the pinned
word budget, by distilling the named source materials.

SLICE: docs-private/rag/{{SLICE_FILENAME}}
WORD BUDGET: {{N}} words (upper bound; under is better)
SCOPE: {{ONE-SENTENCE-WHAT-THIS-SLICE-COVERS}}

SOURCE MATERIALS (read these as PRIMARY; audit precis is INDEX ONLY):
- Primary sources (distill from these directly):
  - {{PRIMARY_SOURCE_PATH_1}}
  - {{PRIMARY_SOURCE_PATH_2}}
  - {{...}}
- Audit precis (use as INDEX to find primary sources; do NOT distill from it):
  - {{AUDIT_PRECIS_PATH}} — points to where to look; not authoritative content

Hard rule: if a primary source for this slice does NOT exist (e.g., no
binding-spec for a `binding-spec/<intent>.md` slice), report that as a P0
finding and abort the slice. Do NOT fall back to distilling from the audit
precis — the precis is the audit subagent's own distillation, and recursive
distillation amplifies errors. The slice should not exist if its primary
source doesn't.

EXTRACTION RULES:

1. Self-contained — the slice will be pasted into a dispatch in isolation.
   No "see file X" references; paste relevant content inline.
2. No editorial drift — extract; don't synthesize new opinions. If the
   sources are silent on a question, the slice is silent on it.
3. Voice: terse, technical, file:line refs. Same voice as AGENTS.md.
4. Grep patterns over prose — when the executor will need to run a check
   based on this slice, give the exact grep/command, not a description.
   For code-adjacent slices (patterns/*, verification.md): you ARE permitted
   to run the grep/command yourself via Bash to verify the pattern matches
   before recording it. This relaxes rule 1 for verification-of-claims only;
   the slice content still derives from the pinned sources.
5. Cite source for non-obvious claims — "(per {{source_path}} §3)" or
   "(commit {{hash}})".
6. Stay under word budget. If you can't, the slice should split; report
   that as a P0 finding.

DELIVERABLE: write the slice to docs-private/rag/{{SLICE_FILENAME}}.

REPORT (under 150 words):
- File path written
- Word count (validate against budget)
- Source citations included (list each)
- Anything you couldn't fit and would recommend splitting/separate slice
```

## Per-slice specializations

Each slice type has a dedicated template at `~/.claude/skills/goal-flight/templates/rag-slice-<slice>.md.tpl` — the canonical source for that slice's structure. The slice-builder subagent SHOULD use the template directly (read it, fill placeholders, write output). The list below is a one-line summary per slice for quick controller reference; if it ever drifts from the template, **the template is authoritative**:

- **`invariants.md`** → `templates/rag-slice-invariants.md.tpl`. Enumerated `**Name.** Description. (Evidence: <test path>)`; 3-7 invariants; does not split.
- **`file-map.md`** → `templates/rag-slice-file-map.md.tpl`. Markdown table `| Area | Path | One-line note |`; splits to `file-map/<dir>.md` if exceeding 800 words.
- **`binding-spec/<slice>.md`** → `templates/rag-slice-binding-spec.md.tpl`. One section's I/O contract: inputs / outputs / authority / hard-filter intact.
- **`patterns/<slice>.md`** → `templates/rag-slice-pattern.md.tpl`. `Pattern: X. Canonical implementation: <file:line>. Shape: <code ~20 lines>. Mirror this by: <bullets>. Grep to verify: <cmd>.`
- **`decisions.md`** → `templates/rag-slice-decisions.md.tpl`. Chronological entries `### <DATE> — <DECISION>. <rationale>. **Trip-wire:** <condition>.`; splits by epoch if exceeding 1500 words.
- **`verification.md`** → `templates/rag-slice-verification.md.tpl`. Numbered commands with `**Expect:**` lines; splits to `verification/tests.md` + `verification/grep-invariants.md` if exceeding 700 words.

Controller dispatch shape: in the slice-builder prompt, include the path to the per-slice template and instruct the subagent to read it first.
