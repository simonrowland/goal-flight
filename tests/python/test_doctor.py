"""Focused doctor payload tests."""

from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
import io
import json
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
        # Simulated Windows must not reach native pid syscalls (ctypes.WinDLL
        # is absent on POSIX). This case's subject is doctor's platform fields.
        patch("goalflight_compat.pid_liveness", return_value=False),
        patch("goalflight_compat.process_start_identity", return_value=None),
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


def _minimal_human_payload(**overrides: object) -> dict:
    payload = {
        "plugin": {
            "skipped": False,
            "manifest_exists": True,
            "manifest": "ok",
            "validate_ok": True,
            "validate_first_line": "ok",
        },
        "claude": {"present": True, "version": "1.0"},
        "codex": {
            "cli": {"present": True, "version": "1.0"},
            "desktop_without_cli": False,
            "install_hint": None,
        },
        "context_mode": {"register_script_exists": True, "check_returncode": 0},
        "cursor_context_mode": {
            "global_check_returncode": 0,
            "global_path": "/tmp/cursor-global",
            "npx_present": True,
            "project_check_returncode": 0,
            "project_path": "/tmp/cursor-project",
        },
        "opencode_context_mode": {
            "global_check_returncode": 0,
            "global_path": "/tmp/oc-global",
            "npx_present": True,
            "project_check_returncode": 0,
            "project_path": "/tmp/oc-project",
        },
        "gstack": {"present": True, "version": "1.0", "detail": None, "level": None},
        "gstack_browser": {"present": True, "detail": "ok"},
        "agent_traits": {"ok": True, "detail": "ok"},
        "autoreview": {"present": True, "version": "1.0"},
        # Session status is a canonical doctor probe. The fixture used to omit
        # it, allowing a missing/failed probe to retain a green verdict.
        "session_status": {
            "ok": True,
            "present": True,
            "active": False,
            "queue_state": None,
        },
        "cursor": {
            "desktop_present": True,
            "agent": {"present": True, "version": "1.0"},
            "models": {
                "user_behind": False,
                "current_user_model": "x",
                "leading_internal": "x",
            },
        },
        "opencode": {"present": True, "version": "1.0"},
        "grok": {"present": True, "version": "1.0", "headless_flags": True},
        "claude_acp_stopgap": {"ok": True, "detail": "ok"},
        "worker_write_probe": {
            "ok": True,
            "agent": "grok-code",
            "kind": "write-file",
            "detail": "ok",
        },
        "pty_shim_health": {"warnings": []},
        "wsl_filesystems": {},
        "host_goalflight_install": {},
        "installed_skill_drift": {"entries": []},
        "acp": {},
        "capacity": {"operating_cap": 4, "raw_ram_ceiling": 8, "ram_mb": 8192},
        "project": {"present": True, "branch": "main", "head": "abc", "dirty": False},
        "worktrees": {"ok": True, "count": 0, "stale": [], "blocking_paths": []},
        "project_goalflight_readiness": {
            "init_done": True,
            "env_caveats": "ok",
            "repo_skill": {"exists": True, "path": "SKILL.md"},
            "routing": {
                "has_goalflight_block": True,
                "path": "AGENTS.md",
                "pins_newest_resume_notes": True,
            },
            "state_layout": {
                "ok": True,
                "missing_files": [],
                "missing_dirs": [],
                "view_schema_skew": [],
                "view_customizations": [],
            },
            "skill_root": {"exists": True, "path": "/tmp/skill", "source": "repo"},
            "commands": {"test": "./tests/run.sh", "lint": None, "build": None},
            "resume_notes": ["docs-private/RESUME-NOTES-2026-08-17.md"],
        },
        "router": {"ok": True, "recommended_entrypoint": "status"},
        "worker_currency": {},
        "rate_pressure": {
            "available": True,
            "providers_under_pressure": [],
            "records_examined": 64,
        },
    }
    payload.update(overrides)
    return payload


