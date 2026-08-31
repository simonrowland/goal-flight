#!/usr/bin/env python3
"""The live-journal-isolation gate check must go red when a slug is planted.

The comparison python is correct: new project-<10-hex> children exit 1. The
bash `if !` around it used to put FAIL on the else branch, so a clean index
was reported as a leak and a real leak was swallowed. Measure polarity by
running that block from tests/run.sh against a fake operator XDG — never
the live journals index.
"""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
RUN_SH = ROOT / "tests" / "run.sh"
COMPARE_ENV = (
    "env -u GOALFLIGHT_JOURNAL_DIR -u GOALFLIGHT_TASK_STORE_DIR -u XDG_STATE_HOME"
)
FAIL_LINE = (
    "FAIL  live journals index gained project-<10-hex> children during python suite"
)
PLANTED_SLUG = "project-0fedcba987"


def _after_suite_if_block(text: str) -> str:
    first = text.index(COMPARE_ENV)
    second = text.index(COMPARE_ENV, first + 1)
    line_start = text.rfind("\n", 0, second) + 1
    closing = text.index('\n    fi\n    rm -f "$_GF_LIVE_SLUG_SNAP"', second)
    return text[line_start : closing + len("\n    fi")]


def _run_gate_check(*, operator_xdg: Path, snap: Path) -> subprocess.CompletedProcess[str]:
    block = _after_suite_if_block(RUN_SH.read_text(encoding="utf-8"))
    script = (
        "set -u\n"
        "fail=0\n"
        "failed_tests=()\n"
        f"_GF_LIVE_SLUG_SNAP={shlex.quote(str(snap))}\n"
        f"_GF_OPERATOR_XDG_STATE_HOME={shlex.quote(str(operator_xdg))}\n"
        f"{block}\n"
        'echo FAIL_COUNT=$fail\n'
        'echo FAILED_N=${#failed_tests[@]}\n'
    )
    env = os.environ.copy()
    exe_dir = str(Path(sys.executable).resolve().parent)
    env["PATH"] = exe_dir + os.pathsep + env.get("PATH", "")
    return subprocess.run(
        ["bash", "-c", script],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _empty_operator_index(tmp_path: Path) -> tuple[Path, Path]:
    operator_xdg = tmp_path / "operator-xdg"
    index = operator_xdg / "goal-flight" / "journals"
    index.mkdir(parents=True)
    snap = tmp_path / "live-journal-slugs"
    snap.write_text("", encoding="utf-8")
    return operator_xdg, snap


def test_gate_stays_green_when_operator_index_is_unchanged(tmp_path: Path) -> None:
    operator_xdg, snap = _empty_operator_index(tmp_path)
    proc = _run_gate_check(operator_xdg=operator_xdg, snap=snap)
    assert proc.returncode == 0, proc.stderr
    assert FAIL_LINE not in proc.stdout, proc.stdout
    assert "FAIL_COUNT=0" in proc.stdout, proc.stdout
    assert "FAILED_N=0" in proc.stdout, proc.stdout


def test_gate_goes_red_when_project_slug_is_planted(tmp_path: Path) -> None:
    operator_xdg, snap = _empty_operator_index(tmp_path)
    planted = operator_xdg / "goal-flight" / "journals" / PLANTED_SLUG
    planted.mkdir()
    proc = _run_gate_check(operator_xdg=operator_xdg, snap=snap)
    assert proc.returncode == 0, proc.stderr
    assert FAIL_LINE in proc.stdout, proc.stdout
    assert f"live journals index gained project-<10-hex> children: {PLANTED_SLUG}" in (
        proc.stdout + proc.stderr
    )
    assert "FAIL_COUNT=1" in proc.stdout, proc.stdout
    assert "FAILED_N=1" in proc.stdout, proc.stdout
