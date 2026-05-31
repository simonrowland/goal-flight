#!/usr/bin/env python3
"""Windows->WSL probe tests for init/doctor readiness."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_compat  # noqa: E402


WSL_EXE = r"C:\Windows\System32\wsl.exe"


READY_SENTINEL = b"__goalflight_wsl_ready__"


def _runner(
    stdout: bytes,
    *,
    stderr: bytes = b"",
    returncode: int = 0,
    launch_stdout: bytes = READY_SENTINEL,
    launch_stderr: bytes = b"",
    launch_returncode: int = 0,
):
    def run(cmd, **_kwargs):
        if cmd == [WSL_EXE, "-l", "-q"]:
            return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)
        if cmd == [WSL_EXE, "-e", "sh", "-lc", "printf __goalflight_wsl_ready__"]:
            return subprocess.CompletedProcess(
                cmd,
                launch_returncode,
                stdout=launch_stdout,
                stderr=launch_stderr,
            )
        raise AssertionError(cmd)

    return run


def _utf16(text: str) -> bytes:
    return text.encode("utf-16le")


def case_wsl_missing_executable() -> None:
    with patch("goalflight_compat.is_windows", return_value=True), \
        patch("goalflight_compat.is_wsl", return_value=False):
        payload = goalflight_compat.probe_wsl(
            ROOT,
            which=lambda _name: None,
            runner=_runner(b"should not run"),
        )
    assert payload["state"] == "missing_executable"
    assert payload["usable"] is False
    assert payload["present"] is False


def case_wsl_present_no_distro_is_not_usable() -> None:
    with patch("goalflight_compat.is_windows", return_value=True), \
        patch("goalflight_compat.is_wsl", return_value=False):
        payload = goalflight_compat.probe_wsl(
            ROOT,
            which=lambda _name: WSL_EXE,
            runner=_runner(_utf16("Windows Subsystem for Linux has no installed distributions.\r\n")),
        )
    assert payload["state"] == "no_installed_distributions"
    assert payload["usable"] is False
    assert payload["present"] is False
    assert payload["wsl_exe_present"] is True
    assert payload["distributions"] == []


def case_wsl_guidance_lines_are_not_distros() -> None:
    text = (
        "Windows Subsystem for Linux has no installed distributions.\r\n"
        "Distributions can be installed by visiting the Microsoft Store:\r\n"
        "https://aka.ms/wslstore\r\n"
    )
    with patch("goalflight_compat.is_windows", return_value=True), \
        patch("goalflight_compat.is_wsl", return_value=False):
        payload = goalflight_compat.probe_wsl(
            ROOT,
            which=lambda _name: WSL_EXE,
            runner=_runner(_utf16(text), launch_returncode=1),
        )
    assert payload["state"] == "no_installed_distributions"
    assert payload["usable"] is False
    assert payload["present"] is False
    assert payload["distributions"] == []


def case_wsl_localized_no_distro_is_absent() -> None:
    samples = [
        "Es sind keine installierten Distributionen vorhanden.\r\n",
        "No hay distribuciones instaladas.\r\n",
        "Aucune distribution n'est installee.\r\n",
        "インストールされているディストリビューションはありません。\r\n",
    ]
    for text in samples:
        with patch("goalflight_compat.is_windows", return_value=True), \
            patch("goalflight_compat.is_wsl", return_value=False):
            payload = goalflight_compat.probe_wsl(
                ROOT,
                which=lambda _name: WSL_EXE,
                runner=_runner(_utf16(text), returncode=1, launch_returncode=1),
            )
        assert payload["state"] == "no_installed_distributions", text
        assert payload["usable"] is False
        assert payload["present"] is False
        assert payload["distributions"] == []


def case_wsl_launch_no_distro_clears_fake_distro_lines() -> None:
    with patch("goalflight_compat.is_windows", return_value=True), \
        patch("goalflight_compat.is_wsl", return_value=False):
        payload = goalflight_compat.probe_wsl(
            ROOT,
            which=lambda _name: WSL_EXE,
            runner=_runner(
                _utf16("This is not a distro\r\n"),
                launch_stdout=b"",
                launch_stderr=_utf16("no installed distributions"),
                launch_returncode=1,
            ),
        )
    assert payload["state"] == "no_installed_distributions"
    assert payload["usable"] is False
    assert payload["present"] is False
    assert payload["distributions"] == []


def case_wsl_utf16_distro_output_is_ready() -> None:
    with patch("goalflight_compat.is_windows", return_value=True), \
        patch("goalflight_compat.is_wsl", return_value=False):
        payload = goalflight_compat.probe_wsl(
            ROOT,
            which=lambda _name: WSL_EXE,
            runner=_runner(_utf16("Ubuntu\r\nDebian\r\n")),
        )
    assert payload["state"] == "ready"
    assert payload["usable"] is True
    assert payload["present"] is True
    assert payload["distributions"] == ["Ubuntu", "Debian"]


def case_wsl_decline_stamp_suppresses_prompt_signal() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        goalflight_compat.record_wsl_install_declined(project)
        with patch("goalflight_compat.is_windows", return_value=True), \
            patch("goalflight_compat.is_wsl", return_value=False):
            payload = goalflight_compat.probe_wsl(
                project,
                which=lambda _name: None,
                runner=_runner(b"should not run"),
            )
    assert payload["declined"] is True
    assert payload["decline_stamp"].endswith("docs-private/windows-wsl-install-declined.json")


def case_wsl_drvfs_mountinfo_parser() -> None:
    lines = [
        "36 25 0:32 / / rw,relatime - ext4 /dev/sdb rw",
        "37 25 0:33 / /mnt/d rw,relatime - ext4 /dev/sdc rw",
        "38 25 0:34 / /custom\\040drive rw,relatime - drvfs C: rw",
        "39 25 0:35 / /mnt/c rw,relatime - 9p C: rw",
    ]
    assert goalflight_compat._mountinfo_fstype_from_lines(  # noqa: SLF001
        Path("/mnt/d/project"), lines
    ) == "ext4"
    assert goalflight_compat._mountinfo_fstype_from_lines(  # noqa: SLF001
        Path("/custom drive/project"), lines
    ) == "drvfs"
    assert goalflight_compat._mountinfo_fstype_from_lines(  # noqa: SLF001
        Path("/mnt/c/project"), lines
    ) == "9p"


def case_wsl_drvfs_detection_uses_mount_fstype_before_syntax() -> None:
    with patch("goalflight_compat._nearest_existing_path", return_value=Path("/mnt/d")), \
        patch("goalflight_compat._mount_fstype_for_path", return_value="ext4"):
        assert not goalflight_compat.is_wsl_drvfs_path("/mnt/d/project")
    with patch("goalflight_compat._mount_fstype_for_path", return_value="drvfs"):
        assert goalflight_compat.is_wsl_drvfs_path("/custom/project")
    with patch("goalflight_compat._mount_fstype_for_path", return_value=None):
        assert goalflight_compat.is_wsl_drvfs_path("/mnt/d/project")


def main() -> None:
    case_wsl_missing_executable()
    case_wsl_present_no_distro_is_not_usable()
    case_wsl_guidance_lines_are_not_distros()
    case_wsl_localized_no_distro_is_absent()
    case_wsl_launch_no_distro_clears_fake_distro_lines()
    case_wsl_utf16_distro_output_is_ready()
    case_wsl_decline_stamp_suppresses_prompt_signal()
    case_wsl_drvfs_mountinfo_parser()
    case_wsl_drvfs_detection_uses_mount_fstype_before_syntax()
    print("OK: WSL probe tests pass")


if __name__ == "__main__":
    main()