def _text_entries(payload: dict, level: str) -> list[tuple[str, str]]:
    prefix = {"warn": "[WARN]", "info": "[INFO]", "ok": "[OK]"}[level]
    out: list[tuple[str, str]] = []
    for line in goalflight_doctor.collect_human_lines(payload):
        if not line.startswith(prefix):
            continue
        parsed = goalflight_doctor.parse_status_line(line)
        out.append((parsed["probe"], parsed["detail"]))
    return out


def case_doctor_json_verdict_matches_text_warnings_and_info() -> None:
    payload = _minimal_human_payload(
        worker_currency={
            "claude": {"behind": True, "current": "2.1.220", "latest": "2.1.233"},
        },
        grok={"present": True, "version": "1.0", "headless_flags": None},
    )
    summary = goalflight_doctor.verdict_summary(payload)
    text_warns = _text_entries(payload, "warn")
    text_infos = _text_entries(payload, "info")
    json_warns = [(row["probe"], row["detail"]) for row in summary["warnings"]]
    json_infos = [(row["probe"], row["detail"]) for row in summary["info"]]
    assert text_warns == json_warns
    assert text_infos == json_infos
    for line in goalflight_doctor.collect_human_lines(payload):
        parsed = goalflight_doctor.parse_status_line(line)
        prefix = {"ok": "[OK]", "warn": "[WARN]", "info": "[INFO]"}[parsed["level"]]
        remainder = parsed["detail"]
        if parsed["fix"]:
            remainder = f"{remainder} {parsed['fix']}".strip() if remainder else parsed["fix"]
        rebuilt = f"{prefix} {parsed['probe']}" + (f" — {remainder}" if remainder else "")
        assert rebuilt == line
    assert summary["verdict"] == "warn"
    assert any(probe == "worker CLI currency" for probe, _detail in json_warns)
    for row in summary["warnings"]:
        assert set(row) == {"probe", "level", "detail", "fix"}
        assert row["level"] == "warn"
    for row in summary["info"]:
        assert set(row) == {"probe", "level", "detail", "fix"}
        assert row["level"] == "info"


def case_doctor_json_verdict_ok_when_text_has_no_warns() -> None:
    payload = _minimal_human_payload()
    summary = goalflight_doctor.verdict_summary(payload)
    assert _text_entries(payload, "warn") == []
    assert summary["warnings"] == []
    assert summary["verdict"] == "ok"


def case_doctor_failed_session_status_makes_verdict_warn() -> None:
    payload = _minimal_human_payload(
        session_status={
            "ok": False,
            "present": True,
            "error": "journal unreadable",
            "install_hint": "run session status directly",
        }
    )
    summary = goalflight_doctor.verdict_summary(payload)
    assert summary["verdict"] == "warn"
    assert any(
        row["probe"] == "session status" and "journal unreadable" in row["detail"]
        for row in summary["warnings"]
    )


def case_doctor_failed_rate_pressure_probe_is_not_ok() -> None:
    with patch.object(
        goalflight_doctor.goalflight_rate_pressure,
        "collect_records",
        side_effect=OSError("pressure ledger unreadable"),
    ):
        unavailable = goalflight_doctor._rate_pressure_summary()
    payload = _minimal_human_payload(rate_pressure=unavailable)
    summary = goalflight_doctor.verdict_summary(payload)
    assert summary["verdict"] == "warn"
    assert any(
        row["probe"] == "rate-pressure unavailable"
        and "pressure ledger unreadable" in row["detail"]
        for row in summary["warnings"]
    )
    assert not any(
        line.startswith("[OK] rate-pressure")
        for line in goalflight_doctor.collect_human_lines(payload)
    )


