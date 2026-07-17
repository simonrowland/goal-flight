#!/usr/bin/env python3
"""Synthetic fixtures for deterministic review-protocol checks."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "goalflight_review_checks.py"


FINDING_A = """FINDING correctness-1 | P1 | reject invalid mode
Evidence: src/check.py:10
Test must assert: mode='unsafe' -> validate_mode() returns exit code 2
"""
FINDING_B = """FINDING contract-1 | P2 | preserve output path
Evidence: src/write.py:20
Test must assert: output path '/tmp/result.json' -> write_report() creates that file
"""


def _run(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(ROOT),
        input=stdin,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_happy_checks() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)
        finder_a = _write(tmp / "finder-a.log", FINDING_A)
        finder_b = _write(tmp / "finder-b.log", FINDING_B)
        report = _write(
            tmp / "fixer.md",
            FINDING_A
            + FINDING_B
            + "\nATTRIBUTION MAP\n| file:range | finding-id |\n"
            + "| src/check.py:10 | correctness-1 |\n"
            + "| src/write.py:20 | contract-1 |\n",
        )
        diff = _write(
            tmp / "change.diff",
            """diff --git a/src/check.py b/src/check.py
--- a/src/check.py
+++ b/src/check.py
@@ -10,3 +10,3 @@
-old
+new
 context
diff --git a/src/write.py b/src/write.py
--- a/src/write.py
+++ b/src/write.py
@@ -20,2 +20,2 @@
-old
+new
""",
        )

        compared = _run(
            "escrow-diff",
            "--finder-tail",
            str(finder_a),
            "--finder-tail",
            str(finder_b),
            "--fixer-report",
            str(report),
        )
        assert compared.returncode == 0, compared.stderr
        assert compared.stdout == "MATCH\n"

        mapped = _run("attribution", "--fixer-report", str(report), "--diff", str(diff))
        assert mapped.returncode == 0, mapped.stdout + mapped.stderr
        assert mapped.stdout == "COMPLETE\n"

        linted = _run("clause-lint", str(finder_a), str(finder_b))
        assert linted.returncode == 0, linted.stdout + linted.stderr
        assert linted.stdout.splitlines() == ["correctness-1 PASS", "contract-1 PASS"]

        dispatch_id = "fixer-dispatch-17"
        sampled = _run("sample", dispatch_id)
        expected = "SAMPLE" if int(hashlib.sha1(dispatch_id.encode()).hexdigest(), 16) % 3 == 0 else "SKIP"
        assert sampled.returncode == 0
        assert sampled.stdout == expected + "\n"


def test_escrow_divergence_is_exact() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)
        finder = _write(tmp / "finder.log", FINDING_A)
        changed = FINDING_A.replace("exit code 2", "exit code 1")
        report = _write(tmp / "fixer.md", changed)
        result = _run(
            "escrow-diff",
            "--finder-tail",
            str(finder),
            "--fixer-report",
            str(report),
        )
        assert result.returncode == 1
        assert "MISSING correctness-1 | P1 | Test must assert:" in result.stdout
        assert "exit code 2" in result.stdout
        assert "EXTRA correctness-1 | P1 | Test must assert:" in result.stdout
        assert "exit code 1" in result.stdout


def test_unattributed_hunk_and_orphan_row_fail() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)
        report = _write(
            tmp / "fixer.md",
            FINDING_A + "\nATTRIBUTION MAP\n| src/check.py:10-12 | missing-finding |\n",
        )
        diff = """diff --git a/src/other.py b/src/other.py
--- a/src/other.py
+++ b/src/other.py
@@ -40,2 +40,2 @@
-old
+new
"""
        result = _run("attribution", "--fixer-report", str(report), stdin=diff)
        assert result.returncode == 1
        assert "UNATTRIBUTED src/other.py:40-40" in result.stdout
        assert "ORPHAN | src/check.py:10-12 | missing-finding |" in result.stdout


def test_deleted_file_hunk_uses_old_range() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)
        report = _write(
            tmp / "fixer.md",
            FINDING_A + "\nATTRIBUTION MAP\n| src/old.py:4-5 | correctness-1 |\n",
        )
        diff = """diff --git a/src/old.py b/src/old.py
