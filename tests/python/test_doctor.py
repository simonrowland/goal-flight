"""Focused doctor payload tests."""

from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import sys
import tempfile
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
        patch("goalflight_doctor.worker_write_file_probe", return_value={"enabled": False, "ok": None}),
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
    assert payload["worker_write_probe"]["enabled"] is False


def case_doctor_reports_platform_fields_for_linux() -> None:
    patches = [
        patch("goalflight_compat.is_windows", return_value=False),
        patch("goalflight_compat.is_macos", return_value=False),
        patch("goalflight_compat.is_linux", return_value=True),
        patch("goalflight_compat.is_wsl", return_value=False),
        patch("goalflight_doctor.goalflight_os_sandbox.os_sandbox_available", return_value=False),
        patch("goalflight_doctor.goalflight_os_sandbox.os_sandbox_platform_key", return_value="linux"),
        patch("goalflight_doctor.goalflight_os_sandbox.platform_supported_os_sandbox_profiles", return_value=["off"]),
    ]
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        platform = goalflight_doctor.check_platform()
    assert platform["is_macos"] is False
    assert platform["is_linux"] is True
    assert platform["os_sandbox_available"] is False
    assert platform["os_sandbox_supported_profiles"] == ["off"]


def case_doctor_linux_desktop_probe_is_unknown_not_missing() -> None:
    with patch("goalflight_compat.is_macos", return_value=False), \
        patch("goalflight_compat.is_linux", return_value=True):
        assert goalflight_doctor.app_exists("DefinitelyMissingGoalFlightApp") is None


def case_doctor_reports_wsl_drvfs_warnings() -> None:
    old_state_dir = os.environ.get("GOALFLIGHT_STATE_DIR")
    os.environ["GOALFLIGHT_STATE_DIR"] = "/mnt/d/goal-flight-state"
    try:
        with patch("goalflight_compat.is_wsl", return_value=True):
            payload = goalflight_doctor.check_wsl_filesystems(
                Path("/mnt/c/project"),
                fleet_dir=Path("/mnt/e/fleet"),
            )
    finally:
        if old_state_dir is None:
            os.environ.pop("GOALFLIGHT_STATE_DIR", None)
        else:
            os.environ["GOALFLIGHT_STATE_DIR"] = old_state_dir
    assert payload["ok"] is False
    assert any("project_root" in item for item in payload["warnings"])
    assert any("state_dir" in item for item in payload["warnings"])
    assert any("fleet_lock_dir" in item for item in payload["warnings"])
    assert any("worktree_root" in item for item in payload["warnings"])


def case_doctor_skips_non_drvfs_mnt_mount_warning() -> None:
    old_state_dir = os.environ.get("GOALFLIGHT_STATE_DIR")
    os.environ["GOALFLIGHT_STATE_DIR"] = "/mnt/d/goal-flight-state"
    try:
        with patch("goalflight_compat.is_wsl", return_value=True), \
            patch("goalflight_compat._nearest_existing_path", return_value=Path("/mnt/d")), \
            patch("goalflight_compat._mount_fstype_for_path", return_value="ext4"):
            payload = goalflight_doctor.check_wsl_filesystems(
                Path("/mnt/d/project"),
                fleet_dir=Path("/mnt/d/fleet"),
            )
    finally:
        if old_state_dir is None:
            os.environ.pop("GOALFLIGHT_STATE_DIR", None)
        else:
            os.environ["GOALFLIGHT_STATE_DIR"] = old_state_dir
    assert payload["ok"] is True
    assert payload["warnings"] == []


def case_doctor_reports_drvfs_mount_warning_from_fstype() -> None:
    with patch("goalflight_compat.is_wsl", return_value=True), \
        patch("goalflight_compat._mount_fstype_for_path", return_value="drvfs"):
        payload = goalflight_doctor.check_wsl_filesystems(
            Path("/custom/project"),
            fleet_dir=Path("/custom/fleet"),
        )
    assert payload["ok"] is False
    assert any("project_root" in item for item in payload["warnings"])


def case_filesystem_type_branches_stat_for_platforms() -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs) -> dict:
        calls.append(cmd)
        return {"ok": True, "stdout": "apfs\n", "stderr": ""}

    with patch("goalflight_doctor._nearest_existing_path", return_value=ROOT), \
        patch("goalflight_compat.is_windows", return_value=False), \
        patch("goalflight_compat.is_linux", return_value=False), \
        patch("goalflight_compat.is_macos", return_value=True), \
        patch("goalflight_doctor.run", side_effect=fake_run):
        goalflight_doctor.filesystem_type(ROOT / "missing")
    assert calls == [["stat", "-f", "%T", str(ROOT)]]

    calls.clear()
    with patch("goalflight_doctor._nearest_existing_path", return_value=ROOT), \
        patch("goalflight_compat.is_windows", return_value=False), \
        patch("goalflight_compat.is_linux", return_value=True), \
        patch("goalflight_compat.is_macos", return_value=False), \
        patch("goalflight_doctor.run", side_effect=fake_run):
        goalflight_doctor.filesystem_type(ROOT / "missing")
    assert calls == [["stat", "-f", "-c", "%T", str(ROOT)]]


def case_doctor_reports_wsl_runtime_fields() -> None:
    with patch("goalflight_compat.is_windows", return_value=False), \
        patch("goalflight_compat.is_wsl", return_value=True):
        payload = goalflight_doctor.check_wsl(ROOT)
    assert payload["host"] == "wsl"
    assert "wsl_version" in payload
    assert "acp_venv" in payload
    assert payload["dispatch_capability"] == "full"