def case_doctor_line_cap_cannot_hide_late_warning() -> None:
    payload = _minimal_human_payload(
        host_goalflight_install={
            f"healthy-host-{index}": {"ok": True, "detail": "ok"}
            for index in range(goalflight_doctor._HUMAN_LINE_CAP + 5)
        },
        rate_pressure={
            "available": True,
            "providers_under_pressure": [
                {
                    "provider": "late-provider",
                    "count": 3,
                    "fallback_providers": [],
                    "recommended_caps": {"codex": 1},
                }
            ],
            "records_examined": 3,
        },
    )
    lines = goalflight_doctor.collect_human_lines(payload)
    assert len(lines) > goalflight_doctor._HUMAN_LINE_CAP
    summary = goalflight_doctor.verdict_summary(payload)
    assert summary["verdict"] == "warn"
    assert any(row["probe"] == "rate-pressure late-provider" for row in summary["warnings"])
    shown = goalflight_doctor.display_human_lines(lines, verbose=True)
    assert len(shown) == goalflight_doctor._HUMAN_LINE_CAP
    assert any(line.startswith("[WARN] rate-pressure late-provider") for line in shown)


def test_doctor_failed_session_status_makes_verdict_warn() -> None:
    case_doctor_failed_session_status_makes_verdict_warn()


def case_doctor_rejects_structurally_empty_session_status() -> None:
    with patch.object(
        goalflight_doctor,
        "run",
        return_value={"returncode": 0, "stdout": "{}", "stderr": ""},
    ):
        result = goalflight_doctor.check_session_status(ROOT, ROOT)
    assert result["ok"] is False
    assert "invalid session status payload" in result["error"]
    assert "missing fields" in result["error"]
    payload = _minimal_human_payload(session_status=result)
    lines = goalflight_doctor.collect_human_lines(payload)
    assert any(line.startswith("[WARN] session status") for line in lines)
    assert not any(line.startswith("[OK] session status") for line in lines)
    assert "active=False" not in "\n".join(lines)


def test_doctor_rejects_structurally_empty_session_status() -> None:
    case_doctor_rejects_structurally_empty_session_status()


def case_doctor_accepts_canonical_inactive_session_status() -> None:
    canonical = {
        "active": False,
        "queue_file": None,
        "queue_state": None,
        "queue_reason": "no queue files",
        "active_capacity_leases_in_project": 0,
        "newest_resume_notes": None,
        "resume_notes_active": False,
    }
    with patch.object(
        goalflight_doctor,
        "run",
        return_value={
            "returncode": 0,
            "stdout": json.dumps(canonical),
            "stderr": "",
        },
    ):
        result = goalflight_doctor.check_session_status(ROOT, ROOT)
    assert result["ok"] is True
    assert result["active"] is False
    assert result["active_capacity_leases_in_project"] == 0
    payload = _minimal_human_payload(session_status=result)
    lines = goalflight_doctor.collect_human_lines(payload)
    assert any(
        line.startswith("[OK] session status") and "active=False" in line
        for line in lines
    )


def test_doctor_accepts_canonical_inactive_session_status() -> None:
    case_doctor_accepts_canonical_inactive_session_status()


def test_doctor_failed_rate_pressure_probe_is_not_ok() -> None:
    case_doctor_failed_rate_pressure_probe_is_not_ok()


def case_doctor_malformed_rate_pressure_record_is_unavailable() -> None:
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        runs = state_dir / "runs.d"
        runs.mkdir(parents=True)
        (runs / "broken.json").write_text("{", encoding="utf-8")
        with patch.object(
            goalflight_doctor.goalflight_compat,
            "resolve_state_dir",
            return_value=state_dir,
        ):
            unavailable = goalflight_doctor._rate_pressure_summary()
    payload = _minimal_human_payload(rate_pressure=unavailable)
    lines = goalflight_doctor.collect_human_lines(payload)
    assert unavailable["available"] is False
    assert "RatePressureInputError" in unavailable["reason"]
    assert any(line.startswith("[WARN] rate-pressure unavailable") for line in lines)
    assert not any(line.startswith("[OK] rate-pressure") for line in lines)


def test_doctor_malformed_rate_pressure_record_is_unavailable() -> None:
    case_doctor_malformed_rate_pressure_record_is_unavailable()


def case_doctor_empty_rate_pressure_directory_is_measured_zero() -> None:
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        (state_dir / "runs.d").mkdir(parents=True)
        with patch.object(
            goalflight_doctor.goalflight_compat,
            "resolve_state_dir",
            return_value=state_dir,
        ):
            available = goalflight_doctor._rate_pressure_summary()
    payload = _minimal_human_payload(rate_pressure=available)
    assert available["available"] is True
    assert available["records_examined"] == 0
    assert any(
        line.startswith("[OK] rate-pressure") and "0 records examined" in line
        for line in goalflight_doctor.collect_human_lines(payload)
    )


