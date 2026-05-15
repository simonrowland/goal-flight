# Binding Spec — {{INTENT_OR_CONCERN}}

<!-- Slice: docs-private/rag/binding-spec/{{INTENT_OR_CONCERN}}.md
     Word budget: ~400 words (upper bound; under is better).
     Scope: I/O contract for ONE section of the binding spec. Self-contained
     — pasted into a dispatch in isolation, this slice alone tells the
     executor what to honor. Abridge prose if needed but keep the contract
     (inputs / outputs / authority / hard-filter) intact verbatim. -->

## Source pins

- `{{SPEC_PATH}}` §{{SECTION_NUMBER}} — {{SECTION_TITLE}}
- `{{SUPPORTING_SOURCE_1}}` (e.g., authority matrix, schema)

## Scope of this slice

<!-- One sentence: which intent or concern this slice covers, and which it
     does NOT (so the executor pulls a different slice for the rest). -->

{{ONE_SENTENCE_SCOPE}}

## Contract

### Inputs

<!-- Each input: name, type, units (if applicable), origin. Required vs
     optional. Verbatim from spec where possible. -->

- `{{INPUT_1_NAME}}` ({{INPUT_1_TYPE}}, {{INPUT_1_UNITS}}) — {{INPUT_1_ORIGIN}}. {{REQUIRED_OR_OPTIONAL_1}}.
- `{{INPUT_2_NAME}}` ({{INPUT_2_TYPE}}, {{INPUT_2_UNITS}}) — {{INPUT_2_ORIGIN}}. {{REQUIRED_OR_OPTIONAL_2}}.
- `{{INPUT_3_NAME}}` ({{INPUT_3_TYPE}}, {{INPUT_3_UNITS}}) — {{INPUT_3_ORIGIN}}. {{REQUIRED_OR_OPTIONAL_3}}.

### Outputs

- `{{OUTPUT_1_NAME}}` ({{OUTPUT_1_TYPE}}, {{OUTPUT_1_UNITS}}) — {{OUTPUT_1_MEANING}}.
- `{{OUTPUT_2_NAME}}` ({{OUTPUT_2_TYPE}}, {{OUTPUT_2_UNITS}}) — {{OUTPUT_2_MEANING}}.

### Authority

<!-- Which provider/module is authoritative for this intent. If the
     authority matrix is the source, paste the relevant row. -->

- Authoritative provider: `{{AUTHORITATIVE_PROVIDER}}` (per `{{AUTHORITY_MATRIX_PATH}}`).
- Fallbacks (in order): {{FALLBACK_LIST_OR_NONE}}.
- Never falls back silently — failure raises `{{FAILURE_EXCEPTION}}`.

### Hard filter

<!-- The pre-conditions the executor checks before invoking. Paste verbatim
     from spec. If the spec has prose, distill to predicates. -->

- {{HARD_FILTER_PREDICATE_1}}
- {{HARD_FILTER_PREDICATE_2}}
- {{HARD_FILTER_PREDICATE_3}}

<!-- example:
### Inputs
- `composition` (dict[str, float], mass fractions, sum=1.0) — from `state/snapshot.py::current`. Required.
- `temperature_K` (float, kelvin, >0) — from caller. Required.
- `pressure_bar` (float, bar, >0) — from caller. Optional; defaults to 1.0.

### Outputs
- `liquidus_T_K` (float, kelvin) — silicate liquidus temperature.
- `liquid_composition` (dict[str, float]) — equilibrium liquid at liquidus.

### Authority
- Authoritative provider: `engines/alphamelts/provider.py` (per `dispatch/authority.py:42`).
- Fallbacks: none. Failure raises `ProviderUnavailable`.

### Hard filter
- All major oxides (SiO2, Al2O3, FeO, MgO, CaO) present in composition.
- 800 < temperature_K < 3000.
- pressure_bar within calibrated range [0.001, 50].
-->

## Self-check before reporting done

- [ ] Contract (inputs / outputs / authority / hard-filter) extracted verbatim where possible.
- [ ] Abridgement only of prose context, never of contract terms.
- [ ] Units stated for every numeric field. No bare floats.
- [ ] Authority cites the matrix file:line.
- [ ] Self-contained — no "see spec §N" without the content also pasted.
- [ ] Word count under budget. If over, the spec section itself is too big to slice cleanly; raise as a P0 finding instead of truncating contract.
- [ ] Voice matches AGENTS.md: terse, technical, file:line refs.
