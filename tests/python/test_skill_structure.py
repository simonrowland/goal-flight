#!/usr/bin/env python3
"""Hermetic structural regression tests for the Goal Flight skill distillation.

Known current gaps surfaced while implementing chunk-5:

- chunk-5.1 skill-distillation catch-up must align these Golden Master anchors
  in SKILL.md: controller-probe-runner-portability,
  read-skill-end-to-end-behaviour, gstack-review-and-challenge-canonical,
  rubric-before-wave, concern-diverse-review.
- chunk-5.1 protocol wording cleanup must remove the tracked "Agent tool"
  literal from protocols/legacy/bash-tail.md and `request_user_input` from
  protocols/dispatch-routing.md.

The allowlists below are intentionally exact. Unknown drift fails. If any known
gap is fixed, this test fails so the allowlist is removed and the invariant
becomes strict for that entry.
"""

from __future__ import annotations

import ast
from collections import Counter
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

REQUIRED_TOP_LEVEL_FIELDS = {
    "id",
    "name",
    "category",
    "controller_does",
    "failure_mode",
    "skill_md_compressed_form",
    "verifier",
    "provenance",
    "severity",
    "last_reviewed_commit",
}

EXPECTED_SCHEMA_FIELDS = REQUIRED_TOP_LEVEL_FIELDS | {"max_skill_lines"}

REQUIRED_NESTED_FIELDS = {
    "skill_md_compressed_form": {"kind", "pattern", "max_section_lines"},
    "verifier": {"kind", "id"},
    "provenance": {"sources", "r_numbers"},
}

EXPECTED_NESTED_SCHEMA_FIELDS = {
    "skill_md_compressed_form": {"kind", "pattern", "max_section_lines"},
    "verifier": {"kind", "id"},
    "provenance": {"sources", "r_numbers"},
}

SEVERITIES = {"high", "med", "low"}
VERIFIER_KINDS = {"textual-invariant", "behaviour-scenario", "runtime-assertion", "manual"}
COMPRESSED_FORM_KINDS = {"literal", "regex"}

EXPECTED_CATEGORIES = {
    "skill-load-and-order",
    "compaction-and-resume",
    "review-discipline",
    "chat-discipline",
    "dispatch-discipline",
    "autonomous-throughput-and-status",
    "capacity-and-rate-limits",
    "worker-markers",
    "verification-first-dispatch",
    "test-gate",
    "push-discipline",
    "trigger-and-codename-hygiene",
    "native-vs-non-native",
    "worker-routing-defaults",
    "state-layers",
    "context-discipline",
    "do-not",
}

EXPECTED_VALIDATION = {
    "total_entry_count": {"min": 50, "max": 120},
    "per_category_min": {
        "skill-load-and-order": 1,
        "compaction-and-resume": 1,
        "review-discipline": 3,
        "chat-discipline": 1,
        "dispatch-discipline": 1,
        "autonomous-throughput-and-status": 2,
        "capacity-and-rate-limits": 2,
        "worker-markers": 1,
        "verification-first-dispatch": 1,
        "test-gate": 1,
        "push-discipline": 1,
        "trigger-and-codename-hygiene": 1,
        "native-vs-non-native": 1,
        "worker-routing-defaults": 1,
        "state-layers": 1,
        "context-discipline": 1,
        "do-not": 1,
    },
}

EXPECTED_VALIDATION_BLOCK = """validation:
  total_entry_count:
    min: 50
    max: 120
    constraint: empirical band; adjust if structural changes shift the count
  per_category_min:
    skill-load-and-order: 1
    compaction-and-resume: 1
    review-discipline: 3
    chat-discipline: 1
    dispatch-discipline: 1
    autonomous-throughput-and-status: 2
    capacity-and-rate-limits: 2
    worker-markers: 1
    verification-first-dispatch: 1
    test-gate: 1
    push-discipline: 1
    trigger-and-codename-hygiene: 1
    native-vs-non-native: 1
    worker-routing-defaults: 1
    state-layers: 1
    context-discipline: 1
    do-not: 1
  unique_id: true
  anchor_uniqueness:
    constraint: no two entries' skill_md_compressed_form.pattern share a substring \u226580 chars
  provenance_path_exists:
    constraint: every path in provenance.sources must exist in repo"""

EXPECTED_VALIDATION_KEYS = {
    "total_entry_count",
    "per_category_min",
    "unique_id",
    "anchor_uniqueness",
    "provenance_path_exists",
}

EXPECTED_VALIDATION_NESTED_KEYS = {
    "total_entry_count": {"min", "max", "constraint"},
    "per_category_min": EXPECTED_CATEGORIES,
    "unique_id": set(),
    "anchor_uniqueness": {"constraint"},
    "provenance_path_exists": {"constraint"},
}

