"""Focused doctor payload tests."""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_doctor  # noqa: E402


def case_doctor_reports_platform_fields_for_windows() -> None:
    patches = [
        patch("goalflight_compat.is_windows", return_value=True),
        patch("goalflight_compat.python_executable", return_value=r"C:\Python311\python.exe"),
        patch("goalflight_compat.probe_wsl", return_value={
            "state": "ready",
            "usable": True,
            "present": True,
            "distributions": ["Ubuntu"],
            "declined": False,
        }),
        patch("goalflight_doctor.app_exists", return_value=False),
        patch("goalflight_doctor.version", return_value={"present": False}),
        patch("goalflight_doctor.check_plugin", return_value={"skipped": True}),
        patch("goalflight_doctor.check_host_goalflight_install", return_value={}),
        patch("goalflight_doctor.check_installed_skill_drift", return_value={"entries": []}),
        patch("goalflight_doctor.check_context_mode", return_value={}),
        patch("goalflight_doctor.check_cursor_context_mode", return_value={}),
        patch("goalflight_doctor.check_opencode_context_mode", return_value={}),
        patch("goalflight_doctor.check_gstack", return_value={}),
        patch("goalflight_doctor.check_autoreview", return_value={}),
        patch("goalflight_doctor.check_agents_md_state", return_value={}),
        patch("goalflight_doctor.check_session_status", return_value={}),
        patch("goalflight_doctor.check_resume_notes_pattern", return_value=[]),
        patch("goalflight_doctor.cursor_models_probe", return_value={}),
        patch("goalflight_doctor.check_grok", return_value={}),
        patch("goalflight_doctor.check_acp", return_value={}),
        patch("goalflight_doctor.git_state", return_value={}),
        patch("goalflight_doctor.check_worktrees", return_value={}),
        patch("goalflight_doctor.check_project_goalflight_readiness", return_value={}),
        patch("goalflight_doctor.check_router", return_value={}),
        patch("goalflight_doctor._fleet_reconcile_summary", return_value={}),
        patch("goalflight_doctor._rate_pressure_summary", return_value={}),
        patch("goalflight_doctor.worker_currency_probe", return_value={}),
        patch("goalflight_doctor.goalflight_capacity.profile", return_value={}),
    ]
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        payload = goalflight_doctor.doctor(ROOT)
    platform = payload["platform"]
    assert platform["is_windows"] is True
    assert platform["resolved_python"] == r"C:\Python311\python.exe"
    assert "dispatch refused" in platform["native_windows_support"]
    assert "tracked pid-only" in platform["native_windows_support"]
    assert payload["wsl"]["host"] == "native_windows"
    assert payload["wsl"]["usable"] is True
    assert payload["wsl"]["dispatch_capability"] == "refused_native_use_wsl"
    assert payload["wsl"]["native_cleanup"] == "degraded_per_pid"
    assert "UTF-16LE/NUL" in payload["wsl"]["false_no_distro_debug"]


def main() -> None:
    case_doctor_reports_platform_fields_for_windows()
    print("OK: doctor tests pass")


if __name__ == "__main__":
    main()