def test_doctor_empty_rate_pressure_directory_is_measured_zero() -> None:
    case_doctor_empty_rate_pressure_directory_is_measured_zero()


def test_doctor_line_cap_cannot_hide_late_warning() -> None:
    case_doctor_line_cap_cannot_hide_late_warning()


def case_doctor_human_omits_ok_keeps_warn_and_info() -> None:
    payload = _minimal_human_payload(
        worker_currency={
            "claude": {"behind": True, "current": "2.1.220", "latest": "2.1.233"},
        },
        grok={"present": True, "version": "1.0", "headless_flags": None},
    )
    full = goalflight_doctor.collect_human_lines(payload)
    shown = goalflight_doctor.display_human_lines(full)
    assert any(line.startswith("[OK]") for line in full)
    assert shown
    assert all(not line.startswith("[OK]") for line in shown)
    assert any(line.startswith("[WARN]") for line in shown)
    assert any(line.startswith("[INFO]") for line in shown)
    assert shown == [line for line in full if not line.startswith("[OK]")]
    buf = io.StringIO()
    with redirect_stdout(buf):
        goalflight_doctor.print_human(payload)
    assert buf.getvalue() == "".join(f"{line}\n" for line in shown)


def case_doctor_verbose_recovers_ok_lines_verbatim() -> None:
    payload = _minimal_human_payload(
        worker_currency={
            "claude": {"behind": True, "current": "2.1.220", "latest": "2.1.233"},
        },
        grok={"present": True, "version": "1.0", "headless_flags": None},
    )
    full = goalflight_doctor.collect_human_lines(payload)
    assert goalflight_doctor.display_human_lines(full, verbose=True) == full
    buf = io.StringIO()
    with redirect_stdout(buf):
        goalflight_doctor.print_human(payload, verbose=True)
    assert buf.getvalue() == "".join(f"{line}\n" for line in full)


def case_doctor_human_all_ok_is_silent() -> None:
    # collect_human_lines always emits at least one [INFO] (gstack-browser);
    # silence is defined on the [OK] chorus, so pin the filter directly.
    lines = [
        "[OK] package plugin manifest — ok",
        "[OK] claude CLI — 1.0",
        "[OK] rate-pressure — no provider under pressure (0 records examined)",
    ]
    assert goalflight_doctor.display_human_lines(lines) == []
    assert goalflight_doctor.display_human_lines(lines, verbose=True) == lines
    payload = _minimal_human_payload()
    shown = goalflight_doctor.display_human_lines(
        goalflight_doctor.collect_human_lines(payload)
    )
    assert all(not line.startswith("[OK]") for line in shown)
    assert goalflight_doctor.verdict_summary(payload)["verdict"] == "ok"


def case_doctor_unhealthy_still_reports_warn() -> None:
    payload = _minimal_human_payload(
        worker_currency={
            "claude": {"behind": True, "current": "2.1.220", "latest": "2.1.233"},
        },
    )
    shown = goalflight_doctor.display_human_lines(
        goalflight_doctor.collect_human_lines(payload)
    )
    assert any(
        line.startswith("[WARN] worker CLI currency") and "Run /goal-flight update" in line
        for line in shown
    ), shown