deleted file mode 100644
--- a/src/old.py
+++ /dev/null
@@ -4,2 +0,0 @@
-old
-lines
"""
        result = _run("attribution", "--fixer-report", str(report), stdin=diff)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == "COMPLETE\n"


def test_rows_before_attribution_map_do_not_cover_hunks() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)
        report = _write(
            tmp / "fixer.md",
            FINDING_A
            + "\n| src/check.py:10-12 | correctness-1 |\n"
            + "\nATTRIBUTION MAP\n",
        )
        diff = """diff --git a/src/check.py b/src/check.py
--- a/src/check.py
+++ b/src/check.py
@@ -10,3 +10,3 @@
-old
+new
 context
"""
        result = _run("attribution", "--fixer-report", str(report), stdin=diff)
        assert result.returncode == 1
        assert "UNATTRIBUTED src/check.py:10-10" in result.stdout


def test_weak_clause_fails() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        weak = _write(
            Path(raw_tmp) / "weak.log",
            """FINDING vague-1 | P2 | vague test
Test must assert: the gate works properly
""",
        )
        result = _run("clause-lint", str(weak))
        assert result.returncode == 1
        assert result.stdout.startswith("vague-1 WEAK:")
        assert "fewer than 6 words" in result.stdout
        assert "no input->output arrow" in result.stdout
        assert "no concrete value/path/symbol" in result.stdout
        assert "vague works/correct/proper wording" in result.stdout


def test_a1_concatenated_finding_marker_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)
        malformed = _write(
            tmp / "finder.log",
            "prose.FINDING A-1 | P1 | bug\n"
            "Test must assert: mode='unsafe' -> validate_mode() returns exit code 2\n",
        )
        report = _write(tmp / "fixer.md", malformed.read_text(encoding="utf-8"))
        compared = _run(
            "escrow-diff", "--finder-tail", str(malformed), "--fixer-report", str(report)
        )
        linted = _run("clause-lint", str(malformed))
        assert compared.returncode != 0
        assert "MATCH" not in compared.stdout
        assert linted.returncode != 0
        assert "PASS" not in linted.stdout


def test_a2_range_excess_fails() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        report = _write(
            Path(raw_tmp) / "fixer.md",
            FINDING_A + "\nATTRIBUTION MAP\n| src/x.py:1-999 | correctness-1 |\n",
        )
        diff = """diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -10 +10 @@
-old
+new
"""
        result = _run("attribution", "--fixer-report", str(report), stdin=diff)
        assert result.returncode == 1
        assert "RANGE_EXCESS" in result.stdout
        assert "COMPLETE" not in result.stdout


def test_a3_added_header_like_text_cannot_redirect_hunks() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        report = _write(
            Path(raw_tmp) / "fixer.md",
            FINDING_A + "\nATTRIBUTION MAP\n| decoy.py:20 | correctness-1 |\n",
        )
        diff = """diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -10,0 +10,1 @@
+++ b/decoy.py
@@ -20 +20 @@
-old
+new
"""
        result = _run("attribution", "--fixer-report", str(report), stdin=diff)
        assert result.returncode == 1
        assert "UNATTRIBUTED src/x.py:20-20" in result.stdout


def test_a4_ambiguous_attribution_names_both_ids() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        report = _write(
            Path(raw_tmp) / "fixer.md",
            "FINDING A-1 | P1 | first claim\n"
            "Test must assert: mode='unsafe' -> validate_mode() returns exit code 2\n"
            "FINDING A-2 | P1 | second claim\n"
            "Test must assert: mode='unsafe' -> validate_mode() returns exit code 2\n"
            + "\nATTRIBUTION MAP\n"
            + "| src/x.py:10 | A-1 |\n"
            + "| src/x.py:10 | A-2 |\n",
        )
        diff = """diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -10 +10 @@
-old
+new
"""
        result = _run("attribution", "--fixer-report", str(report), stdin=diff)
        assert result.returncode == 1
        assert "AMBIGUOUS" in result.stdout
        assert "A-1" in result.stdout
        assert "A-2" in result.stdout
        assert "COMPLETE" not in result.stdout


def test_a5_vague_output_is_not_concrete() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        weak = _write(
            Path(raw_tmp) / "weak.log",
            "FINDING A-5 | P1 | vague output\n"
            "Test must assert: request_id=7 -> some failure happens eventually\n",
        )
        result = _run("clause-lint", str(weak))
        assert result.returncode == 1
        assert "A-5 WEAK: output is not concrete" in result.stdout
        assert "PASS" not in result.stdout


def test_b1_range_excess_fails_for_multiline_hunk() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        report = _write(
            Path(raw_tmp) / "fixer.md",
            FINDING_A + "\nATTRIBUTION MAP\n| src/x.py:1-999 | correctness-1 |\n",
        )
        diff = """diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -10,3 +10,3 @@
