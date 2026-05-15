# RAG Corpus Schema — {{TOPIC}}

The init step's corpus-builder pipeline produces this structure under `docs-private/rag/`. Each slice is curated by a dedicated subagent at init time and refreshed by the corpus-drift reviewer at milestone reviews. Dispatch composition then becomes "select which slices apply to this chunk" — controller picks; subagents read.

## Directory

```
docs-private/rag/
  ├── invariants.md                   # ~300 words. Distilled from AGENTS.md hard invariants.
  │                                   # Does NOT split — if exceeding 300, tighten the slice
  │                                   # rather than fracturing the load-bearing rule set.
  ├── file-map.md                     # ~800 words. Annotated paths with one-paragraph notes per major dir.
  │                                   # 400 was too tight for medium repos (>15 major dirs); 800 covers most.
  │                                   # If exceeding 800: split into file-map/<top-level-dir>.md slices.
  ├── binding-spec/                   # Spec sliced by section, one file per intent/concern.
  │   ├── {{INTENT_OR_CONCERN_1}}.md  # ~400 words each. Self-contained for one dispatch use.
  │   ├── {{INTENT_OR_CONCERN_2}}.md
  │   └── ...
  ├── patterns/                       # Code/test conventions executors mirror.
  │   ├── {{PATTERN_1}}.md            # ~500 words. Includes a canonical example + grep pattern.
  │   ├── {{PATTERN_2}}.md
  │   └── ...
  ├── decisions.md                    # ~1500 words. Chronological cross-goal decision log.
  │                                   # Each entry: date, decision, rationale, trip-wire-if-revisit.
  │                                   # Splitting loses the chronological view; raise budget instead.
  │                                   # For projects exceeding 1500 words: split by EPOCH (decisions-pre-v1.md,
  │                                   # decisions-v1.md, etc.) rather than by topic.
  └── verification.md                 # ~700 words. Test cmds, grep patterns, mass-balance check, etc.
                                      # Per-command "expect:" line included.
                                      # The "repeated dispatch tail" — paste into every executor wrapper.
                                      # If exceeding 700, split into verification/tests.md + verification/grep-invariants.md.
```

## Slice writing rules

- **Self-contained.** Each slice is meant to be pasted into a dispatch in isolation. Don't write "see file X" — paste the relevant content.
- **Word budget pinned per slice.** Above are upper bounds. Going over signals the slice should split.
- **No editorial drift.** Slice authors extract; they don't synthesize new opinions. Decisions slice is the ONLY place opinions live, and each must cite the goal/commit that produced it.
- **Grep patterns over prose.** Where the executor will run a check, give them the exact command, not a description.
- **Voice: terse, technical, file:line refs.** Same voice as `AGENTS.md`.

## When the corpus updates

- **Init**: corpus-builder pipeline runs once, dispatched by `commands/init.md` step 3.5.
- **Per goal**: if a goal lands a decision, the controller appends to `decisions.md`. If the goal MOVED a canonical implementation, ALSO update the affected `patterns/<X>.md`. If the goal CHANGED a verification command (new test target, new grep invariant), ALSO update `verification.md`. If the goal MOVED or RENAMED files, ALSO update `file-map.md`. Cheap updates; controller-direct. Don't wait for milestone drift to discover what a single commit obviously changed.
- **Milestone**: corpus-drift reviewer runs as part of the gstack milestone fleet (`commands/execute.md` step 4). Catches the diffuse drift the per-goal updates miss (helpers lifted across multiple goals, slow file relocations, accumulated env caveats).
- **User-triggered**: `/goal-flight resume` can include a `--rag-drift-check` flag (future) to force a drift sweep.

## Why this beats inline-the-landscape

Inline-the-landscape (every dispatch reconstructs context from scratch) is correct on the context-window axis but wastes the controller's tokens repeatedly composing the same content. The corpus pattern is "compose once at init; reference by file at dispatch." Controller's context goes toward integration, requirements adjudication, and graph-orientation calls — not toward re-pasting AGENTS.md hard invariants for the 12th time.