def case_doctor_json_untouched_by_human_filter() -> None:
    payload = _minimal_human_payload(
        worker_currency={
            "claude": {"behind": True, "current": "2.1.220", "latest": "2.1.233"},
        },
    )
    buf = io.StringIO()
    with patch.object(goalflight_doctor, "doctor", return_value=payload), redirect_stdout(buf):
        rc = goalflight_doctor.main(["--json", "--project-root", str(ROOT)])
    assert rc == 0
    data = json.loads(buf.getvalue())
    verbose_buf = io.StringIO()
    with patch.object(goalflight_doctor, "doctor", return_value=payload), redirect_stdout(verbose_buf):
        verbose_rc = goalflight_doctor.main(
            ["--json", "--verbose", "--project-root", str(ROOT)]
        )
    assert verbose_rc == 0
    assert verbose_buf.getvalue() == buf.getvalue()
    assert data["plugin"]["manifest"] == "ok"
    assert data["claude"]["present"] is True
    assert data["claude"]["version"] == "1.0"
    assert data["verdict"] == "warn"
    assert "warnings" in data
    assert "info" in data
    human = "".join(
        f"{line}\n"
        for line in goalflight_doctor.display_human_lines(
            goalflight_doctor.collect_human_lines(payload)
        )
    )
    assert "[OK] claude CLI" not in human
    assert data["claude"]["version"] == "1.0"


def case_doctor_exit_codes_unchanged() -> None:
    healthy = _minimal_human_payload()
    with patch.object(goalflight_doctor, "doctor", return_value=healthy), \
        patch("goalflight_messages.emit_controller_mail_notice"), \
        patch("goalflight_messages.emit_controller_milestone_notice"), \
        redirect_stdout(io.StringIO()):
        assert goalflight_doctor.main(["--project-root", str(ROOT)]) == 0
        assert goalflight_doctor.main(["--verbose", "--project-root", str(ROOT)]) == 0
        assert goalflight_doctor.main(["--json", "--project-root", str(ROOT)]) == 0
    broken = _minimal_human_payload()
    broken["plugin"] = {
        "skipped": False,
        "manifest_exists": False,
        "manifest": None,
        "validate_ok": False,
        "validate_first_line": "missing",
    }
    with patch.object(goalflight_doctor, "doctor", return_value=broken), \
        patch("goalflight_messages.emit_controller_mail_notice"), \
        patch("goalflight_messages.emit_controller_milestone_notice"), \
        redirect_stdout(io.StringIO()):
        assert goalflight_doctor.main(["--project-root", str(ROOT)]) == 1
        assert goalflight_doctor.main(["--verbose", "--project-root", str(ROOT)]) == 1
        assert goalflight_doctor.main(["--json", "--project-root", str(ROOT)]) == 1


def case_doctor_json_cli_attaches_verdict_alongside_probes() -> None:
    payload = _minimal_human_payload(
        worker_currency={
            "claude": {"behind": True, "current": "2.1.220", "latest": "2.1.233"},
        },
    )
    buf = io.StringIO()
    with patch.object(goalflight_doctor, "doctor", return_value=payload), redirect_stdout(buf):
        rc = goalflight_doctor.main(["--json", "--project-root", str(ROOT)])
    assert rc == 0
    data = json.loads(buf.getvalue())
    assert data["verdict"] == "warn"
    assert data["plugin"]["manifest"] == "ok"
    text_warns = _text_entries(payload, "warn")
    json_warns = [(row["probe"], row["detail"]) for row in data["warnings"]]
    assert text_warns == json_warns
    assert "info" in data


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
    case_doctor_json_verdict_matches_text_warnings_and_info()
    case_doctor_json_verdict_ok_when_text_has_no_warns()
    case_doctor_failed_session_status_makes_verdict_warn()
    case_doctor_rejects_structurally_empty_session_status()
    case_doctor_accepts_canonical_inactive_session_status()
    case_doctor_failed_rate_pressure_probe_is_not_ok()
    case_doctor_malformed_rate_pressure_record_is_unavailable()
    case_doctor_empty_rate_pressure_directory_is_measured_zero()
    case_doctor_line_cap_cannot_hide_late_warning()
    case_doctor_json_cli_attaches_verdict_alongside_probes()
    case_doctor_human_omits_ok_keeps_warn_and_info()
    case_doctor_verbose_recovers_ok_lines_verbatim()
    case_doctor_human_all_ok_is_silent()
    case_doctor_unhealthy_still_reports_warn()
    case_doctor_json_untouched_by_human_filter()
    case_doctor_exit_codes_unchanged()
    print("OK: doctor tests pass")


if __name__ == "__main__":
    main()