EXPECTED_ENTRY_IDS = {
    "skill-load-order-mandatory",
    "status-without-asking-hook-welcome",
    "compaction-skill-reload-scoped",
    "mid-session-ask-append-to-goal-queue",
    "no-blocking-cursor-task-worker",
    "autonomous-throughput-commit-as-complete",
    "autoreview-complementary-not-default",
    "user-status-cadence-15min",
    "milestone-standalone-protocol",
    "review-layers-three-distinct",
    "gstack-default-review-chunk",
    "skill-organization-navigation-map",
    "maintainer-autoreview-env-optional",
    "wave2-scenarios-registered",
    "controller-probe-runner-portability",
    "skill-invariants-textual-guard",
    "read-skill-end-to-end-behaviour",
    "compaction-reload-skill-behaviour",
    "review-flight-at-completion-behaviour",
    "per-host-pointer-for-non-native",
    "controller-chat-is-requirements-not-inline-editor",
    "gstack-review-and-challenge-canonical",
    "tests-run-green-before-commit",
    "push-explicit-permission-only",
    "executor-self-review-7categories",
    "fabricated-approval-rejected",
    "capacity-check-before-spawn",
    "worker-markers-status-path-required",
    "state-layers-separated",
    "context-mode-for-analysis",
    "trigger-codename-hygiene",
    "no-print-mode-review-for-live-probes",
    "plan-before-inline-edits",
    "background-tests-pending",
    "reviewer-misses-regression-tests",
    "classify-acp-failure-layer",
    "controller-direct-plan-marked",
    "question-prep-before-ask",
    "rubric-before-wave",
    "concern-diverse-review",
    "cross-slice-consolidation",
    "agents-md-diff-only",
    "build-corpus-eagerly",
    "corpus-primary-sources",
    "corpus-init-not-inline",
    "typed-dispatch-wrappers",
    "dispatch-wrapper-five-layers",
    "self-review-specialize",
    "parallel-fix-forbid-lists",
    "split-large-chunk-scope",
    "worker-context-optional",
    "source-truth-self-consistency",
    "bounded-dispatch-timeouts",
    "heartbeats-file-pull-model",
    "noninteractive-mcp-preflight",
    "remote-worker-designated-controller",
    "single-status-plane",
    "user-need-controller-relay",
    "phase-gate-before-remote-dispatch",
    "pidfile-isolation-per-controller",
    "learned-rate-pressure-ledger",
    "controller-provider-conservative",
    "controller-readiness-probes-before-dispatch",
    "controller-live-gate-supported-ready",
    "worker-live-gate-supported-ready-verified",
    "discovery-probe-budget-bounded",
    "discovery-probes-no-network-model",
    "permission-forbidden-shell-families",
    "auto-approve-scans-strict-fail",
    "irreversible-ops-user-gated",
    "secrets-stay-out-of-probes",
    "forbidden-exec-args-rejected-everywhere",
    "risky-exec-args-need-justification",
    "abstract-tool-map-host-specific",
    "same-provider-policy-routing",
    "repo-files-canonical-memory-backend",
    "memory-writeback-lock-required",
    "status-contract-heartbeats-required",
    "stale-after-threshold-enforced",
    "terminal-states-closed-set",
    "marker-namespace-grammar",
    "default-agent-caps-enforced",
    "capacity-acquire-wait-reasons",
    "terminal-lease-lifecycle-pruned",
    "ledger-pid-plus-process-identity",
    "acp-permit-file-ipc-contract",
    "install-actions-user-gate-backups",
}

KNOWN_SKILL_ALIGNMENT_GAPS = {
    "controller-probe-runner-portability": {
        "fix_chunk": "chunk-5.1 skill-distillation catch-up",
        "kind": "literal",
        "pattern": "Controller behaviour probes run through portable host adapters, not host-specific print-mode shortcuts",
        "current_substitute": "Controller behaviour probes run through portable host adapters, not\nhost-specific print-mode shortcuts.",
        "current_substitute_section": "## Per-host pointers",
    },
    "read-skill-end-to-end-behaviour": {
        "fix_chunk": "chunk-5.1 skill-distillation catch-up",
        "kind": "literal",
        "pattern": "Read this skill end-to-end, including Worker Routing, State, and Context Discipline",
        # 2026-05-28: preamble promoted to a bold callout per AUI surface
        # audit; substitute wording updated to match while preserving the
        # canonical "Read this skill end-to-end" phrase verbatim.
        "current_substitute": "Read this skill end-to-end before acting** \u2014 including Worker\n> Routing, State, Context Discipline, and Do Not.",
        "current_substitute_section": "preamble",
    },
    "gstack-review-and-challenge-canonical": {
        "fix_chunk": "chunk-5.1 skill-distillation catch-up",
        "kind": "literal",
        "pattern": "Reviews go through gstack `/review` and `/challenge`; do not hand-roll review prompts",
        "current_substitute": "Companion tools: gstack `/review` is the canonical chunk reviewer; gstack\n`/challenge` is the adversarial frame; fall back to `prompts/gstack-*.md` only\nwhen gstack is absent.",
        "current_substitute_section": "## Per-host pointers",
    },
    "rubric-before-wave": {
        "fix_chunk": "chunk-5.1 skill-distillation catch-up",
        "kind": "literal",
        "pattern": "Write review rubrics before first wave dispatch",
        "current_substitute": "Write review\nrubrics before first wave dispatch.",
        "current_substitute_section": "## Review layers",
    },
    "concern-diverse-review": {
        "fix_chunk": "chunk-5.1 skill-distillation catch-up",
        "kind": "literal",
        "pattern": "Diversify reviewer concern, not just model",
        "current_substitute": "Diversify reviewer concern, not just\nmodel.",
        "current_substitute_section": "## Review layers",
    },
}