-one
-two
-three
+four
+five
+six
"""
        result = _run("attribution", "--fixer-report", str(report), stdin=diff)
        assert result.returncode == 1
        assert "RANGE_EXCESS" in result.stdout
        assert "COMPLETE" not in result.stdout


def test_b2_overlapping_attribution_rows_fail() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        report = _write(
            Path(raw_tmp) / "fixer.md",
            "FINDING B-1 | P1 | first claim\n"
            "Test must assert: mode='unsafe' -> validate_mode() returns exit code 2\n"
            "FINDING B-2 | P1 | second claim\n"
            "Test must assert: mode='unsafe' -> validate_mode() returns exit code 2\n"
            + "\nATTRIBUTION MAP\n"
            + "| src/x.py:10-12 | B-1 |\n"
            + "| src/x.py:10-12 | B-2 |\n",
        )
        diff = """diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -10,3 +10,3 @@
-one
-two
-three
+four
+five
+six
"""
        result = _run("attribution", "--fixer-report", str(report), stdin=diff)
        assert result.returncode == 1
        assert "AMBIGUOUS" in result.stdout
        assert "B-1" in result.stdout
        assert "B-2" in result.stdout
        assert "COMPLETE" not in result.stdout


def test_b3_metadata_only_diff_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        report = _write(Path(raw_tmp) / "fixer.md", FINDING_A + "\nATTRIBUTION MAP\n")
        diff = """diff --git a/src/x.py b/src/x.py
old mode 100644
new mode 100755
"""
        result = _run("attribution", "--fixer-report", str(report), stdin=diff)
        assert result.returncode == 1
        assert "UNSUPPORTED" in result.stdout
        assert "COMPLETE" not in result.stdout


def test_b4_missing_clause_cannot_match() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)
        malformed = "FINDING B-1 | P1 | x\n"
        finder = _write(tmp / "finder.log", malformed)
        report = _write(tmp / "fixer.md", malformed)
        result = _run(
            "escrow-diff", "--finder-tail", str(finder), "--fixer-report", str(report)
        )
        assert result.returncode == 1
        assert "MATCH" not in result.stdout


def test_b5_empty_tail_reports_no_findings() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        empty = _write(Path(raw_tmp) / "empty.log", "")
        result = _run("clause-lint", str(empty))
        assert result.returncode == 1
        assert "NO_FINDINGS" in result.stdout


def test_b6_duplicate_clause_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        duplicate = _write(
            Path(raw_tmp) / "duplicate.log",
            "FINDING B-1 | P1 | duplicate clauses\n"
            "Test must assert: mode='unsafe' -> validate_mode() returns exit code 2\n"
            "Test must assert: the gate works\n",
        )
        result = _run("clause-lint", str(duplicate))
        assert result.returncode == 1
        assert "B-1 WEAK: duplicate Test must assert clauses" in result.stdout
        assert "B-1 PASS" not in result.stdout


def _assert_joint_attribution_delimiter(delimiter: str) -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)
        findings = (
            "FINDING A-1 | P1 | first claim\n"
            "Test must assert: mode='unsafe' -> validate_mode() returns exit code 2\n"
            "FINDING B-1 | P1 | second claim\n"
            "Test must assert: path '/tmp/x' -> write_report() creates that file\n"
        )
        diff = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -10 +10 @@
-old
+new
"""
        report = _write(
            tmp / "fixer.md",
            findings
            + "\nATTRIBUTION MAP\n"
            + f"| src/a.py:10 | A-1{delimiter}B-1 |\n",
        )
        result = _run("attribution", "--fixer-report", str(report), stdin=diff)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == "COMPLETE\n"


def test_r1_1_joint_attribution_accepts_plus_delimiter() -> None:
    _assert_joint_attribution_delimiter("+")


def test_r2_7_joint_attribution_accepts_slash_delimiter() -> None:
    _assert_joint_attribution_delimiter("/")


