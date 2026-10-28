"""Daemon log wrapper safety tests."""

from __future__ import annotations

from pathlib import Path

import pytest

import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_daemon_logs as daemon_logs  # noqa: E402


def test_wrapper_does_not_pass_symlinked_log_to_newsyslog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outside = tmp_path / "outside.log"
    outside.write_bytes(b"keep")
    symlink = tmp_path / "drain-launchd.log"
    symlink.symlink_to(outside)
    calls: list[list[str]] = []
    sweeps: list[dict] = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)

    def stop_exec(_path, _argv):
        raise RuntimeError("stop before drain")

    def fake_popen(_argv, **kwargs):
        sweeps.append(kwargs)

    monkeypatch.setattr(daemon_logs.subprocess, "run", fake_run)
    monkeypatch.setattr(daemon_logs.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(daemon_logs.os, "execv", stop_exec)
    with pytest.raises(RuntimeError, match="stop before drain"):
        daemon_logs.main([str(symlink), "python3", "-c", "pass"])

    assert calls
    assert str(symlink) not in calls[0]
    assert len(sweeps) == 1
    assert sweeps[0]["start_new_session"] is True
    assert Path(sweeps[0]["stdout"].name) == tmp_path / "dispatch-retention.log"
    assert sweeps[0]["stderr"] is sweeps[0]["stdout"]
    assert outside.read_bytes() == b"keep"
