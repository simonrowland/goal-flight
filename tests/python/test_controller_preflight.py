"""Controller preflight matrix smoke tests."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "goalflight_controller_preflight.py"


def _run(adapter: str) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as td:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--adapter",
                adapter,
                "--fleet-dir",
                td,
                "--json",
            ],
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            check=False,
        )


def test_grok_controller_red():
    proc = _run("grok")
    payload = json.loads(proc.stdout)
    assert payload["status"] == "red"
    assert proc.returncode == 2


def test_cursor_preflight_emits_probe():
    proc = _run("cursor")
    assert proc.returncode in {0, 1}, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["status"] in {"green", "red", "yellow"}
    assert "context_files" in payload["checks"]


def _run_tests():
    failed = []
    passed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            passed += 1
        except Exception as exc:
            failed.append((name, str(exc)))
    return passed, failed


if __name__ == "__main__":
    passed, failed = _run_tests()
    if failed:
        print(f"FAIL tests/python/test_controller_preflight.py ({len(failed)} failed)")
        for name, err in failed:
            print(f"  - {name}: {err}")
        sys.exit(1)
    print(f"PASS tests/python/test_controller_preflight.py ({passed} tests)")
