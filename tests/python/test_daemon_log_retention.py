"""Exercise retention through the rendered launchd command, with real open FDs."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
CAP = 16 * 1024 * 1024
LOG_NAMES = (
    "drain-launchd.log",
    "fleet-console-fleet-launchd.log",
    "fleet-console-attention-launchd.log",
    "codex-seatd.log",
    "grok-seatd.log",
    "codex-rotate.log",
)
pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS newsyslog")


@pytest.fixture
def launch_tick(tmp_path, request):
    # Render the production plist, replacing only the drain payload. No launchctl,
    # installed plists, real HOME, dispatches or shared-state writes are involved.
    env = dict(os.environ, HOME=str(tmp_path), SKILL_ROOT=str(ROOT))
    env.pop("GOALFLIGHT_DRAIN_LOG", None)
    if hasattr(request, "param"):
        env["GOALFLIGHT_DRAIN_LOG"] = str(tmp_path / ".goal-flight" / request.param)
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/install-drainer.sh"), "--dry-run"],
        env=env, capture_output=True, text=True, check=True,
    )
    argv = plistlib.loads(result.stdout.encode())["ProgramArguments"]
    payload = argv.index(str(ROOT / "scripts/goalflight_dispatch.py"))
    command = argv[:payload] + ["-c", "import sys; print('tick-finished'); sys.exit(7)"]
    log_dir = tmp_path / ".goal-flight"
    log_dir.mkdir()

    def tick():
        proc = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
        assert proc.returncode == 7, proc.stderr
        assert proc.stdout == "tick-finished\n", proc.stdout
        assert not proc.stderr, proc.stderr

    return log_dir, tick


@pytest.mark.parametrize("name", LOG_NAMES)
def test_past_cap_rotates_without_dropping_recent_bytes(launch_tick, name):
    log_dir, tick = launch_tick
    path = log_dir / name
    content = b"x" * CAP + b"\nlatest-before-rotation\n"
    path.write_bytes(content)
    inode = path.stat().st_ino
    tick()
    archive = path.with_name(path.name + ".0")
    assert archive.is_file(), "past-cap log was not rotated"
    assert archive.stat().st_ino == inode, "rotation must preserve the live inode"
    assert archive.read_bytes() == content, "rotation lost the newest content"
    assert path.stat().st_size == 0


def test_under_cap_is_untouched(launch_tick):
    log_dir, tick = launch_tick
    path = log_dir / LOG_NAMES[0]
    # newsyslog measures allocated KiB, so use a file well below the threshold
    # rather than depending on the filesystem's allocation rounding at 16 MiB.
    content = b"x" * (4 * 1024 * 1024)
    path.write_bytes(content)
    before = path.stat()
    tick()
    assert path.read_bytes() == content
    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert list(log_dir.iterdir()) == [path], "under-cap log must not rotate"


@pytest.mark.parametrize("append", [True, False])
def test_open_writer_and_tail_keep_receiving_lines(launch_tick, append):
    log_dir, tick = launch_tick
    path = log_dir / LOG_NAMES[0]
    path.write_bytes(b"x" * CAP + b"\nbefore\n")
    flags = os.O_WRONLY | (os.O_APPEND if append else 0)
    fd = os.open(path, flags)
    try:
        # Include a non-O_APPEND descriptor: rename must not depend on resetting
        # a writer's offset after truncation. The real launchd writer uses append.
        os.lseek(fd, 0, os.SEEK_END)
        with path.open("rb") as tail:
            tail.seek(0, os.SEEK_END)
            tick()
            os.write(fd, b"after-rotation\n")
            assert tail.read() == b"after-rotation\n", "open tail lost subsequent lines"
            archive = path.with_name(path.name + ".0")
            assert archive.is_file(), "open writer was not retained in an archive"
            assert archive.read_bytes().endswith(b"\nbefore\nafter-rotation\n")
            assert archive.stat().st_size == CAP + len(b"\nbefore\nafter-rotation\n")
            with path.open("ab") as next_tick:
                next_tick.write(b"next-invocation\n")
            assert path.read_bytes() == b"next-invocation\n"
    finally:
        os.close(fd)


def test_archive_count_is_bounded(launch_tick):
    log_dir, tick = launch_tick
    path = log_dir / LOG_NAMES[0]
    for generation in range(4):
        path.write_bytes(b"x" * CAP + f"\ngeneration-{generation}\n".encode())
        tick()
    assert sorted(p.name for p in log_dir.iterdir()) == [
        path.name, path.name + ".0", path.name + ".1",
    ]
    assert path.with_name(path.name + ".0").read_bytes().endswith(b"generation-3\n")
    assert path.with_name(path.name + ".1").read_bytes().endswith(b"generation-2\n")


@pytest.mark.parametrize("launch_tick", ["custom drain.log"], indirect=True)
def test_existing_drain_log_override_is_preserved(launch_tick):
    log_dir, tick = launch_tick
    path = log_dir / "custom drain.log"
    path.write_bytes(b"x" * CAP + b"\nlatest\n")
    tick()
    assert path.with_name(path.name + ".0").read_bytes().endswith(b"\nlatest\n")
    assert not (log_dir / LOG_NAMES[0]).exists()


def test_retention_failure_reports_error_and_still_executes_tick(monkeypatch, capsys):
    sys.path.insert(0, str(ROOT / "scripts"))
    import goalflight_daemon_logs as logs

    def fail(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, "/usr/sbin/newsyslog")

    executed = []
    monkeypatch.setattr(logs.subprocess, "run", fail)
    monkeypatch.setattr(logs.os, "execv", lambda *args: executed.append(args))
    logs.main(["/unused/drain.log", "/usr/bin/python3", "drain.py", "--json"])
    assert executed == [("/usr/bin/python3", ["/usr/bin/python3", "drain.py", "--json"])]
    assert "daemon log retention failed" in capsys.readouterr().err