KNOWN_PROTOCOL_LITERAL_GAPS = {
    ("protocols/legacy/bash-tail.md", "Agent tool"): {
        "fix_chunk": "chunk-5.1 protocol-legacy wording cleanup",
        "line_number": 96,
        "line": "Prefer the Agent tool for sub-billed dispatches \u2014 Agent-tool subagents",
    },
    ("protocols/dispatch-routing.md", "request_user_input"): {
        "fix_chunk": "chunk-5.1 protocol-elicitation wording cleanup",
        "line_number": 181,
        "line": "3. **MCP elicitation** (`request_user_input`) \u2014 raised by tools like context-mode's",
    },
}

KNOWN_MAX_SECTION_LINE_GAPS = {
    "split-large-chunk-scope": {
        "fix_chunk": "chunk-5.1 skill-section-budget catch-up",
        "max_section_lines": 20,
        "actual_content_lines": 21,
        "section_heading": "## Dispatch Model",
    },
    "default-agent-caps-enforced": {
        "fix_chunk": "chunk-X budget realignment after AUI hardening",
        "max_section_lines": 25,
        "actual_content_lines": 40,
        "section_heading": "## Worker Routing",
    },
    # 2026-05-28 worker-reliability hardening overruns. Each was driven by
    # the AUI surface audit + sweep B/C/D findings:
    # - preamble: bold callout + activation check directive (skill_load_order)
    # - ## State: 7-step canonical post-compaction reload order (8 lines
    #   added; previously implicit, scattered across protocols/state-handoff.md)
    # - ## Hard Invariants: worked replacement commands for "no tail -f" +
    #   commit-guard pointer + permission-pattern warning (4 lines added)
    # - ## Review layers: < /dev/null context fence + bypass-flag scope
    #   note (4 lines added)
    # The Golden Master budgets stay at their original values pending a
    # formal realignment in a follow-up chunk; this allowlist makes the
    # overruns explicit + reviewable rather than silent.
    "skill-load-order-mandatory": {
        "fix_chunk": "chunk-X budget realignment after AUI hardening",
        "max_section_lines": 10,
        "actual_content_lines": 15,
        "section_heading": "preamble",
    },
    "user-status-cadence-15min": {
        "fix_chunk": "chunk-X budget realignment after AUI hardening",
        "max_section_lines": 20,
        "actual_content_lines": 22,
        "section_heading": "## Hard Invariants",
    },
    "reviewer-misses-regression-tests": {
        "fix_chunk": "chunk-X budget realignment after AUI hardening",
        "max_section_lines": 25,
        "actual_content_lines": 28,
        "section_heading": "## Review layers",
    },
    "state-layers-separated": {
        "fix_chunk": "chunk-X budget realignment after AUI hardening",
        "max_section_lines": 25,
        "actual_content_lines": 35,
        "section_heading": "## State",
    },
    "repo-files-canonical-memory-backend": {
        "fix_chunk": "chunk-X budget realignment after AUI hardening",
        "max_section_lines": 25,
        "actual_content_lines": 35,
        "section_heading": "## State",
    },
    "single-status-plane": {
        "fix_chunk": "chunk-X budget realignment after AUI hardening",
        "max_section_lines": 25,
        "actual_content_lines": 35,
        "section_heading": "## State",
    },
    "ledger-pid-plus-process-identity": {
        "fix_chunk": "chunk-X budget realignment after AUI hardening",
        "max_section_lines": 25,
        "actual_content_lines": 35,
        "section_heading": "## State",
    },
    "memory-writeback-lock-required": {
        "fix_chunk": "chunk-X budget realignment after AUI hardening",
        "max_section_lines": 25,
        "actual_content_lines": 35,
        "section_heading": "## State",
    },
    "classify-acp-failure-layer": {
        "fix_chunk": "chunk-X budget realignment after AUI hardening",
        "max_section_lines": 25,
        "actual_content_lines": 35,
        "section_heading": "## State",
    },
    "remote-worker-designated-controller": {
        "fix_chunk": "chunk-X budget realignment after AUI hardening",
        "max_section_lines": 25,
        "actual_content_lines": 35,
        "section_heading": "## State",
    },
    "pidfile-isolation-per-controller": {
        "fix_chunk": "chunk-X budget realignment after AUI hardening",
        "max_section_lines": 20,
        "actual_content_lines": 35,
        "section_heading": "## State",
    },
    "agents-md-diff-only": {
        "fix_chunk": "chunk-X budget realignment after AUI hardening",
        "max_section_lines": 20,
        "actual_content_lines": 35,
        "section_heading": "## State",
    },
}

KNOWN_ENTRY_SCHEMA_EXTENSIONS = {
    "skill-load-order-mandatory": {
        "top": {"notes"},
        "nested": {
            "skill_md_compressed_form": {"note"},
            "verifier": {"fallback"},
        },
    },
}


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def read_repo_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def split_frontmatter(markdown: str) -> tuple[str, str]:
    lines = markdown.splitlines()
    assert_true("frontmatter opens with ---", bool(lines) and lines[0].strip() == "---")
    close = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            close = idx
            break
    assert_true("frontmatter closes with ---", close is not None)
    assert close is not None
    return "\n".join(lines[1:close]), "\n".join(lines[close + 1 :])


