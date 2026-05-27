# tests

Bash and Python test harness for goal-flight's scripts and templates. Run from the
skill root:

```bash
bash tests/run.sh
```

Or run an individual test:

```bash
bash tests/bash/test-install-codex-overrides.sh
python3 tests/python/test_goalflight_procedural.py
```

## Layout

| Path | Purpose |
| --- | --- |
| `run.sh` | Discovers and runs all hermetic suites |
| `bash/test-*.sh` | Installers, adapters, host helpers, guards |
| `python/test_*.py` | ACP client, procedural runtime, fleet logic |
| `fixtures/` | Shared JSON/Python fixtures for both suites |
| `manual/` | Live probes (not part of `run.sh`) |

`run.sh` discovers every `bash/test-*.sh` and `python/test_*.py`, runs each, and
reports a pass/fail tally. Exit code = number of failed tests.

## Sandboxing

Tests that touch user config (`~/.codex/config.toml`, etc.) sandbox
`$HOME` to a tempdir. The real user config is NEVER modified, even on
failure. The sandbox auto-cleans via the test's `trap`.

## Adding a bash test

Create `bash/test-<feature>.sh`, make it executable, and ensure:

1. It exits non-zero on any assertion failure.
2. It does not touch the real `$HOME` — use the `mk_sandbox` pattern in
   `bash/test-install-codex-overrides.sh` as a template.
3. It uses a `trap 'rm -rf "$TMPROOT"' EXIT` so tempdirs clean up.
4. Print one line per assertion: `testN pass: <what>` or `testN FAIL: <what>`.
5. Resolve repo root with `REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"`.

## What's tested

- `bash/test-install-codex-overrides.sh` — the codex trust registration script:
  fresh-state behaviour, idempotency, `--check`, `--no-project-mirror`,
  pre-existing project mirror handling.

## What's NOT tested (yet)

- Template substitution (`templates/*.tpl` `{{VAR}}` replacement) — manual
  inspection at init time.
- Dispatch wrapper rendering — exercised by `commands/validate-dispatch.md`
  at the per-call level, not unit-tested here.
- The actual `/goal-flight init` / `decompose-plan` / `execute` end-to-end
  flow — would require a synthetic project + Claude Code session simulation.
  Tracked as future work; defer until usage justifies the harness cost.
