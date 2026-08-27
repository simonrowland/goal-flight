#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

python3 - "$REPO_ROOT" <<'PY'
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "scripts"))

import goalflight_gate

expected_default = (root / "tests" / "run.sh").resolve()
actual_default = Path(goalflight_gate.DEFAULT_CMD[0]).resolve()
if goalflight_gate.DEFAULT_CMD != [str(expected_default)] or actual_default != expected_default:
    raise SystemExit(
        f"quiet gate must default to the complete hermetic suite: {goalflight_gate.DEFAULT_CMD}"
    )

runner_text = (root / "tests" / "run.sh").read_text(encoding="utf-8")
required_fragments = (
    "python3 -m pytest tests/python -q",
    "find tests/python -type f -name 'test_*.py'",
    "$skip skipped",
    "skill_structure_collected",
    "-u GOALFLIGHT_ISOLATED_TEST_FILE",
    'GOALFLIGHT_MESSAGES_DIR="$_GF_MESSAGES_BASE"',
    'GOALFLIGHT_JOURNAL_DIR="$_GF_JOURNAL_BASE"',
    "project-<10-hex>",
)
for fragment in required_fragments:
    if fragment not in runner_text:
        raise SystemExit(f"tests/run.sh lost honest coverage/reporting fragment: {fragment}")
if 'run_isolated_test_env "$py" "$test"' in runner_text:
    raise SystemExit("tests/run.sh regressed to direct file execution that vacuously passes pytest modules")
if "skill_structure_collected=1" in runner_text:
    raise SystemExit("tests/run.sh regressed to a constant Golden Master collection guard")

listed = subprocess.run(
    ["bash", str(root / "tests" / "run.sh"), "--list"],
    cwd=root,
    text=True,
    capture_output=True,
    check=False,
)
if listed.returncode != 0:
    raise SystemExit(f"tests/run.sh --list failed: {listed.stdout}\n{listed.stderr}")
paths = set(listed.stdout.splitlines())
for required in (
    "tests/python/ext/test_claude_usage.py",
    "tests/python/test_ci_mutation_guards.py",
    "tests/python/test_goalflight_gate.py",
    "tests/python/test_script_style_modules.py",
    "tests/js/test_gf_escape.js",
):
    if required not in paths:
        raise SystemExit(f"tests/run.sh --list omitted {required}")

print("CI gate honesty tests passed")
PY