def parse_scalar(raw_value: str) -> Any:
    value = raw_value.strip()
    if value == "":
        return ""
    if value.startswith("`"):
        match = re.match(r"`([^`]+)`", value)
        if match:
            return match.group(1)
    if value.startswith("[") and value.endswith("]"):
        inside = value[1:-1].strip()
        if not inside:
            return []
        return [item.strip() for item in inside.split(",")]
    if value[0] in {"'", '"'}:
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return value.strip("\"'")
    return value


def parse_frontmatter(frontmatter: str) -> dict[str, Any]:
    lines = frontmatter.splitlines()
    schema_fields: list[str] = []
    required_fields: list[str] = []
    nested_schema_fields: dict[str, list[str]] = {}
    category_enum: list[str] = []
    validation_keys: list[str] = []
    validation_nested_keys: dict[str, list[str]] = {}
    validation_block_lines: list[str] = []
    validation: dict[str, Any] = {
        "total_entry_count": {},
        "per_category_min": {},
    }

    in_validation = False
    current_validation_key: str | None = None
    for line in lines:
        if line == "validation:":
            in_validation = True
            validation_block_lines.append(line)
            continue
        if not in_validation:
            continue
        if line and not line.startswith(" "):
            in_validation = False
            current_validation_key = None
            continue
        validation_block_lines.append(line)
        validation_key_match = re.match(r"^  ([A-Za-z0-9_-]+):(?:\s+.*)?$", line)
        if validation_key_match:
            current_validation_key = validation_key_match.group(1)
            validation_keys.append(current_validation_key)
            validation_nested_keys[current_validation_key] = []
            continue
        nested_validation_match = re.match(r"^    ([A-Za-z0-9_-]+):", line)
        if nested_validation_match and current_validation_key is not None:
            validation_nested_keys[current_validation_key].append(nested_validation_match.group(1))

    idx = 0
    while idx < len(lines):
        line = lines[idx]

        top_field = re.match(r"^  ([A-Za-z0-9_-]+):$", line)
        if top_field and top_field.group(1) != "fields":
            field_name = top_field.group(1)
            schema_fields.append(field_name)
            block: list[str] = []
            idx += 1
            while idx < len(lines) and not re.match(r"^(  [A-Za-z0-9_-]+:|validation:)", lines[idx]):
                block.append(lines[idx])
                idx += 1
            if any(part.strip() == "required: true" for part in block):
                required_fields.append(field_name)
            nested_fields = [match.group(1) for part in block if (match := re.match(r"^      ([A-Za-z0-9_-]+):$", part))]
            if nested_fields:
                nested_schema_fields[field_name] = nested_fields
            if field_name == "category":
                for block_idx, block_line in enumerate(block):
                    if block_line.strip() == "enum:":
                        for enum_line in block[block_idx + 1 :]:
                            enum_match = re.match(r"^      - (.+)$", enum_line)
                            if enum_match:
                                category_enum.append(enum_match.group(1).strip())
                            elif enum_line.strip() and len(enum_line) - len(enum_line.lstrip(" ")) <= 4:
                                break
            continue

        if line == "validation:":
            idx += 1
            while idx < len(lines):
                total_match = re.match(r"^  total_entry_count:$", lines[idx])
                per_category_match = re.match(r"^  per_category_min:$", lines[idx])
                if total_match:
                    idx += 1
                    while idx < len(lines) and lines[idx].startswith("    "):
                        count_match = re.match(r"^    (min|max): ([0-9]+)$", lines[idx])
                        if count_match:
                            validation["total_entry_count"][count_match.group(1)] = int(count_match.group(2))
                        idx += 1
                    continue
                if per_category_match:
                    idx += 1
                    while idx < len(lines) and lines[idx].startswith("    "):
                        cat_match = re.match(r"^    ([a-z0-9-]+): ([0-9]+)$", lines[idx])
                        if cat_match:
                            validation["per_category_min"][cat_match.group(1)] = int(cat_match.group(2))
                        idx += 1
                    continue
                idx += 1
            continue

        idx += 1

    return {
        "schema_fields": schema_fields,
        "required_fields": required_fields,
        "nested_schema_fields": nested_schema_fields,
        "category_enum": category_enum,
        "validation_block": "\n".join(validation_block_lines),
        "validation_keys": validation_keys,
        "validation_nested_keys": validation_nested_keys,
        "validation": validation,
    }