def test_r1_2_r2_1_mixed_metadata_section_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        report = _write(
            Path(raw_tmp) / "fixer.md",
            FINDING_A + "\nATTRIBUTION MAP\n| src/a.py:10 | correctness-1 |\n",
        )
        diff = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -10 +10 @@
-old
+new
diff --git a/src/b.py b/src/b.py
old mode 100644
new mode 100755
"""
        result = _run("attribution", "--fixer-report", str(report), stdin=diff)
        assert result.returncode == 1
        assert result.stdout == (
            "UNSUPPORTED section contains no attributable hunks | "
            "diff --git a/src/b.py b/src/b.py\n"
        )


def test_r1_3_deleted_double_dash_line_does_not_hide_next_hunk() -> None:
    _assert_deleted_dash_line_exposes_next_hunk("--flag")


def test_r2_2_deleted_single_dash_line_does_not_hide_next_hunk() -> None:
    _assert_deleted_dash_line_exposes_next_hunk("-flag")


def _assert_deleted_dash_line_exposes_next_hunk(source_line: str) -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        report = _write(
            Path(raw_tmp) / "fixer.md",
            FINDING_A + "\nATTRIBUTION MAP\n| src/a.py:10 | correctness-1 |\n",
        )
        diff = f"""diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -10 +10,0 @@
-{source_line}
@@ -20 +19 @@
-old
+new
"""
        result = _run("attribution", "--fixer-report", str(report), stdin=diff)
        assert result.returncode == 1
        assert result.stdout == "UNATTRIBUTED src/a.py:19-19 | @@ -20 +19 @@\n"


def test_r2_3_attribution_uses_changed_lines_not_context_span() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        report = _write(
            Path(raw_tmp) / "fixer.md",
            FINDING_A + "\nATTRIBUTION MAP\n| src/a.py:11 | correctness-1 |\n",
        )
        diff = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -10,3 +10,3 @@
 context-before
-old
+new
 context-after
"""
        result = _run("attribution", "--fixer-report", str(report), stdin=diff)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == "COMPLETE\n"


def test_r2_3_context_only_line_is_not_a_changed_range() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        report = _write(
            Path(raw_tmp) / "fixer.md",
            FINDING_A + "\nATTRIBUTION MAP\n| src/a.py:10 | correctness-1 |\n",
        )
        diff = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -10,3 +10,3 @@
 context-before
-old
+new
 context-after
"""
        result = _run("attribution", "--fixer-report", str(report), stdin=diff)
        assert result.returncode == 1
        assert result.stdout == (
            "UNATTRIBUTED src/a.py:11-11 | @@ -10,3 +10,3 @@\n"
            "RANGE_EXCESS | src/a.py:10 | correctness-1 |\n"
        )


def test_r2_4_vague_snake_case_output_is_weak() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        weak = _write(
            Path(raw_tmp) / "weak.log",
            "FINDING R2-4 | P1 | vague output\n"
            "Test must assert: request_id=7 with retry_count=2 -> "
            "response_status remains unexpected forever\n",
        )
        result = _run("clause-lint", str(weak))
        assert result.returncode == 1
        assert result.stdout == "R2-4 WEAK: output is not concrete\n"


def test_r1_4_duplicate_finding_id_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)
        duplicate = (
            "FINDING A-1 | P1 | first claim\n"
            "Test must assert: mode='unsafe' -> validate_mode() returns exit code 2\n"
            "FINDING A-1 | P1 | second claim\n"
            "Test must assert: mode='safe' -> validate_mode() returns exit code 0\n"
        )
        finder = _write(tmp / "finder.log", duplicate)
        report = _write(tmp / "fixer.md", duplicate)
        result = _run(
            "escrow-diff", "--finder-tail", str(finder), "--fixer-report", str(report)
        )
        assert result.returncode == 1
        assert result.stdout == (
            f"INVALID {finder}: A-1 DUPLICATE_FINDING_ID\n"
            f"INVALID {report}: A-1 DUPLICATE_FINDING_ID\n"
        )


def test_r2_5_named_exception_is_concrete_output() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        finding = _write(
            Path(raw_tmp) / "finding.log",
            "FINDING R2-5 | P2 | named exception\n"
            "Test must assert: input value 'unsafe' -> raises ValueError exception at validation\n",
        )
        result = _run("clause-lint", str(finding))
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == "R2-5 PASS\n"


def test_l3_1_bare_outcome_word_is_not_concrete() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        finding = _write(
            Path(raw_tmp) / "finding.log",
            "FINDING L3-1 | P1 | bare outcome word\n"
            "Test must assert: request_id=7 -> output changes after processing\n",
        )
        result = _run("clause-lint", str(finding))
        assert result.returncode == 1
        assert result.stdout == "L3-1 WEAK: output is not concrete\n"


def test_l3_2_named_state_transition_is_concrete() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        finding = _write(
            Path(raw_tmp) / "finding.log",
            "FINDING L3-2 | P2 | named state transition\n"
            "Test must assert: job_id=7 -> transitions from RUNNING to FAILED\n",
        )
        result = _run("clause-lint", str(finding))
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == "L3-2 PASS\n"


def test_l3_3_relative_filename_existence_is_concrete() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        finding = _write(
            Path(raw_tmp) / "finding.log",
            "FINDING L3-3 | P2 | relative filename existence\n"
            "Test must assert: mode='clean' -> README.md no longer exists\n",
        )
        result = _run("clause-lint", str(finding))
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == "L3-3 PASS\n"


def test_r2_6_reversed_range_is_invalid() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        report = _write(
            Path(raw_tmp) / "fixer.md",
            FINDING_A + "\nATTRIBUTION MAP\n| src/a.py:12-10 | correctness-1 |\n",
        )
        diff = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -10,3 +10,3 @@
-old1
-old2
-old3
+new1
+new2
+new3
"""
        result = _run("attribution", "--fixer-report", str(report), stdin=diff)
        assert result.returncode == 1
        assert result.stdout == (
            "INVALID_RANGE | src/a.py:12-10 | correctness-1 |\n"
        )


