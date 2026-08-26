#!/usr/bin/env python3
"""Regression tests for ACP SDK python re-exec target selection."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("uses POSIX venv bin paths and symlink semantics")

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
os.environ["GOALFLIGHT_ACP_PYTHON"] = str(ROOT / ".missing-acp-test-python")

from goalflight_acp_client import (  # noqa: E402
    ACP_SDK_IMPORTABLE,
    ACP_SDK_REEXEC,
    ACP_SDK_UNAVAILABLE,
    AcpError,
    require_acp_sdk,
)
from goalflight_acp_run import _acp_reexec_target, _ensure_acp_sdk_python  # noqa: E402


def _fake_python(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)


def case_reexec_target_reexecs_to_venv_symlink_for_same_real_python() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        real_python = root / "realbin" / "python3.12"
        venv_python = root / "venv" / "bin" / "python"
        _fake_python(real_python)
        venv_python.parent.mkdir(parents=True, exist_ok=True)
        venv_python.symlink_to(real_python)

        with (
            patch.dict(os.environ, {"GOALFLIGHT_ACP_PYTHON": str(venv_python)}),
            patch("goalflight_acp_client.ACP_IMPORT_ERROR", ModuleNotFoundError("No module named 'acp'")),
            patch("goalflight_acp_client.sys.executable", str(real_python)),
        ):
            resolution = _acp_reexec_target()
            assert resolution.state == ACP_SDK_REEXEC, resolution
            assert resolution.target_python == str(venv_python), resolution
            assert resolution.target_python != os.path.realpath(venv_python)


def case_reexec_target_loop_guard_uses_invocation_path() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        real_python = root / "realbin" / "python3.12"
        venv_python = root / "venv" / "bin" / "python"
        _fake_python(real_python)
        venv_python.parent.mkdir(parents=True, exist_ok=True)
        venv_python.symlink_to(real_python)

        with (
            patch.dict(os.environ, {"GOALFLIGHT_ACP_PYTHON": str(venv_python)}),
            patch("goalflight_acp_client.ACP_IMPORT_ERROR", ModuleNotFoundError("No module named 'acp'")),
            patch("goalflight_acp_client.sys.executable", str(venv_python)),
        ):
            resolution = _acp_reexec_target()
            assert resolution.state == ACP_SDK_UNAVAILABLE, resolution
            assert "already the current interpreter" in resolution.reason
            assert "repair the SDK installation" in resolution.reason


def case_reexec_target_stays_put_when_acp_importable() -> None:
    with patch("goalflight_acp_client.ACP_IMPORT_ERROR", None):
        resolution = _acp_reexec_target()
        assert resolution.state == ACP_SDK_IMPORTABLE, resolution
        assert resolution.target_python is None


def case_reexec_target_honors_env_override() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        override_python = root / "custom" / "python"
        default_python = root / "home" / ".goal-flight" / "venvs" / "acp-0.10" / "bin" / "python"
        current_python = root / "bin" / "python3.12"
        _fake_python(override_python)
        _fake_python(default_python)
        _fake_python(current_python)

        with (
            patch.dict(os.environ, {"GOALFLIGHT_ACP_PYTHON": str(override_python)}),
            patch("goalflight_acp_client.Path.home", return_value=root / "home"),
            patch("goalflight_acp_client.ACP_IMPORT_ERROR", ModuleNotFoundError("No module named 'acp'")),
            patch("goalflight_acp_client.sys.executable", str(current_python)),
        ):
            resolution = _acp_reexec_target()
            assert resolution.state == ACP_SDK_REEXEC, resolution
            assert resolution.target_python == str(override_python)


def case_missing_target_is_distinct_from_importable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        missing = root / "missing" / "python"
        with (
            patch.dict(os.environ, {"GOALFLIGHT_ACP_PYTHON": str(missing)}),
            patch("goalflight_acp_client.ACP_IMPORT_ERROR", ModuleNotFoundError("No module named 'acp'")),
            patch("goalflight_acp_client.sys.executable", str(root / "current-python")),
        ):
            resolution = _acp_reexec_target()
            assert resolution.state == ACP_SDK_UNAVAILABLE, resolution
            assert resolution.target_python == str(missing)
            assert "does not exist" in resolution.reason
            assert "GOALFLIGHT_ACP_PYTHON" in resolution.reason


def case_require_sdk_reports_wrong_interpreter_without_install_claim() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "venv" / "bin" / "python"
        _fake_python(target)
        with (
            patch.dict(os.environ, {"GOALFLIGHT_ACP_PYTHON": str(target)}),
            patch("goalflight_acp_client.ACP_IMPORT_ERROR", ModuleNotFoundError("No module named 'acp'")),
            patch("goalflight_acp_client.sys.executable", str(root / "system-python")),
        ):
            try:
                require_acp_sdk()
            except AcpError as exc:
                message = str(exc)
            else:
                raise AssertionError("wrong interpreter did not fail the SDK requirement")
            assert "requires a different interpreter" in message
            assert str(target) in message
            assert "run install" not in message


def case_require_sdk_reports_genuinely_absent_managed_venv() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("goalflight_acp_client.Path.home", return_value=root),
            patch("goalflight_acp_client.ACP_IMPORT_ERROR", ModuleNotFoundError("No module named 'acp'")),
            patch("goalflight_acp_client.sys.executable", str(root / "system-python")),
        ):
            try:
                require_acp_sdk()
            except AcpError as exc:
                message = str(exc)
            else:
                raise AssertionError("absent managed venv did not fail the SDK requirement")
            assert "requirement cannot be satisfied" in message
            assert "does not exist" in message
            assert "run install" in message
            assert "requires a different interpreter" not in message


def case_execv_failure_reports_execution_remedy_not_install() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "venv" / "bin" / "python"
        _fake_python(target)
        with (
            patch.dict(os.environ, {"GOALFLIGHT_ACP_PYTHON": str(target)}),
            patch("goalflight_acp_client.ACP_IMPORT_ERROR", ModuleNotFoundError("No module named 'acp'")),
            patch("goalflight_acp_client.sys.executable", str(root / "system-python")),
            patch("goalflight_acp_run.os.execv", side_effect=PermissionError("execute denied")),
        ):
            try:
                _ensure_acp_sdk_python()
            except AcpError as exc:
                message = str(exc)
            else:
                raise AssertionError("os.execv failure was swallowed")
            assert "re-exec failed" in message
            assert "PermissionError: execute denied" in message
            assert str(target) in message
            assert "run install" not in message


def main() -> None:
    case_reexec_target_reexecs_to_venv_symlink_for_same_real_python()
    case_reexec_target_loop_guard_uses_invocation_path()
    case_reexec_target_stays_put_when_acp_importable()
    case_reexec_target_honors_env_override()
    case_missing_target_is_distinct_from_importable()
    case_require_sdk_reports_wrong_interpreter_without_install_claim()
    case_require_sdk_reports_genuinely_absent_managed_venv()
    case_execv_failure_reports_execution_remedy_not_install()
    print("OK: ACP re-exec target tests pass")


if __name__ == "__main__":
    main()