def parse_entries(body: str) -> list[dict[str, Any]]:
    headers = list(re.finditer(r"^### Entry: ([^\n]+)$", body, flags=re.MULTILINE))
    entries: list[dict[str, Any]] = []
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(body)
        block = body[header.start() : end]
        entry: dict[str, Any] = {"entry_heading": header.group(1).strip()}
        current_top: str | None = None
        current_nested: str | None = None

        for line in block.splitlines():
            top_match = re.match(r"^- \*\*([A-Za-z0-9_-]+):\*\*\s*(.*)$", line)
            if top_match:
                key, raw_value = top_match.groups()
                if raw_value.strip():
                    entry[key] = parse_scalar(raw_value)
                else:
                    entry[key] = {}
                current_top = key
                current_nested = None
                continue

            nested_match = re.match(r"^    - \*\*([A-Za-z0-9_-]+):\*\*\s*(.*)$", line)
            if nested_match and current_top:
                key, raw_value = nested_match.groups()
                parent = entry.setdefault(current_top, {})
                assert_true(f"{current_top} is mapping", isinstance(parent, dict))
                parent_dict = parent
                if raw_value.strip():
                    parent_dict[key] = parse_scalar(raw_value)
                else:
                    parent_dict[key] = []
                current_nested = key
                continue

            list_match = re.match(r"^      - (.+)$", line)
            if list_match and current_top and current_nested:
                parent = entry.setdefault(current_top, {})
                assert_true(f"{current_top} is mapping", isinstance(parent, dict))
                values = parent.setdefault(current_nested, [])
                assert_true(f"{current_top}.{current_nested} is list", isinstance(values, list))
                raw_item = list_match.group(1).strip()
                backtick_match = re.match(r"`([^`]+)`", raw_item)
                values.append(backtick_match.group(1) if backtick_match else parse_scalar(raw_item))

        entries.append(entry)

    return entries


def load_golden_master() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    frontmatter, body = split_frontmatter(read_repo_text("docs/controller-behaviours.md"))
    return parse_frontmatter(frontmatter), parse_entries(body)


def longest_common_substring_length(left: str, right: str) -> int:
    previous = [0] * (len(right) + 1)
    best = 0
    for left_index in range(1, len(left) + 1):
        current = [0] * (len(right) + 1)
        for right_index in range(1, len(right) + 1):
            if left[left_index - 1] == right[right_index - 1]:
                current[right_index] = previous[right_index - 1] + 1
                best = max(best, current[right_index])
        previous = current
    return best


def skill_section_budget_measure(skill_lines: list[str], match_line_index: int) -> tuple[str, int]:
    frontmatter_markers = [idx for idx, line in enumerate(skill_lines) if line.strip() == "---"]
    section_start = (frontmatter_markers[1] + 1) if len(frontmatter_markers) >= 2 else 0
    section_heading = "preamble"

    for idx in range(match_line_index, -1, -1):
        if skill_lines[idx].startswith("## "):
            section_start = idx + 1
            section_heading = skill_lines[idx]
            break
        if skill_lines[idx].strip() == "---":
            section_start = idx + 1
            break

    section_end = len(skill_lines)
    for idx in range(match_line_index + 1, len(skill_lines)):
        if skill_lines[idx].startswith("## "):
            section_end = idx
            break

    content_line_count = sum(1 for line in skill_lines[section_start:section_end] if line.strip())
    return section_heading, content_line_count


def test_golden_master_entry_schema() -> None:
    frontmatter, entries = load_golden_master()
    required_fields = set(frontmatter["required_fields"])
    schema_fields = set(frontmatter["schema_fields"])
    nested_schema_fields = {key: set(value) for key, value in frontmatter["nested_schema_fields"].items()}
    categories = set(frontmatter["category_enum"])

    assert_true("entry_schema fields match expected set", schema_fields == EXPECTED_SCHEMA_FIELDS)
    assert_true("entry_schema required fields match invariant", required_fields == REQUIRED_TOP_LEVEL_FIELDS)
    assert_true("entry_schema nested fields match expected set", nested_schema_fields == EXPECTED_NESTED_SCHEMA_FIELDS)
    assert_true("category enum matches expected 17 values", categories == EXPECTED_CATEGORIES)

    ids = [entry.get("id") for entry in entries]
    assert_true("ids are unique", len(ids) == len(set(ids)))
    assert_true("Golden Master entry ID set matches expected", set(ids) == EXPECTED_ENTRY_IDS)
    assert_true(
        "schema extension allowlist keys exist",
        set(KNOWN_ENTRY_SCHEMA_EXTENSIONS).issubset(set(ids)),
    )

    for entry in entries:
        entry_id = str(entry.get("id", entry.get("entry_heading", "<missing id>")))
        expected_extensions = KNOWN_ENTRY_SCHEMA_EXTENSIONS.get(entry_id, {"top": set(), "nested": {}})
        top_extensions = set(entry) - {"entry_heading"} - EXPECTED_SCHEMA_FIELDS
        assert_true(
            f"{entry_id} top-level schema extensions match allowlist",
            top_extensions == expected_extensions["top"],
        )

        for field in REQUIRED_TOP_LEVEL_FIELDS:
            assert_true(f"{entry_id} has {field}", field in entry and entry[field] not in ("", None, [], {}))

        for parent, children in REQUIRED_NESTED_FIELDS.items():
            assert_true(f"{entry_id} {parent} is mapping", isinstance(entry[parent], dict))
            for child in children:
                assert_true(f"{entry_id} has {parent}.{child}", child in entry[parent])
                value = entry[parent][child]
                if parent == "provenance" and child == "r_numbers":
                    assert_true(f"{entry_id} {parent}.{child} is list", isinstance(value, list))
                else:
                    assert_true(f"{entry_id} has nonempty {parent}.{child}", value not in ("", None, [], {}))

        expected_nested_extensions = expected_extensions["nested"]
        for parent, expected_children in EXPECTED_NESTED_SCHEMA_FIELDS.items():
            if isinstance(entry.get(parent), dict):
                nested_extensions = set(entry[parent]) - expected_children
                assert_true(
                    f"{entry_id} {parent} schema extensions match allowlist",
                    nested_extensions == expected_nested_extensions.get(parent, set()),
                )
        missing_expected_nested_extension_parents = set(expected_nested_extensions) - {
            parent for parent in EXPECTED_NESTED_SCHEMA_FIELDS if isinstance(entry.get(parent), dict)
        }
        assert_true(
            f"{entry_id} expected nested extension parents exist",
            not missing_expected_nested_extension_parents,
        )

        assert_true(f"{entry_id} heading matches id", entry["entry_heading"] == entry["id"])
        assert_true(f"{entry_id} category enum", entry["category"] in categories)
        assert_true(f"{entry_id} severity enum", entry["severity"] in SEVERITIES)
        assert_true(f"{entry_id} verifier kind enum", entry["verifier"]["kind"] in VERIFIER_KINDS)
        assert_true(
            f"{entry_id} compressed-form kind enum",
            entry["skill_md_compressed_form"]["kind"] in COMPRESSED_FORM_KINDS,
        )

        for source in entry["provenance"]["sources"]:
            assert_true(f"{entry_id} provenance source exists: {source}", (ROOT / source).exists())


