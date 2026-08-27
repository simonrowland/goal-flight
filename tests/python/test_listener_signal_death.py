"""b-176: listener deaths must be diagnosable, and SIGURG must not kill them.

144 = 128+16 on the POSIX shell convention. On macOS/BSD, 16 is SIGURG, whose
kernel default is discard — a Python listener with SIG_DFL does not die.
Detached-listener refusal is exit 4, so bulk-144 reports are not that path
(b-048 was closed against the wrong cause when it attributed 144 to detached).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_wake as wake  # noqa: E402


@pytest.fixture()
def isolated(monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, str]]:
    td = Path(tempfile.mkdtemp(prefix="gf-listener-signal-"))
    env = {
        "GOALFLIGHT_JOURNAL_DIR": str(td / "journals"),
        "GOALFLIGHT_STATE_DIR": str(td / "state"),
        "GOALFLIGHT_WAKE_LEDGER_DIR": str(td / "wake-ledger"),
        "GOALFLIGHT_MESSAGES_DIR": str(td / "messages"),
        "GOALFLIGHT_TASK_STORE_DIR": str(td / "task-store"),
        "GOAL_FLIGHT_PIDFILE_DIR": str(td / "pids"),
        "GOALFLIGHT_CAPACITY_CONF": os.devnull,
        "GOALFLIGHT_TEST_MODE": "1",
        "GOALFLIGHT_TEST_LISTENER_START_TOKEN": "signal-listener-token",
        "GOALFLIGHT_LISTENER_SLOTS": "2",
    }
    for value in env.values():
        if value != os.devnull:
            Path(value).mkdir(parents=True, exist_ok=True)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GOALFLIGHT_CONTROLLER_LABEL", raising=False)
    monkeypatch.delenv("GOALFLIGHT_CONTROLLER_LEASE_NONCE", raising=False)
    monkeypatch.delenv("GOALFLIGHT_DISPATCH_ID", raising=False)
    project = td / "project"
    project.mkdir()
    return project, {**os.environ, **env}


def _claim(project: Path, label: str = "signal-ctl") -> journal.LeaseIdentity:
    authority = journal.open_or_create_journal(project)
    claimed = authority.claim_or_renew_lease(
        label, principal={"principal_id": f"{label}-principal"}
    )
    assert claimed.committed and claimed.value is not None
    return claimed.value


def _listen_cmd(project: Path, *, label: str, nonce: str, timeout_s: float = 20) -> list[str]:
    return [
        sys.executable,
        str(SCRIPTS / "goalflight_messages.py"),
        "listen",
        "--project-root",
        str(project),
        "--controller-label",
        label,
        "--lease-nonce",
        nonce,
        "--poll-secs",
        "0.01",
        "--timeout-s",
        str(timeout_s),
        "--listener-slots",
        "2",
    ]


def _spawn_listener(
    project: Path,
    env: dict[str, str],
    *,
    label: str,
    nonce: str,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        _listen_cmd(project, label=label, nonce=nonce),
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_armed(
    authority: journal.Journal,
    proc: subprocess.Popen[str],
    *,
    label: str,
    timeout_s: float = 4.0,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out, err = proc.communicate()
            raise AssertionError(
                f"listener exited before arm rc={proc.returncode} stdout={out!r} stderr={err!r}"
            )
        coverage = authority.active_coverage(label)
        if coverage is not None and coverage.get("pid") == proc.pid:
            return dict(coverage)
        time.sleep(0.02)
    raise AssertionError(f"listener pid={proc.pid} never armed")


def _read_nonblocking(stream: object, timeout_s: float = 1.5) -> str:
    if stream is None:
        return ""
    fd = stream.fileno()
    os.set_blocking(fd, False)
    deadline = time.monotonic() + timeout_s
    chunks: list[str] = []
    while time.monotonic() < deadline:
        try:
            piece = stream.read()
        except OSError:
            piece = ""
        if piece:
            chunks.append(piece)
            if "listen:" in "".join(chunks):
                break
        time.sleep(0.02)
    return "".join(chunks)


def test_posix_144_is_sigurg_and_default_disposition_does_not_kill() -> None:
    assert int(signal.SIGURG) == 16
    assert messages.LISTENER_POSIX_SIGNAL_EXIT_BASE + 16 == 144
    assert messages.listener_posix_signal_exit_code(signal.SIGURG) == 144
    assert messages.DETACHED_LISTENER_EXIT_CODE == 4
    assert messages.DETACHED_LISTENER_EXIT_CODE != 144
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(20)"],
    )
    try:
        time.sleep(0.1)
        os.kill(proc.pid, signal.SIGURG)
        time.sleep(0.2)
        pid, raw = os.waitpid(proc.pid, os.WNOHANG)
        assert pid == 0 and raw == 0
        assert proc.poll() is None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


def test_sigurg_keeps_listener_alive_and_holds_slot(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    label = "signal-ctl"
    claimed = _claim(project, label)
    authority = journal.open_or_create_journal(project)
    with wake.register_lease_holder(
        project, controller_label=label, lease_nonce=claimed.nonce
    ):
        proc = _spawn_listener(project, env, label=label, nonce=claimed.nonce)
        try:
            _wait_armed(authority, proc, label=label)
            assert wake.listener_slot_holder_pids(project, controller_label=label) == [
                proc.pid
            ]
            os.kill(proc.pid, signal.SIGURG)
            err = _read_nonblocking(proc.stderr)
            assert proc.poll() is None
            assert "SIGURG" in err
            assert "staying alive" in err
            assert wake.listener_slot_holder_pids(project, controller_label=label) == [
                proc.pid
            ]
            coverage = authority.active_coverage(label)
            assert coverage is not None
            assert coverage.get("pid") == proc.pid
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate(timeout=2)


def test_sigterm_dies_loudly_releases_slot_and_exits_128_plus_signal(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    label = "signal-ctl"
    claimed = _claim(project, label)
    authority = journal.open_or_create_journal(project)
    with wake.register_lease_holder(
        project, controller_label=label, lease_nonce=claimed.nonce
    ):
        proc = _spawn_listener(project, env, label=label, nonce=claimed.nonce)
        _wait_armed(authority, proc, label=label)
        os.kill(proc.pid, signal.SIGTERM)
        try:
            stdout, stderr = proc.communicate(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate(timeout=2)
            raise AssertionError(
                f"listener did not exit on SIGTERM stdout={stdout!r} stderr={stderr!r}"
            )
        expected = messages.listener_posix_signal_exit_code(signal.SIGTERM)
        assert proc.returncode == expected, (proc.returncode, stdout, stderr)
        assert "SIGTERM" in stderr
        assert "releasing slot" in stderr
        assert wake.listener_slot_holder_pids(project, controller_label=label) == []
        coverage = authority.active_coverage(label)
        assert coverage is None
        rows = authority.read_all(
            "SELECT state, exit_reason FROM listener_coverage WHERE pid = ?",
            (proc.pid,),
        )
        assert rows
        assert str(rows[0]["state"]) == "EXITED"
        assert str(rows[0]["exit_reason"]) == "signal"


def test_sigkill_releases_slot_lock_without_journal_exit(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    label = "signal-ctl"
    claimed = _claim(project, label)
    authority = journal.open_or_create_journal(project)
    with wake.register_lease_holder(
        project, controller_label=label, lease_nonce=claimed.nonce
    ):
        proc = _spawn_listener(project, env, label=label, nonce=claimed.nonce)
        _wait_armed(authority, proc, label=label)
        os.kill(proc.pid, signal.SIGKILL)
        _, raw = os.waitpid(proc.pid, 0)
        proc.returncode = -os.WTERMSIG(raw)
        assert os.WIFSIGNALED(raw)
        assert os.WTERMSIG(raw) == int(signal.SIGKILL)
        assert messages.LISTENER_POSIX_SIGNAL_EXIT_BASE + int(signal.SIGKILL) == 137
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if not wake.listener_slot_holder_pids(project, controller_label=label):
                break
            time.sleep(0.02)
        assert wake.listener_slot_holder_pids(project, controller_label=label) == []
        coverage = authority.active_coverage(label)
        assert coverage is not None
        assert coverage.get("pid") == proc.pid


def test_controller_mail_documents_144_is_not_detached() -> None:
    doctrine = (
        Path(__file__).resolve().parents[2] / "protocols" / "controller-mail.md"
    ).read_text(encoding="utf-8")
    assert "144 is SIGURG" in doctrine
    assert "detached exits 4" in doctrine or "detached-listener deaths" in doctrine
    assert "| 4 | Detached-listener refusal" in doctrine
