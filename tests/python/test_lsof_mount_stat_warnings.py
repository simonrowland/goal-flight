"""lsof mount stat() warnings must not turn seat and scratch probes into "unknown".

On 2026-10-27 smbfs volumes were mounted under /Volumes. From then on every
lsof run printed a three-line "can't stat() smbfs file system" warning on
stderr with exit status 0. Three callers treated any stderr as a failed probe:
the seat cwd census (worker_cwd_unknown, so seats were never released or
resumed), the seat open-handle probe (dedupe skipped every seat), and scratch
retention (kept everything as lsof_error). Every other diagnostic must still
fail closed.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import goalflight_reap_dispatch_homes as reaper  # noqa: E402
import goalflight_seat_dedupe as dedupe  # noqa: E402
import goalflight_subprocess  # noqa: E402
import goalflight_worktree_pool as pool  # noqa: E402

# Verbatim from `lsof -nP -d cwd -Fpfn` on the controller laptop, 2026-10-27.
MOUNT_WARNINGS = (
    "lsof: WARNING: can't stat() smbfs file system /Volumes/StudioStack4TB\n"
    "      Output information may be incomplete.\n"
    '      assuming "dev=36000007" from mount table\n'
    "lsof: WARNING: can't stat() smbfs file system /Volumes/warpx-data\n"
    "      Output information may be incomplete.\n"
    '      assuming "dev=36000008" from mount table\n'
)
OTHER_DIAGNOSTICS = (
    "lsof: WARNING: can't stat() process 424242: Permission denied\n",
    "lsof: no pwd entry for UID 501\n",
    MOUNT_WARNINGS + "lsof: unexpected diagnostic\n",
    # lsof prints this bare line when it drops a mount it cannot resolve.
    "      Output information may be incomplete.\n",
    '      assuming "dev=36000007" from mount table\n',
)


def test_allowlist_accepts_only_the_mount_warning_family() -> None:
    allow = goalflight_subprocess.lsof_stderr_is_mount_stat_warnings_only
    assert allow("")
    assert allow(MOUNT_WARNINGS)
    assert allow(MOUNT_WARNINGS.encode())
    for stderr in OTHER_DIAGNOSTICS:
        assert not allow(stderr)
        assert not allow(stderr.encode())


def _fake_run(returncode: int, stdout, stderr):
    def run(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)
    return run


def test_cwd_census_survives_mount_warnings(monkeypatch, tmp_path: Path) -> None:
    rows = f"p{os.getpid()}\nfcwd\nn{tmp_path}\n"
    monkeypatch.setattr(
        pool.goalflight_subprocess, "run_with_deadline", _fake_run(0, rows, MOUNT_WARNINGS)
    )
    assert pool._process_cwd_snapshot(timeout_s=5) == [(os.getpid(), str(tmp_path))]


@pytest.mark.parametrize("stderr", OTHER_DIAGNOSTICS)
def test_cwd_census_other_diagnostics_stay_unknown(monkeypatch, tmp_path: Path, stderr: str) -> None:
    rows = f"p{os.getpid()}\nfcwd\nn{tmp_path}\n"
    monkeypatch.setattr(
        pool.goalflight_subprocess, "run_with_deadline", _fake_run(0, rows, stderr)
    )
    assert pool._process_cwd_snapshot(timeout_s=5) is None


def test_cwd_census_exit_one_stays_unknown(monkeypatch, tmp_path: Path) -> None:
    rows = f"p{os.getpid()}\nfcwd\nn{tmp_path}\n"
    monkeypatch.setattr(
        pool.goalflight_subprocess, "run_with_deadline", _fake_run(1, rows, MOUNT_WARNINGS)
    )
    assert pool._process_cwd_snapshot(timeout_s=5) is None


def test_seat_open_handle_probe_survives_mount_warnings(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        dedupe.goalflight_subprocess, "run_with_deadline",
        _fake_run(1, b"", MOUNT_WARNINGS.encode()),
    )
    assert dedupe._seat_has_open_handles(tmp_path, 5) is False


@pytest.mark.parametrize("stderr", OTHER_DIAGNOSTICS)
def test_seat_open_handle_probe_other_diagnostics_raise(monkeypatch, tmp_path: Path, stderr: str) -> None:
    monkeypatch.setattr(
        dedupe.goalflight_subprocess, "run_with_deadline",
        _fake_run(1, b"", stderr.encode()),
    )
    with pytest.raises(OSError):
        dedupe._seat_has_open_handles(tmp_path, 5)


def test_scratch_retention_snapshot_survives_mount_warnings(monkeypatch, tmp_path: Path) -> None:
    candidate = tmp_path / "old-owner"
    candidate.mkdir()
    output = "p123\0cPython\0\nf0\0tREG\0n/elsewhere/file\0\n"
    monkeypatch.setattr(
        reaper.goalflight_subprocess, "run_with_deadline", _fake_run(0, output, MOUNT_WARNINGS)
    )
    result = reaper._legacy_scratch_process_snapshot([candidate])
    assert result[os.path.abspath(candidate)][1] != "lsof_error"


@pytest.mark.parametrize("stderr", OTHER_DIAGNOSTICS)
def test_scratch_retention_snapshot_other_diagnostics_keep_all(monkeypatch, tmp_path: Path, stderr: str) -> None:
    candidate = tmp_path / "old-owner"
    candidate.mkdir()
    output = "p123\0cPython\0\nf0\0tREG\0n/elsewhere/file\0\n"
    monkeypatch.setattr(
        reaper.goalflight_subprocess, "run_with_deadline", _fake_run(0, output, stderr)
    )
    result = reaper._legacy_scratch_process_snapshot([candidate])
    assert result[os.path.abspath(candidate)] == (False, "lsof_error")


@pytest.mark.parametrize(
    "rows",
    (
        "p{pid}\nfcwd\n",  # newline-terminated but missing the name record
        "p{pid}\nn{path}\n",  # name without the cwd fd record
        "pnotapid\nfcwd\nn{path}\n",
        "p{pid}\nfcwd\nn{path}\nx-unexpected\n",
    ),
)
def test_cwd_census_malformed_records_stay_unknown(monkeypatch, tmp_path: Path, rows: str) -> None:
    stdout = rows.format(pid=os.getpid(), path=tmp_path)
    monkeypatch.setattr(
        pool.goalflight_subprocess, "run_with_deadline", _fake_run(0, stdout, MOUNT_WARNINGS)
    )
    assert pool._process_cwd_snapshot(timeout_s=5) is None


def test_scratch_retention_exit_one_keeps_all(monkeypatch, tmp_path: Path) -> None:
    candidate = tmp_path / "old-owner"
    candidate.mkdir()
    output = "p123\0cPython\0\nf0\0tREG\0n/elsewhere/file\0\n"
    monkeypatch.setattr(
        reaper.goalflight_subprocess, "run_with_deadline", _fake_run(1, output, MOUNT_WARNINGS)
    )
    result = reaper._legacy_scratch_process_snapshot([candidate])
    assert result[os.path.abspath(candidate)] == (False, "lsof_error")
