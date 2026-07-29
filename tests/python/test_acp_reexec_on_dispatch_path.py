"""Behavioral coverage for dispatch-path ACP interpreter re-exec."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
DISPATCH = REPO_ROOT / "scripts" / "goalflight_dispatch.py"


def _reexec_probe(tmp_path: Path) -> tuple[Path, Path]:
    record = tmp_path / "reexec.argv"
    target = tmp_path / "fake-acp-python"
    target.write_text(
        "#!/bin/sh\n"
        "{\n"
        "  printf '%s\\n' \"$0\"\n"
        "  printf '%s\\n' \"$@\"\n"
        "} > \"$GOALFLIGHT_REEXEC_RECORD\"\n"
        "exit 73\n",
        encoding="utf-8",
    )
    target.chmod(0o755)
    return target, record


def _run_dispatch_help(tmp_path: Path, shape: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    target, record = _reexec_probe(tmp_path)
    env = os.environ.copy()
    env["GOALFLIGHT_ACP_PYTHON"] = str(target)
    env["GOALFLIGHT_REEXEC_RECORD"] = str(record)
    run = subprocess.run(
        [
            sys.executable,
            "-S",
            str(DISPATCH),
            "--shape",
            shape,
            "--help",
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
    )
    return run, record


def test_dispatch_acp_shape_reexecs_via_configured_sdk_interpreter(tmp_path: Path) -> None:
    run, record = _run_dispatch_help(tmp_path, "acp")
    assert run.returncode == 73, (run.stdout, run.stderr)
    argv = record.read_text(encoding="utf-8").splitlines()
    assert str(DISPATCH) in argv, argv
    assert "--shape" in argv and "acp" in argv, argv


def test_dispatch_bash_shape_does_not_reexec_via_sdk_interpreter(tmp_path: Path) -> None:
    run, record = _run_dispatch_help(tmp_path, "bash")
    assert run.returncode == 0, (run.stdout, run.stderr)
    assert not record.exists(), "bash dispatch was moved onto the ACP interpreter"
