#!/usr/bin/env python3
"""Native-Windows init contract checks."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_compat  # noqa: E402


WSL_EXE = r"C:\Windows\System32\wsl.exe"


def _runner(stdout: bytes):
    def run(cmd, **_kwargs):
        assert cmd == [WSL_EXE, "-l", "-q"], cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr=b"")

    return run


def case_init_docs_skip_posix_acp_venv_when_wsl_unusable() -> None:
    with patch("goalflight_compat.is_windows", return_value=True), \
        patch("goalflight_compat.is_wsl", return_value=False):
        probe = goalflight_compat.probe_wsl(
            ROOT,
            which=lambda _name: WSL_EXE,
            runner=_runner(
                "Windows Subsystem for Linux has no installed distributions.\r\n"
                "Distributions can be installed by visiting the Microsoft Store.\r\n".encode("utf-16le")
            ),
        )
    assert probe["usable"] is False
    assert probe["present"] is False

    init = (ROOT / "commands" / "init.md").read_text(encoding="utf-8")
    assert "skip\n   this block" in init
    assert "`bin/python` path is\n   intentionally not valid" in init
    assert "treat that as nonfatal" in init
    assert "native\n   control-plane mode" in init


def main() -> None:
    case_init_docs_skip_posix_acp_venv_when_wsl_unusable()
    print("OK: Windows init contract tests pass")


if __name__ == "__main__":
    main()
