#!/usr/bin/env python3
"""Default-skip live ACP push-gate matrix wrapper."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("live ACP matrix is WSL/POSIX-only")

import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts" / "goalflight_acp_push_gate_matrix.py"


def main() -> int:
    if os.environ.get("GOALFLIGHT_ACP_LIVE_MATRIX") != "1":
        print("SKIP: set GOALFLIGHT_ACP_LIVE_MATRIX=1 to run real ACP push-gate matrix")
        return 0
    with tempfile.TemporaryDirectory(prefix="goalflight-acp-matrix-test-") as td:
        tmp = Path(td)
        proc = subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "--state-dir",
                str(tmp / "state"),
                "--report",
                str(tmp / "report.json"),
            ],
            cwd=ROOT,
            env=os.environ.copy(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=3600,
            check=False,
        )
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