def case_claude_acp_newer_npm_retires_pinned_build() -> None:
    with patch("goalflight_doctor._claude_acp_installed_version", return_value="0.1.2"), \
        patch("goalflight_doctor._claude_acp_platform_binary", return_value=None):
        payload = goalflight_doctor.check_claude_acp_stopgap()
    assert payload["ok"] is True
    assert payload["pinned_fix_commit"] == "14a5b0c"
    assert payload["pinned_build_applied"] is None
    assert "newer than 0.1.1" in payload["detail"]
    assert "npm release should include the fix" in payload["detail"]


def case_claude_acp_warns_when_broken_binary_without_cargo() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-doctor-claude-acp-") as tmp:
        binary = Path(tmp) / "claude-code-cli-acp"
        binary.write_text("npm-binary\n", encoding="utf-8")
        with patch("goalflight_doctor._claude_acp_installed_version", return_value="0.1.1"), \
            patch("goalflight_doctor._claude_acp_platform_binary", return_value=binary), \
            patch("goalflight_doctor.shutil.which", return_value=None):
            payload = goalflight_doctor.check_claude_acp_stopgap()
    assert payload["ok"] is False
    assert payload["cargo_present"] is False
    assert payload["pinned_build_applied"] is False
    assert "broken npm binary" in payload["detail"]
    assert "install Rust cargo" in payload["detail"]
    assert "npm > 0.1.1" in payload["detail"]


def case_doctor_pty_shim_health_warns_when_orphans_present() -> None:
    with patch(
        "goalflight_acp_client.count_orphaned_acp_shims",
        return_value={
            "orphan_count": 2,
            "reapable_count": 1,
            "count_includes_foreign_shims": True,
            "orphans": [
                {"pid": 11, "goalflight_owned": True},
                {"pid": 12, "goalflight_owned": False},
            ],
        },
    ), patch("goalflight_doctor._read_ptmx_max", return_value=511), \
        patch("goalflight_doctor._read_ptmx_open_count", return_value=100):
        payload = goalflight_doctor.check_pty_shim_health()
    assert payload["level"] == "warning"
    assert payload["warnings"]
    assert payload["reapable_shim_count"] == 1
    assert "2 orphaned claude-code-cli-acp shims" in payload["warnings"][0]
    assert "ptmx_max=511" in payload["warnings"][0]
    # NO-GO guard: the warning must NOT imply the reaper clears all orphans; it
    # distinguishes the goal-flight-owned subset from editor/foreign-launched ones.
    assert "1 goal-flight-owned" in payload["warnings"][0]
    assert "editor/foreign-launched" in payload["warnings"][0]


def case_doctor_pty_shim_health_all_foreign_says_reaper_wont_act() -> None:
    with patch(
        "goalflight_acp_client.count_orphaned_acp_shims",
        return_value={
            "orphan_count": 1,
            "reapable_count": 0,
            "count_includes_foreign_shims": True,
            "orphans": [{"pid": 21, "goalflight_owned": False}],
        },
    ), patch("goalflight_doctor._read_ptmx_max", return_value=511), \
        patch("goalflight_doctor._read_ptmx_open_count", return_value=100):
        payload = goalflight_doctor.check_pty_shim_health()
    assert payload["level"] == "warning"
    assert payload["reapable_shim_count"] == 0
    assert "none are goal-flight-owned (reaper won't act)" in payload["warnings"][0]


def case_doctor_pty_shim_health_ok_when_no_orphans() -> None:
    with patch(
        "goalflight_acp_client.count_orphaned_acp_shims",
        return_value={"orphan_count": 0, "orphans": []},
    ), patch("goalflight_doctor._read_ptmx_max", return_value=511), \
        patch("goalflight_doctor._read_ptmx_open_count", return_value=100):
        payload = goalflight_doctor.check_pty_shim_health()
    assert payload["level"] == "ok"
    assert payload["warnings"] == []


def case_claude_acp_reports_pinned_build_when_orig_differs() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-doctor-claude-acp-") as tmp:
        binary = Path(tmp) / "claude-code-cli-acp"
        binary.write_text("pinned-build\n", encoding="utf-8")
        Path(f"{binary}.orig").write_text("npm-binary\n", encoding="utf-8")
        with patch("goalflight_doctor._claude_acp_installed_version", return_value="0.1.1"), \
            patch("goalflight_doctor._claude_acp_platform_binary", return_value=binary), \
            patch("goalflight_doctor.shutil.which", return_value="/usr/bin/cargo"):
            payload = goalflight_doctor.check_claude_acp_stopgap()
    assert payload["ok"] is True
    assert payload["pinned_build_applied"] is True
    assert "14a5b0c" in payload["detail"]
    assert "backup at" in payload["detail"]


def main() -> None:
    case_doctor_reports_platform_fields_for_windows()
    case_doctor_reports_platform_fields_for_linux()
    case_doctor_linux_desktop_probe_is_unknown_not_missing()
    case_doctor_reports_wsl_drvfs_warnings()
    case_doctor_skips_non_drvfs_mnt_mount_warning()
    case_doctor_reports_drvfs_mount_warning_from_fstype()
    case_filesystem_type_branches_stat_for_platforms()
    case_doctor_reports_wsl_runtime_fields()
    case_claude_acp_newer_npm_retires_pinned_build()
    case_claude_acp_warns_when_broken_binary_without_cargo()
    case_doctor_pty_shim_health_warns_when_orphans_present()
    case_doctor_pty_shim_health_all_foreign_says_reaper_wont_act()
    case_doctor_pty_shim_health_ok_when_no_orphans()
    case_claude_acp_reports_pinned_build_when_orig_differs()
    print("OK: doctor tests pass")


if __name__ == "__main__":
    main()
