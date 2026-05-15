# File Map — {{TOPIC}}

<!-- Slice: docs-private/rag/file-map.md
     Word budget: ~800 words (upper bound; under is better). Bumped from
     400 — medium repos with 15+ major dirs were forced to omit. If still
     exceeding 800, split into file-map/<top-level-dir>.md slices.
     Scope: annotated table of every major source dir. Skip generated/
     vendored/build-artifact dirs. Self-contained — an executor reading
     this slice alone should know where to look for any concern. -->

## Source pins

- `{{SOURCE_PATH_1}}` (e.g., repo root listing)
- `{{SOURCE_PATH_2}}` (e.g., `AGENTS.md` file map section)
- `{{SOURCE_PATH_3}}` (e.g., `pyproject.toml` / `package.json` for entry points)

## Map

<!-- Format: markdown table `| Area | Path | One-line note |`.
     Order: load-bearing dirs first, tests next, docs last. One row per dir
     (not per file) unless a single file is load-bearing enough to call out.
     Skip: `node_modules/`, `__pycache__/`, `build/`, `dist/`, `.venv/`,
     vendored third-party trees, generated code dirs. -->

| Area | Path | One-line note |
|------|------|---------------|
| {{AREA_1}} | `{{PATH_1}}` | {{NOTE_1}} |
| {{AREA_2}} | `{{PATH_2}}` | {{NOTE_2}} |
| {{AREA_3}} | `{{PATH_3}}` | {{NOTE_3}} |
| {{AREA_4}} | `{{PATH_4}}` | {{NOTE_4}} |
| {{AREA_5}} | `{{PATH_5}}` | {{NOTE_5}} |
| Tests | `{{TEST_PATH}}` | {{TEST_NOTE}} |
| Public docs | `{{PUBLIC_DOCS_PATH}}` | {{PUBLIC_DOCS_NOTE}} |
| Private plans | `{{PRIVATE_DOCS_PATH}}` | gitignored; plan + queue + RAG corpus live here. |

<!-- example:
| Area | Path | One-line note |
|------|------|---------------|
| Providers (engines) | `engines/` | One subdir per provider; each exposes a `provider.py` that registers intents. |
| Dispatcher | `dispatch/` | Routes `(unit, intent) -> provider`; authority matrix at `dispatch/authority.py`. |
| Canonical store | `state/` | Parquet-backed; `state/commit.py` is the sole writer (invariant). |
| Tests | `tests/test_*.py` | One file per module; mass-balance gate at `tests/test_invariants.py`. |
| Public docs | `docs/*.md` | User-facing; do not put planning notes here. |
| Private plans | `docs-private/*.md` | gitignored; plan + queue + RAG corpus live here. |
-->

## Future / planned (do not create speculatively)

<!-- Pulled from refactor-plan if one exists. List dirs the plan calls for
     but that don't exist yet, so executors don't invent them or write
     code into them prematurely. -->

- `{{FUTURE_PATH_1}}` — {{FUTURE_NOTE_1}}
- `{{FUTURE_PATH_2}}` — {{FUTURE_NOTE_2}}

## Self-check before reporting done

- [ ] Every major source dir present. Grep `ls -d */` and reconcile.
- [ ] No generated/vendored/build dirs listed.
- [ ] Notes are one line each — what lives there, not how it works.
- [ ] Each path verified to exist (or marked under "Future / planned").
- [ ] Self-contained: no "see AGENTS.md" — restate.
- [ ] Word count under budget. If over, you have too much prose in notes; tighten.
- [ ] Voice matches AGENTS.md: terse, technical.