def test_skill_md_matches_golden_master() -> None:
    _, entries = load_golden_master()
    skill = read_repo_text("SKILL.md")
    skill_lines = skill.splitlines()
    missing: dict[str, str] = {}
    entries_by_id = {entry["id"]: entry for entry in entries}
    section_budget_gaps: dict[str, dict[str, Any]] = {}

    for entry in entries:
        entry_id = entry["id"]
        compressed = entry["skill_md_compressed_form"]
        pattern = compressed["pattern"]
        if compressed["kind"] == "literal":
            matched = pattern in skill
            match_offset = skill.index(pattern) if matched else None
        else:
            matches = list(re.finditer(pattern, skill, flags=re.MULTILINE))
            matched = bool(matches)
            match_offset = matches[0].start() if matches else None
        if not matched:
            missing[entry_id] = pattern
        elif compressed["kind"] == "literal":
            occurrence_count = skill.count(pattern)
            assert_true(f"{entry_id} literal pattern appears exactly once", occurrence_count == 1)
        else:
            assert_true(f"{entry_id} regex pattern appears exactly once", len(matches) == 1)

        if matched and "max_section_lines" in compressed:
            budget = int(str(compressed["max_section_lines"]))
            assert match_offset is not None
            match_line_index = skill[:match_offset].count("\n")
            section_heading, content_line_count = skill_section_budget_measure(skill_lines, match_line_index)
            if content_line_count > budget:
                section_budget_gaps[entry_id] = {
                    "max_section_lines": budget,
                    "actual_content_lines": content_line_count,
                    "section_heading": section_heading,
                }

    known_metadata_drift = []
    for entry_id, expected in KNOWN_SKILL_ALIGNMENT_GAPS.items():
        entry = entries_by_id.get(entry_id)
        if not entry:
            known_metadata_drift.append(f"{entry_id}: entry removed")
            continue
        compressed = entry["skill_md_compressed_form"]
        if compressed["kind"] != expected["kind"] or compressed["pattern"] != expected["pattern"]:
            known_metadata_drift.append(f"{entry_id}: allowlist pattern metadata stale")
        substitute_count = skill.count(expected["current_substitute"])
        if substitute_count != 1:
            known_metadata_drift.append(f"{entry_id}: current substitute count {substitute_count}")
        else:
            substitute_line_index = skill[: skill.index(expected["current_substitute"])].count("\n")
            substitute_section, _content_lines = skill_section_budget_measure(skill_lines, substitute_line_index)
            if substitute_section != expected["current_substitute_section"]:
                known_metadata_drift.append(
                    f"{entry_id}: current substitute section {substitute_section!r}"
                )

    unknown_missing = {key: value for key, value in missing.items() if key not in KNOWN_SKILL_ALIGNMENT_GAPS}
    resolved_known = sorted(set(KNOWN_SKILL_ALIGNMENT_GAPS) - set(missing))
    unknown_section_budget_gaps = {
        key: value for key, value in section_budget_gaps.items() if key not in KNOWN_MAX_SECTION_LINE_GAPS
    }
    stale_section_budget_gaps = []
    for entry_id, expected in KNOWN_MAX_SECTION_LINE_GAPS.items():
        actual = section_budget_gaps.get(entry_id)
        expected_shape = {
            "max_section_lines": expected["max_section_lines"],
            "actual_content_lines": expected["actual_content_lines"],
            "section_heading": expected["section_heading"],
        }
        if actual is None:
            stale_section_budget_gaps.append(f"{entry_id}: fixed; remove allowlist")
        elif actual != expected_shape:
            stale_section_budget_gaps.append(f"{entry_id}: expected {expected_shape!r}, got {actual!r}")

    assert_true("known SKILL.md drift allowlist metadata stale: " + ", ".join(known_metadata_drift), not known_metadata_drift)
    assert_true(
        "unknown SKILL.md Golden Master drift: "
        + ", ".join(f"{key}={value!r}" for key, value in sorted(unknown_missing.items())),
        not unknown_missing,
    )
    assert_true(
        "unknown max_section_lines gaps: " + ", ".join(f"{key}={value!r}" for key, value in sorted(unknown_section_budget_gaps.items())),
        not unknown_section_budget_gaps,
    )
    assert_true("known max_section_lines gap metadata stale: " + ", ".join(stale_section_budget_gaps), not stale_section_budget_gaps)
    assert_true(
        "known SKILL.md drift fixed; remove allowlist entries: " + ", ".join(resolved_known),
        not resolved_known,
    )


