"""Shared helpers for the file-runner Python tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _current_test_name() -> str:
    return Path(sys.argv[0]).name or "test"


def _posix_on_windows_reason(reason: str) -> str:
    import goalflight_compat

    try:
        probe = goalflight_compat.probe_wsl(ROOT)
    except Exception as exc:  # pragma: no cover - defensive visibility path
        probe = {"state": f"probe_failed:{type(exc).__name__}", "usable": False}

    if probe.get("usable"):
        reminder = "run this suite under WSL (where these POSIX tests execute)"
    else:
        reminder = (
            "needs the POSIX-for-Windows package - install WSL (`wsl --install`) "
            "and run this suite under WSL"
        )
    state = probe.get("state") or "unknown"
    return f"{reason}; native Windows cannot run POSIX primitives; WSL state={state}; {reminder}"


def skip_posix_on_native_windows(reason: str) -> None:
    """Exit cleanly on native Windows for tests that require POSIX semantics."""
    if os.name != "nt":
        return
    print(f"SKIP: {_current_test_name()}: {_posix_on_windows_reason(reason)}")
    raise SystemExit(0)


def skip_case_posix_on_native_windows(case_name: str, reason: str) -> bool:
    """Return True after printing a visible native-Windows skip for one case."""
    if os.name != "nt":
        return False
    print(f"SKIP: {case_name}: {_posix_on_windows_reason(reason)}")
    return True


def skip_unless_native_windows(reason: str) -> None:
    """Exit cleanly unless this is a real native-Windows Python process."""
    if os.name == "nt":
        return
    print(f"SKIP: {_current_test_name()}: {reason}")
    raise SystemExit(0)


def note_skip(case_name: str, reason: str) -> None:
    print(f"SKIP: {case_name}: {reason}")
