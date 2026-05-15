# Verification — {{TOPIC}}

<!-- Slice: docs-private/rag/verification.md
     Word budget: ~700 words (upper bound; under is better).
     Scope: the "repeated dispatch tail" — pasted into every executor
     wrapper. Test commands, grep patterns, mass-balance checks, etc.
     Each command has a one-line "expect:" describing pass criterion.
     Self-contained — paste-ready. If exceeding 700, split into
     verification/tests.md + verification/grep-invariants.md. -->

## Source pins

- `{{SOURCE_PATH_1}}` (e.g., `AGENTS.md` Run + test section)
- `{{SOURCE_PATH_2}}` (e.g., `tests/test_invariants.py`)
- `{{SOURCE_PATH_3}}` (e.g., `pyproject.toml` test runner config)
- `{{SOURCE_PATH_4}}` (e.g., `Makefile` / `package.json` scripts)

## Run before reporting done

<!-- Format: numbered list. Each command on its own fenced block, followed
     by one-line `**Expect:**`. Commands are paste-ready — assume the cwd
     is the repo root. -->

1. **Suite green.**
   ```bash
   {{TEST_COMMAND}}
   ```
   **Expect:** {{TEST_EXPECTATION}}.

2. **Invariant gate.**
   ```bash
   {{INVARIANT_TEST_COMMAND}}
   ```
   **Expect:** {{INVARIANT_TEST_EXPECTATION}}.

3. **Mutation purity grep.**
   ```bash
   {{MUTATION_GREP_COMMAND}}
   ```
   **Expect:** {{MUTATION_GREP_EXPECTATION}}.

4. **Authority/scope grep.**
   ```bash
   {{AUTHORITY_GREP_COMMAND}}
   ```
   **Expect:** {{AUTHORITY_GREP_EXPECTATION}}.

5. **{{DOMAIN_BALANCE_CHECK_NAME}}.**
   ```bash
   {{DOMAIN_BALANCE_COMMAND}}
   ```
   **Expect:** {{DOMAIN_BALANCE_EXPECTATION}}.

6. **Lint / type-check.**
   ```bash
   {{LINT_COMMAND}}
   ```
   **Expect:** {{LINT_EXPECTATION}}.

7. **No emoji in diff.**
   ```bash
   {{EMOJI_CHECK_COMMAND}}
   ```
   **Expect:** {{EMOJI_CHECK_EXPECTATION}}.

<!-- example:
1. **Suite green.**
   ```bash
   pytest -q
   ```
   **Expect:** all previously passing tests still pass; new tests for this chunk pass.

3. **Mutation purity grep.**
   ```bash
   rg "canonical_store\.write" --type py | rg -v "^engines/commit\.py"
   ```
   **Expect:** zero matches. Only `engines/commit.py` writes to the canonical store.

5. **Mass balance recompute.**
   ```bash
   python -m scripts.mass_balance --fixture tests/fixtures/cohort-3.json
   ```
   **Expect:** closure delta < 1e-9 kg on every step.

7. **No emoji in diff.**
   ```bash
   git diff main...HEAD | rg "[\U0001F300-\U0001FAFF]"
   ```
   **Expect:** no matches.
-->

## Self-check before reporting done

- [ ] Every command is paste-ready — copy/paste into a shell at repo root and it runs.
- [ ] Each command has one-line `**Expect:**`. No "should probably pass" prose.
- [ ] You ran each command yourself (per rag-slice-builder.md rule 4 — verification-
      of-claims relaxes self-containment) and confirmed the expectation holds at HEAD.
- [ ] Greps are anchored (line-start regex, `--type` filters, path filters) so they
      don't false-positive on docs/fixtures.
- [ ] No "see test file X" — the command itself is the artifact.
- [ ] Self-contained: this slice is pasted into EVERY executor wrapper as the
      "repeated dispatch tail". It must work without any other context.
- [ ] Word count under budget. If over: split into `verification/tests.md`
      (numbered commands 1-N for test runs) + `verification/grep-invariants.md`
      (numbered commands for static checks).
- [ ] Voice matches AGENTS.md: terse, technical, file:line refs.