def test_skill_md_structural_invariants() -> None:
    skill = read_repo_text("SKILL.md")
    skill_lines = skill.splitlines()
    wc_line_count = skill.count("\n")
    # Budget raised from 450 → 525 on 2026-05-28 to accommodate the
    # worker-reliability hardening additions (commit-guard pointer,
    # session-status activation contract, canonical post-compaction reload
    # order, in-flight monitoring worked commands for ACP + bash-tail,
    # permission-pattern warning, stale-wrapper warning, dangerous-bypass
    # context fence). Each addition was directly recommended by the AUI
    # surface audit + sweep B/C review findings. The budget catches
    # future feature-add bloat; the new ceiling leaves ~50 lines of
    # margin from the current state.
    assert_true(f"SKILL.md wc -l <= 525 (got {wc_line_count})", wc_line_count <= 525)

    frontmatter_markers = [idx for idx, line in enumerate(skill_lines) if line.strip() == "---"]
    assert_true("SKILL.md has YAML frontmatter close", len(frontmatter_markers) >= 2)
    close = frontmatter_markers[1]
    preamble_window = skill_lines[close + 1 : close + 26]
    assert_true(
        "Read this skill end-to-end appears within first 25 post-frontmatter lines",
        any("Read this skill end-to-end" in line for line in preamble_window),
    )

    agents = read_repo_text("AGENTS.md")
    assert_true("AGENTS.md mirrors state-handoff pointer", "protocols/state-handoff.md" in agents)

    protocol_readme = read_repo_text("protocols/README.md")
    chunk_review_rows = [line for line in protocol_readme.splitlines() if "`chunk-review.md`" in line]
    assert_true("protocols/README.md has chunk-review row", len(chunk_review_rows) == 1)
    row = chunk_review_rows[0]
    assert_true("chunk-review row mentions gstack", "gstack" in row)
    assert_true("chunk-review row mentions autoreview", "autoreview" in row)
    assert_true("chunk-review row orders gstack before autoreview", row.index("gstack") < row.index("autoreview"))


def test_golden_master_validation_rules() -> None:
    frontmatter, entries = load_golden_master()
    validation = frontmatter["validation"]
    entry_count = len(entries)
    total_count = validation["total_entry_count"]

    assert_true("validation block text matches expected", frontmatter["validation_block"] == EXPECTED_VALIDATION_BLOCK)
    assert_true("validation top-level keys exact", set(frontmatter["validation_keys"]) == EXPECTED_VALIDATION_KEYS)
    validation_nested_keys = {key: set(value) for key, value in frontmatter["validation_nested_keys"].items()}
    assert_true("validation nested keys exact", validation_nested_keys == EXPECTED_VALIDATION_NESTED_KEYS)
    assert_true("validation block matches expected rules", validation == EXPECTED_VALIDATION)
    assert_true("validation total_entry_count has min", "min" in total_count)
    assert_true("validation total_entry_count has max", "max" in total_count)
    assert_true("entry count >= validation min", entry_count >= total_count["min"])
    assert_true("entry count <= validation max", entry_count <= total_count["max"])

    ids = [entry["id"] for entry in entries]
    assert_true("validation unique_id", len(ids) == len(set(ids)))

    category_counts = Counter(entry["category"] for entry in entries)
    for category, minimum in validation["per_category_min"].items():
        assert_true(
            f"category {category} count >= {minimum}",
            category_counts[category] >= minimum,
        )
    assert_true("per_category_min covers expected categories", set(validation["per_category_min"]) == EXPECTED_CATEGORIES)

    patterns = [(entry["id"], entry["skill_md_compressed_form"]["pattern"]) for entry in entries]
    overlaps = []
    for left_index, (left_id, left_pattern) in enumerate(patterns):
        for right_id, right_pattern in patterns[left_index + 1 :]:
            common = longest_common_substring_length(left_pattern, right_pattern)
            if common >= 80:
                overlaps.append(f"{left_id} <-> {right_id}: {common}")
    assert_true("anchor_uniqueness no common substring >=80 chars: " + ", ".join(overlaps), not overlaps)


