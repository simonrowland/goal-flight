#!/usr/bin/env python3
"""Regression tests for ACP SDK python re-exec target selection."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from goalflight_acp_run import _acp_reexec_target  # noqa: E402


def _fake_python(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)


def case_reexec_target_preserves_venv_symlink() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        real_python = root / "realbin" / "python3.12"
        other_python = root / "otherbin" / "python3.12"
        venv_python = root / "venv" / "bin" / "python"
        _fake_python(real_python)
        _fake_python(other_python)
        venv_python.parent.mkdir(parents=True, exist_ok=True)
        venv_python.symlink_to(real_python)

        with (
            patch.dict(os.environ, {"GOALFLIGHT_ACP_PYTHON": str(venv_python)}),
            patch("goalflight_acp_run.importlib.util.find_spec", return_value=None),
            patch("goalflight_acp_run.sys.executable", str(other_python)),
        ):
            assert _acp_reexec_target() == str(venv_python)
            assert _acp_reexec_target() != os.path.realpath(venv_python)


def case_reexec_target_loop_guard_uses_resolved_paths() -> None:
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
            assert _acp_reexec_target() is None


def main() -> None:
    case_reexec_target_preserves_venv_symlink()
    case_reexec_target_loop_guard_uses_resolved_paths()
    print("OK: ACP re-exec target tests pass")


if __name__ == "__main__":
    main()
