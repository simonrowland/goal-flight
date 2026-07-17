#!/usr/bin/env python3
"""Deterministic mechanical checks for the Goal Flight review protocol."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import sys
from typing import Iterable


FINDING_RE = re.compile(r"FINDING\s+(\S+)\s*\|\s*(P[0-3])\s*\|")
FINDING_MARKER_RE = re.compile(r"FINDING\b")
CLAUSE_RE = re.compile(r"^\s*(?:[-*]\s*)?Test must assert:\s*(.*?)\s*$", re.IGNORECASE | re.MULTILINE)
MAP_ROW_RE = re.compile(r"^\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*$")
HUNK_RE = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")
VAGUE_RE = re.compile(r"\b(?:works?|correct(?:ly)?|proper(?:ly)?)\b", re.IGNORECASE)
VAGUE_OUTPUT_RE = re.compile(
    r"\b(?:some\s+failure|happens?|wrong|eventually|unexpected|forever)\b",
    re.IGNORECASE,
)
CONCRETE_RE = re.compile(
    r"(?:"
    r"['\"][^'\"]+['\"]"  # quoted value
    r"|\b\d+(?:\.\d+)?\b"  # numeric value
    r"|(?:^|\s)(?:\.?\.?/|/)[^\s]+"  # path
    r"|\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b"  # dotted path/symbol
    r"|\b[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]+\b"  # snake_case symbol
    r"|\b[A-Za-z_][A-Za-z0-9_]*\([^)]*\)"  # callable symbol
    r")"
)
OUTPUT_LITERAL_RE = re.compile(
    r"(?:"
    r"['\"][^'\"]+['\"]"
    r"|\b\d+(?:\.\d+)?\b"
    r"|(?:^|\s)(?:\.?\.?/|/)[^\s]+"
    r")"
)
ASSIGNED_VALUE_RE = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*\s*=\s*(?:['\"][^'\"]+['\"]|\d+(?:\.\d+)?|[A-Za-z_][A-Za-z0-9_]*)\b"
)
EXCEPTION_OUTCOME_RE = re.compile(
    r"\b(?:raises?|raised|throws?|thrown|errors?|errored)\b"
    r"[^\n]*\b[A-Z][A-Za-z0-9_]*(?:Error|Exception)\b",
    re.IGNORECASE,
)
EXIT_CODE_RE = re.compile(r"\bexit\s+code\s*(?:=|is|of)?\s*\d+\b", re.IGNORECASE)
NAMED_TRANSITION_RE = re.compile(
    r"\b(?:transitions?|moves?|changes?)\s+from\s+"
    r"[A-Z][A-Z0-9_]*\s+to\s+[A-Z][A-Z0-9_]*\b"
)
RELATIVE_FILENAME_RE = r"(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
FILE_EXISTENCE_RE = re.compile(
    rf"\b{RELATIVE_FILENAME_RE}\b[^\n]*"
    r"\b(?:exists?|absent|missing|removed|no\s+longer\s+exists?|does\s+not\s+exist)\b",
    re.IGNORECASE,
)
REFERENCED_FILE_ACTION_RE = re.compile(
    r"\b(?:creates?|created|writes?|written|deletes?|deleted|removes?|removed)\s+that\s+file\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, order=True)
class Finding:
    finding_id: str
    severity: str
    clause: str


@dataclass(frozen=True)
class Attribution:
    path: str
    start: int
    end: int
    finding_ids: tuple[str, ...]
    source: str


@dataclass(frozen=True)
class DiffHunk:
    path: str
    start: int
    end: int
    header: str


class InvalidAttributionRange(ValueError):
    """An attribution row declares its binding range backwards."""


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def finding_blocks(text: str) -> list[tuple[str, str, str]]:
    matches = list(FINDING_RE.finditer(text))
    blocks: list[tuple[str, str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        blocks.append((match.group(1), match.group(2), text[match.start() : end]))
    return blocks


def finding_diagnostics(text: str) -> list[str]:
    blocks = finding_blocks(text)
    diagnostics: list[str] = []
    if not blocks:
        diagnostics.append("NO_FINDINGS")

    parsed_starts = {match.start() for match in FINDING_RE.finditer(text)}
    marker_starts = {match.start() for match in FINDING_MARKER_RE.finditer(text)}
    if marker_starts - parsed_starts:
        diagnostics.append("UNPARSED_FINDING")
    if any(start > 0 and text[start - 1] not in "\r\n" for start in parsed_starts):
        diagnostics.append("UNPARSED_FINDING")

    finding_ids = [finding_id for finding_id, _severity, _block in blocks]
    for finding_id in dict.fromkeys(finding_ids):
        if finding_ids.count(finding_id) > 1:
            diagnostics.append(f"{finding_id} DUPLICATE_FINDING_ID")

    for finding_id, _severity, block in blocks:
        clauses = CLAUSE_RE.findall(block)
        if not clauses:
            diagnostics.append(f"{finding_id} MISSING_CLAUSE")
        elif len(clauses) > 1:
            diagnostics.append(f"{finding_id} DUPLICATE_CLAUSE")
    return list(dict.fromkeys(diagnostics))


def extract_findings(text: str) -> set[Finding]:
    findings: set[Finding] = set()
    for finding_id, severity, block in finding_blocks(text):
        clauses = CLAUSE_RE.findall(block)
        clause = clauses[0].strip() if len(clauses) == 1 else "<INVALID>"
        findings.add(Finding(finding_id, severity, clause))
    return findings


def _format_finding(prefix: str, finding: Finding) -> str:
    return f"{prefix} {finding.finding_id} | {finding.severity} | Test must assert: {finding.clause}"


def escrow_diff(finder_paths: Iterable[str], fixer_report: str) -> int:
    finder_findings: set[Finding] = set()
    invalid = False
    for path in finder_paths:
        text = _read(path)
        diagnostics = finding_diagnostics(text)
        for diagnostic in diagnostics:
            print(f"INVALID {path}: {diagnostic}")
        invalid = invalid or bool(diagnostics)
        finder_findings.update(extract_findings(text))
    fixer_text = _read(fixer_report)
    fixer_diagnostics = finding_diagnostics(fixer_text)
    for diagnostic in fixer_diagnostics:
        print(f"INVALID {fixer_report}: {diagnostic}")
    invalid = invalid or bool(fixer_diagnostics)
    fixer_findings = extract_findings(fixer_text)
    if invalid:
        return 1
    missing = sorted(finder_findings - fixer_findings)
    extra = sorted(fixer_findings - finder_findings)
    if not missing and not extra:
        print("MATCH")
        return 0
    for finding in missing:
        print(_format_finding("MISSING", finding))
    for finding in extra:
        print(_format_finding("EXTRA", finding))
    return 1


def sample(dispatch_id: str, k: int) -> int:
    if k < 1:
        raise ValueError("k must be at least 1")
    digest = hashlib.sha1(dispatch_id.encode()).hexdigest()
    print("SAMPLE" if int(digest, 16) % k == 0 else "SKIP")
    return 0


def _normalize_diff_path(value: str) -> str:
    value = value.strip().split("\t", 1)[0]
    if value.startswith(("a/", "b/")):
        value = value[2:]
    while value.startswith("./"):
        value = value[2:]
    return value


def parse_attribution_rows(report: str) -> list[Attribution]:
    rows: list[Attribution] = []
    in_map = False
    for line in report.splitlines():
        if re.match(r"^\s*ATTRIBUTION MAP\b", line, re.IGNORECASE):
            in_map = True
            continue
        if in_map and re.match(r"^\s*(?:DIFF-FOOTPRINT\b|#{1,6}\s)", line, re.IGNORECASE):
            break
        if not in_map:
            continue
        match = MAP_ROW_RE.match(line)
        if not match:
            continue
        location, finding_id_cell = (part.strip() for part in match.groups())
        if location.lower() in {"file:range", "file", "path:range"}:
            continue
        location_match = re.match(r"^(.+):(\d+)(?:-(\d+))?$", location)
        if not location_match:
            continue
        start = int(location_match.group(2))
        end = int(location_match.group(3) or start)
        if end < start:
            raise InvalidAttributionRange(line.strip())
        finding_ids = tuple(
            part.strip() for part in re.split(r"[+/,]", finding_id_cell)
        )
        rows.append(
            Attribution(
                _normalize_diff_path(location_match.group(1)),
                start,
                end,
                finding_ids,
                line.strip(),
            )
        )
    return rows


def parse_diff(diff: str) -> tuple[list[DiffHunk], list[str]]:
    old_path: str | None = None
    new_path: str | None = None
    hunks: list[DiffHunk] = []
    unsupported_sections: list[str] = []
    in_file = False
    section_header = ""
    section_hunk_count = 0
    old_remaining = 0
    new_remaining = 0
    old_line = 0
    new_line = 0
    hunk_header = ""
    deleted_start: int | None = None
    deleted_end: int | None = None
    added_start: int | None = None
    added_end: int | None = None

    def flush_change() -> None:
        nonlocal deleted_start, deleted_end, added_start, added_end, section_hunk_count
        path = old_path if added_start is None else new_path
        start = deleted_start if added_start is None else added_start
        end = deleted_end if added_start is None else added_end
        if path is not None and start is not None and end is not None:
            hunks.append(DiffHunk(path, start, end, hunk_header))
            section_hunk_count += 1
        deleted_start = deleted_end = added_start = added_end = None

    def finish_section() -> None:
        if in_file and section_hunk_count == 0:
            unsupported_sections.append(section_header)

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            flush_change()
            finish_section()
            in_file = True
            section_header = line
            section_hunk_count = 0
            old_path = None
            new_path = None
            old_remaining = 0
            new_remaining = 0
            continue
        if old_remaining or new_remaining:
            if line.startswith("+"):
                if added_start is None:
                    added_start = new_line
                added_end = new_line
                new_line += 1
                new_remaining = max(0, new_remaining - 1)
            elif line.startswith("-"):
                if deleted_start is None:
                    deleted_start = old_line
                deleted_end = old_line
                old_line += 1
                old_remaining = max(0, old_remaining - 1)
            elif line.startswith(" "):
                flush_change()
                old_line += 1
                new_line += 1
                old_remaining = max(0, old_remaining - 1)
                new_remaining = max(0, new_remaining - 1)
            if not old_remaining and not new_remaining:
                flush_change()
            continue
        if not in_file:
            continue
        if line.startswith("--- "):
            candidate = _normalize_diff_path(line[4:])
            old_path = None if candidate == "/dev/null" else candidate
            continue
        if line.startswith("+++ "):
            candidate = _normalize_diff_path(line[4:])
            new_path = None if candidate == "/dev/null" else candidate
            continue
        match = HUNK_RE.match(line)
        if not match:
            continue
        old_start = int(match.group(1))
        old_count = int(match.group(2) or "1")
        new_start = int(match.group(3))
        new_count = int(match.group(4) or "1")
        old_line = old_start
        new_line = new_start
        hunk_header = line
        old_remaining = old_count
        new_remaining = new_count
    flush_change()
    finish_section()
    return hunks, unsupported_sections


def parse_diff_hunks(diff: str) -> list[DiffHunk]:
    """Return attributable changed-line runs from a unified diff."""
    return parse_diff(diff)[0]


def attribution(report_path: str, diff_text: str) -> int:
    report = _read(report_path)
    report_diagnostics = finding_diagnostics(report)
    if report_diagnostics:
        for diagnostic in report_diagnostics:
            print(f"INVALID {report_path}: {diagnostic}")
        return 1
    finding_ids = {
        atomic_id.strip()
        for finding in extract_findings(report)
        for atomic_id in re.split(r"[+/,]", finding.finding_id)
    }
    try:
        rows = parse_attribution_rows(report)
    except InvalidAttributionRange as exc:
        print(f"INVALID_RANGE {exc}")
        return 1
    hunks, unsupported_sections = parse_diff(diff_text)
    if diff_text.strip() and not hunks and not unsupported_sections:
        print("UNSUPPORTED non-empty diff contains no parseable hunks")
        return 1
    orphan_rows = [
        row
        for row in rows
        if not row.finding_ids or any(finding_id not in finding_ids for finding_id in row.finding_ids)
    ]
    ambiguous: list[tuple[DiffHunk, list[Attribution]]] = []
    unattributed: list[DiffHunk] = []
    exact_rows: set[Attribution] = set()
    for hunk in hunks:
        matches = [
            row
            for row in rows
            if row.path == hunk.path and row.start == hunk.start and row.end == hunk.end
        ]
        exact_rows.update(matches)
        if not matches:
            unattributed.append(hunk)
        elif len(matches) > 1:
            ambiguous.append((hunk, matches))
    excess_rows = [row for row in rows if row not in exact_rows and row not in orphan_rows]
    if (
        not unsupported_sections
        and not orphan_rows
        and not unattributed
        and not ambiguous
        and not excess_rows
    ):
        print("COMPLETE")
        return 0
    for section in unsupported_sections:
        print(f"UNSUPPORTED section contains no attributable hunks | {section}")
    for hunk in unattributed:
        print(f"UNATTRIBUTED {hunk.path}:{hunk.start}-{hunk.end} | {hunk.header}")
    for row in orphan_rows:
        print(f"ORPHAN {row.source}")
    for hunk, matches in ambiguous:
        ids = ",".join(finding_id for row in matches for finding_id in row.finding_ids)
        print(f"AMBIGUOUS {hunk.path}:{hunk.start}-{hunk.end} | {ids}")
    for row in excess_rows:
        print(f"RANGE_EXCESS {row.source}")
    return 1


def lint_clause(clause: str) -> list[str]:
    reasons: list[str] = []
    words = re.findall(r"\b[\w./'-]+\b", clause)
    if len(words) < 6:
        reasons.append("fewer than 6 words")
    arrow_parts = re.split(r"(?:->|→)", clause)
    if len(arrow_parts) != 2:
        reasons.append("no input->output arrow")
    elif not CONCRETE_RE.search(arrow_parts[0]):
        reasons.append("input is not concrete")
    else:
        input_text = arrow_parts[0]
        output = arrow_parts[1]
        concrete_output = any(
            pattern.search(output)
            for pattern in (
                OUTPUT_LITERAL_RE,
                ASSIGNED_VALUE_RE,
                EXCEPTION_OUTCOME_RE,
                EXIT_CODE_RE,
                NAMED_TRANSITION_RE,
                FILE_EXISTENCE_RE,
            )
        ) or (
            REFERENCED_FILE_ACTION_RE.search(output) is not None
            and re.search(
                r"(?:['\"](?:\.?\.?/|/)[^'\"]+['\"]|(?:^|\s)(?:\.?\.?/|/)[^\s]+)",
                input_text,
            )
            is not None
        )
        if (
            VAGUE_OUTPUT_RE.search(output)
            or not concrete_output
        ):
            reasons.append("output is not concrete")
    if not CONCRETE_RE.search(clause):
        reasons.append("no concrete value/path/symbol")
    if VAGUE_RE.search(clause):
        reasons.append("vague works/correct/proper wording")
    return reasons


def clause_lint(paths: Iterable[str]) -> int:
    weak = False
    for path in paths:
        text = _read(path)
        diagnostics = finding_diagnostics(text)
        if diagnostics:
            weak = True
            for diagnostic in diagnostics:
                if diagnostic == "NO_FINDINGS":
                    print(f"NO_FINDINGS {path}")
                elif diagnostic == "UNPARSED_FINDING":
                    print(f"UNPARSED_FINDING {path}")
                elif diagnostic.endswith(" MISSING_CLAUSE"):
                    finding_id = diagnostic.split()[0]
                    print(f"{finding_id} WEAK: missing Test must assert clause")
                elif diagnostic.endswith(" DUPLICATE_CLAUSE"):
                    finding_id = diagnostic.split()[0]
                    print(f"{finding_id} WEAK: duplicate Test must assert clauses")
                elif diagnostic.endswith(" DUPLICATE_FINDING_ID"):
                    finding_id = diagnostic.split()[0]
                    print(f"{finding_id} WEAK: DUPLICATE_FINDING_ID")
            continue
        for finding_id, _severity, block in finding_blocks(text):
            clauses = CLAUSE_RE.findall(block)
            if not clauses:
                reasons = ["missing Test must assert clause"]
            else:
                reasons = lint_clause(clauses[0].strip())
            if reasons:
                weak = True
                print(f"{finding_id} WEAK: {'; '.join(reasons)}")
            else:
                print(f"{finding_id} PASS")
    return 1 if weak else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    escrow = commands.add_parser("escrow-diff", help="compare finder escrow with fixer report")
    escrow.add_argument("--finder-tail", action="append", required=True, help="repeat for each finder tail")
    escrow.add_argument("--fixer-report", required=True)

    sampling = commands.add_parser("sample", help="deterministically select a fixer for deep review")
    sampling.add_argument("dispatch_id")
    sampling.add_argument("--k", type=int, default=3)

    mapped = commands.add_parser("attribution", help="check attribution map against a unified diff")
    mapped.add_argument("--fixer-report", required=True)
    mapped.add_argument("--diff", help="unified diff path; omit to read stdin")

    lint = commands.add_parser("clause-lint", help="lint Test must assert clauses")
    lint.add_argument("paths", nargs="+")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "escrow-diff":
            return escrow_diff(args.finder_tail, args.fixer_report)
        if args.command == "sample":
            return sample(args.dispatch_id, args.k)
        if args.command == "attribution":
            diff_text = _read(args.diff) if args.diff else sys.stdin.read()
            return attribution(args.fixer_report, diff_text)
        if args.command == "clause-lint":
            return clause_lint(args.paths)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
