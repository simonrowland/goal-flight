# Invariants — {{TOPIC}}

<!-- Slice: docs-private/rag/invariants.md
     Word budget: ~300 words (upper bound; under is better).
     Scope: hard invariants distilled from AGENTS.md + tests. The smallest set
     a reviewer would reject a PR for. Self-contained — paste-ready into a
     dispatch. 3-7 invariants max; more means the slice should split or some
     are not really hard invariants. -->

## Source pins

- `{{SOURCE_PATH_1}}` (e.g., `AGENTS.md` §Hard invariants)
- `{{SOURCE_PATH_2}}` (e.g., `tests/test_invariants.py`)
- `{{SOURCE_PATH_3}}`

## Invariants

<!-- Format per entry: **Name.** One-line description. (Evidence: <test path>)
     The name must be greppable. The evidence line must be a real test path or
     a real grep command that proves the invariant is enforced. -->

1. **{{INVARIANT_1_NAME}}.** {{INVARIANT_1_DESCRIPTION}} (Evidence: `{{INVARIANT_1_EVIDENCE}}`)
2. **{{INVARIANT_2_NAME}}.** {{INVARIANT_2_DESCRIPTION}} (Evidence: `{{INVARIANT_2_EVIDENCE}}`)
3. **{{INVARIANT_3_NAME}}.** {{INVARIANT_3_DESCRIPTION}} (Evidence: `{{INVARIANT_3_EVIDENCE}}`)
4. **{{INVARIANT_4_NAME}}.** {{INVARIANT_4_DESCRIPTION}} (Evidence: `{{INVARIANT_4_EVIDENCE}}`)
5. **{{INVARIANT_5_NAME}}.** {{INVARIANT_5_DESCRIPTION}} (Evidence: `{{INVARIANT_5_EVIDENCE}}`)

<!-- example:
1. **Mass balance closes.** Every state mutation closes the species
   conservation equation to within 1e-9 kg. (Evidence: `tests/test_mass_balance.py::test_closure`)
2. **Single writer to canonical store.** Only `engines/commit.py` writes to
   `state/canonical_store.parquet`. (Evidence: `rg "canonical_store\.write" --type py`)
3. **No silent fallback.** Provider failure on a requested unit raises
   `ProviderUnavailable`; never substitutes a less-trusted provider.
   (Evidence: `tests/test_provider_routing.py::test_no_silent_fallback`)
-->

## Self-check before reporting done

- [ ] 3-7 invariants — no more, no less. If you have more than 7, some aren't hard.
- [ ] Each entry is one line. No multi-line prose.
- [ ] Each evidence ref is a real path/command, not a description. Verified it exists.
- [ ] Names are greppable terms (e.g., `Mass balance closes`, not `Things should sum`).
- [ ] No editorial drift — every invariant is anchored in AGENTS.md or a guard test.
- [ ] Self-contained: a fresh executor can read this slice alone and know what
      not to break. No "see file X" — paste the rule.
- [ ] Word count under budget. If over, split is not allowed for this slice; tighten.
- [ ] Voice matches AGENTS.md: terse, technical, file:line refs.