def test_joint_attribution_rejects_unknown_atomic_id() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        report = _write(
            Path(raw_tmp) / "fixer.md",
            FINDING_A
            + "\nATTRIBUTION MAP\n"
            + "| src/a.py:10 | correctness-1+missing-finding |\n",
        )
        diff = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -10 +10 @@
-old
+new
"""
        result = _run("attribution", "--fixer-report", str(report), stdin=diff)
        assert result.returncode == 1
        assert result.stdout == (
            "ORPHAN | src/a.py:10 | correctness-1+missing-finding |\n"
        )


def main() -> None:
    test_happy_checks()
    test_escrow_divergence_is_exact()
    test_unattributed_hunk_and_orphan_row_fail()
    test_deleted_file_hunk_uses_old_range()
    test_rows_before_attribution_map_do_not_cover_hunks()
    test_weak_clause_fails()
    test_a1_concatenated_finding_marker_fails_closed()
    test_a2_range_excess_fails()
    test_a3_added_header_like_text_cannot_redirect_hunks()
    test_a4_ambiguous_attribution_names_both_ids()
    test_a5_vague_output_is_not_concrete()
    test_b1_range_excess_fails_for_multiline_hunk()
    test_b2_overlapping_attribution_rows_fail()
    test_b3_metadata_only_diff_fails_closed()
    test_b4_missing_clause_cannot_match()
    test_b5_empty_tail_reports_no_findings()
    test_b6_duplicate_clause_is_rejected()
    test_r1_1_joint_attribution_accepts_plus_delimiter()
    test_r2_7_joint_attribution_accepts_slash_delimiter()
    test_r1_2_r2_1_mixed_metadata_section_fails_closed()
    test_r1_3_deleted_double_dash_line_does_not_hide_next_hunk()
    test_r2_2_deleted_single_dash_line_does_not_hide_next_hunk()
    test_r2_3_attribution_uses_changed_lines_not_context_span()
    test_r2_3_context_only_line_is_not_a_changed_range()
    test_r2_4_vague_snake_case_output_is_weak()
    test_r1_4_duplicate_finding_id_fails_closed()
    test_r2_5_named_exception_is_concrete_output()
    test_l3_1_bare_outcome_word_is_not_concrete()
    test_l3_2_named_state_transition_is_concrete()
    test_l3_3_relative_filename_existence_is_concrete()
    test_r2_6_reversed_range_is_invalid()
    test_joint_attribution_rejects_unknown_atomic_id()
    print("OK: review checks tests pass")


if __name__ == "__main__":
    main()
