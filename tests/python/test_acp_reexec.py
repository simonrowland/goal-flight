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

from goalflight_acp_run import _acp_reexec_target  # noqa: E402


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
            patch("goalflight_acp_run.importlib.util.find_spec", return_value=None),
            patch("goalflight_acp_run.sys.executable", str(real_python)),
        ):
            assert _acp_reexec_target() == str(venv_python)
            assert _acp_reexec_target() != os.path.realpath(venv_python)


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
            patch("goalflight_acp_run.importlib.util.find_spec", return_value=None),
            patch("goalflight_acp_run.sys.executable", str(venv_python)),
        ):
            assert _acp_reexec_target() is None


def case_reexec_target_stays_put_when_acp_importable() -> None:
    with (
        patch("goalflight_acp_run.importlib.util.find_spec", return_value=object()),
        patch("goalflight_acp_run.sys.executable", "/usr/bin/python3"),
    ):
        assert _acp_reexec_target() is None


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
            patch("goalflight_acp_run.Path.home", return_value=root / "home"),
            patch("goalflight_acp_run.importlib.util.find_spec", return_value=None),
            patch("goalflight_acp_run.sys.executable", str(current_python)),
        ):
            assert _acp_reexec_target() == str(override_python)


def main() -> None:
    case_reexec_target_reexecs_to_venv_symlink_for_same_real_python()
    case_reexec_target_loop_guard_uses_invocation_path()
    case_reexec_target_stays_put_when_acp_importable()
    case_reexec_target_honors_env_override()
    print("OK: ACP re-exec target tests pass")


if __name__ == "__main__":
    main()