def test_runner_invokes_skill_structure() -> None:
    run_sh = read_repo_text("tests/run.sh")
    assert_true("tests/run.sh lists test_skill_structure", "tests/python/test_skill_structure.py" in run_sh)
    assert_true("tests/run.sh uses python test glob", "for test in tests/python/test_*.py" in run_sh)
    assert_true("tests/run.sh has structural-test execution sentinel", "skill_structure_seen" in run_sh)
    assert_true("tests/run.sh fails if structural test not executed", "required Golden Master guard was not executed" in run_sh)
    for acp_python in (None, "/tmp/goal-flight-missing-acp-python"):
        env = None
        label = "default env"
        if acp_python is not None:
            env = os.environ.copy()
            env["GOALFLIGHT_ACP_PYTHON"] = acp_python
            label = "missing ACP env"
        list_proc = subprocess.run(
            ["bash", "tests/run.sh", "--list"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        assert_true(f"tests/run.sh --list exits 0 with {label}: {list_proc.stderr}", list_proc.returncode == 0)
        assert_true(f"tests/run.sh --list has no FAIL with {label}", "FAIL" not in list_proc.stdout)
        assert_true(f"tests/run.sh --list has no SDK missing with {label}", "SDK missing" not in list_proc.stdout)
        listed_tests = [line.strip() for line in list_proc.stdout.splitlines() if line.strip()]
        assert_true(
            f"tests/run.sh --list includes test_skill_structure once with {label}",
            listed_tests.count("tests/python/test_skill_structure.py") == 1,
        )
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_root = Path(tmpdir)
        (fake_root / "tests/bash").mkdir(parents=True)
        (fake_root / "tests/python").mkdir(parents=True)
        shutil.copy2(ROOT / "tests/run.sh", fake_root / "tests/run.sh")
        (fake_root / "tests/python/test_placeholder.py").write_text("print('placeholder')\n", encoding="utf-8")
        missing_guard_proc = subprocess.run(
            ["bash", "tests/run.sh"],
            cwd=fake_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert_true("tests/run.sh fails when structural test absent", missing_guard_proc.returncode != 0)
        assert_true(
            "tests/run.sh absent structural test failure message",
            "required Golden Master guard was not executed" in missing_guard_proc.stdout,
        )
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_root = Path(tmpdir)
        marker = fake_root / "skill-structure-ran.txt"
        (fake_root / "tests/bash").mkdir(parents=True)
        (fake_root / "tests/python").mkdir(parents=True)
        shutil.copy2(ROOT / "tests/run.sh", fake_root / "tests/run.sh")
        (fake_root / "tests/python/test_skill_structure.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n",
            encoding="utf-8",
        )
        present_guard_proc = subprocess.run(
            ["bash", "tests/run.sh"],
            cwd=fake_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert_true(f"tests/run.sh fake structural test exits 0: {present_guard_proc.stdout}", present_guard_proc.returncode == 0)
        assert_true("tests/run.sh executes present structural test", marker.read_text(encoding="utf-8") == "ran")
    assert_true("tests/run.sh has no skip_skill_regression token", "skip_skill_regression" not in run_sh)


def protocol_markdown_files() -> list[str]:
    return sorted(str(path.relative_to(ROOT)) for path in (ROOT / "protocols").rglob("*.md") if path.is_file())


def test_protocol_host_tool_literal_scan() -> None:
    token_patterns = {
        "Skill(": re.compile(r"Skill\("),
        "AskUserQuestion": re.compile(r"AskUserQuestion"),
        "request_user_input": re.compile(r"request_user_input"),
        "functions.exec_command": re.compile(r"functions\.exec_command"),
        "Agent tool": re.compile(r"Agent(?:\s+|-)tool", flags=re.IGNORECASE),
    }
    hits: dict[tuple[str, str], list[tuple[int, str]]] = {}

    for relative_path in protocol_markdown_files():
        text = read_repo_text(relative_path)
        for token, pattern in token_patterns.items():
            for line_number, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    hits.setdefault((relative_path, token), []).append((line_number, line))

    unknown_hits = {key: value for key, value in hits.items() if key not in KNOWN_PROTOCOL_LITERAL_GAPS}
    resolved_known = sorted(set(KNOWN_PROTOCOL_LITERAL_GAPS) - set(hits))
    known_metadata_drift = []
    for key, expected in KNOWN_PROTOCOL_LITERAL_GAPS.items():
        actual = hits.get(key)
        expected_hit = [(expected["line_number"], expected["line"])]
        if actual is not None and actual != expected_hit:
            known_metadata_drift.append(f"{key[0]} {key[1]}: expected {expected_hit!r}, got {actual!r}")

    assert_true(
        "protocol host-tool literal leaks: "
        + ", ".join(
            f"{path}:{line_number} {token}"
            for (path, token), matches in sorted(unknown_hits.items())
            for line_number, _line in matches
        ),
        not unknown_hits,
    )
    assert_true("known protocol literal allowlist metadata stale: " + ", ".join(known_metadata_drift), not known_metadata_drift)
    assert_true(
        "known protocol literal gap fixed; remove allowlist entries: "
        + ", ".join(f"{path} {token}" for path, token in resolved_known),
        not resolved_known,
    )


def main() -> None:
    tests = sorted((name, value) for name, value in globals().items() if name.startswith("test_") and callable(value))
    failed = 0
    for name, test in tests:
        try:
            test()
            print(f"PASS  {name}")
        except Exception:
            failed += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
    if failed:
        raise SystemExit(failed)


if __name__ == "__main__":
    main()
