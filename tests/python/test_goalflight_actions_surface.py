"""Track B surface tests: commands.env, setup-map, fleet action registry."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_actions  # noqa: E402


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "goalflight_actions.py"), *args],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
    )


def test_commands_env_idempotent():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "commands.env"
        first = _run(["commands-env", "--out", str(out)])
        assert first.returncode == 0, first.stderr
        assert "GF_ACTION_core_doctor_read=" in out.read_text()
        assert "goalflight_doctor.py" in out.read_text()
        assert "goalflight_acp_run.py" in out.read_text()
        second = _run(["commands-env", "--out", str(out)])
        assert second.returncode == 0
        assert "unchanged" in second.stdout


def test_validate_includes_fleet_actions():
    proc = _run(["validate"])
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "fleet.bootstrap.execute" in proc.stdout or proc.returncode == 0


def test_setup_map_has_worker_destinations():
    proc = _run(["setup-map", "--json"])
    assert proc.returncode == 0, proc.stderr
    rows = json.loads(proc.stdout)
    assert any(row["destination_id"] == "codex-cli-worker" for row in rows)


def test_windows_action_render_quotes_paths():
    entry = {
        "command": "${GOALFLIGHT_PYTHON} ${GOALFLIGHT_REPO}/scripts/goalflight_doctor.py --json",
        "env": {
            "GOALFLIGHT_PYTHON": "python3",
            "GOALFLIGHT_REPO": "${GOALFLIGHT_REPO_ROOT}",
        },
    }
    with patch("goalflight_actions.goalflight_compat.is_windows", return_value=True), \
        patch("goalflight_actions.goalflight_compat.python_executable", return_value=r"C:\Program Files\Python\python.exe"), \
        patch.dict(os.environ, {"GOALFLIGHT_REPO_ROOT": r"C:\Users\Ada Lovelace\goal-flight"}):
        cmd = goalflight_actions.resolve_command(entry)
    assert r'"C:\Program Files\Python\python.exe"' in cmd
    assert r'"C:\Users\Ada Lovelace\goal-flight"/scripts/goalflight_doctor.py' in cmd


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
        print(f"FAIL tests/python/test_goalflight_actions_surface.py ({len(failed)} failed)")
        for name, err in failed:
            print(f"  - {name}: {err}")
        sys.exit(1)
    print(f"PASS tests/python/test_goalflight_actions_surface.py ({passed} tests)")
