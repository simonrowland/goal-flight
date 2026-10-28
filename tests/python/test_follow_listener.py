"""t-292: persistent stdout lines are live wakes, not exit-buffered output."""

from __future__ import annotations

import contextlib
from contextlib import ExitStack
import ctypes
import errno
import io
import json
import os
from pathlib import Path
import select
import shlex
import signal
import sqlite3
import struct
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from machine_isolation import AMBIENT_IDENTITY_ENV, isolated_machine_env, wait_until


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
import goalflight_fleet_console as fleet  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_session_status as sessions  # noqa: E402
import goalflight_wake as wake  # noqa: E402


@pytest.fixture()
def isolated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, dict[str, str], journal.LeaseIdentity]:
    label = "follow-test"
    env = dict(os.environ)
    for key in AMBIENT_IDENTITY_ENV:
        env.pop(key, None)
        monkeypatch.delenv(key, raising=False)
    env.pop("GOALFLIGHT_WAKE_LEDGER", None)
    env.update(isolated_machine_env(tmp_path))
    env.update(
        {
            "GOALFLIGHT_ROOT": str(ROOT),
            "GOALFLIGHT_CONTROLLER_LABEL": label,
            "GOALFLIGHT_PROCESS_ROLE": "controller",
            "GOALFLIGHT_TEST_MODE": "1",
            "GOALFLIGHT_WAKE_ENTRY_POLL_S": "0",
        }
    )
    ps_dir = tmp_path / "absent-supervisor-process-listing"
    ps_dir.mkdir()
    ps_shim = ps_dir / "ps"
    ps_shim.write_text(
        "#!/bin/sh\nprintf '%s\\n' '1 init'\nexit 0\n",
        encoding="utf-8",
    )
    ps_shim.chmod(0o755)
    env["PATH"] = f"{ps_dir}:{env.get('PATH', '')}"
    monkeypatch.setattr(wake, "_process_listing", lambda **_kwargs: [])
    for key, value in env.items():
        if key.startswith("GOAL") or key in {"PYTHONUNBUFFERED", "PATH"}:
            monkeypatch.setenv(key, value)
    project = tmp_path / "project"
    project.mkdir()
    authority = journal.open_or_create_journal(project)
    claimed = authority.claim_or_renew_lease(
        label,
        principal={"principal_id": "follow-test-principal"},
    )
    assert claimed.committed and claimed.value is not None
    lease = claimed.value
    with wake.register_lease_holder(
        project, controller_label=lease.label, lease_nonce=lease.nonce
    ):
        yield project, env, lease


def _follow_command(
    project: Path,
    lease: journal.LeaseIdentity,
    *,
    heartbeat_s: float,
    poll_s: float = 0.01,
) -> list[str]:
    return [
        sys.executable,
        str(SCRIPTS / "goalflight_messages.py"),
        "follow",
        "--project-root",
        str(project),
        "--controller-label",
        lease.label,
        "--lease-nonce",
        lease.nonce,
        "--poll-secs",
        str(poll_s),
        "--heartbeat-secs",
        str(heartbeat_s),
        "--frontier-floor-secs",
        str(heartbeat_s * 20),
    ]


def _cpu_sampled_bootstrap(
    tmp_path: Path,
    name: str,
    command: list[str],
    samples_path: Path,
    *,
    extra_setup: tuple[str, ...] = (),
    materialization_counter_path: Path | None = None,
) -> Path:
    materialization_setup: tuple[str, ...] = ()
    if materialization_counter_path is not None:
        materialization_setup = (
            "import os",
            "from pathlib import Path",
            f"materialization_counter_path = Path({str(materialization_counter_path)!r})",
            "materialization_count = 0",
            "def observe_materialization(original):",
            "    def counted(*args, **kwargs):",
            "        global materialization_count",
            "        materialization_count += 1",
            "        temp = materialization_counter_path.with_name(",
            "            f'{materialization_counter_path.name}.{os.getpid()}.tmp'",
            "        )",
            "        temp.write_text(str(materialization_count), encoding='utf-8')",
            "        temp.replace(materialization_counter_path)",
            "        return original(*args, **kwargs)",
            "    return counted",
            "goalflight_messages._envelopes_with_rows = observe_materialization(",
            "    goalflight_messages._envelopes_with_rows",
            ")",
        )
    bootstrap = tmp_path / f"{name}.py"
    bootstrap.write_text(
        "\n".join(
            [
                "import sys, time",
                f"sample_path = {str(samples_path)!r}",
                f"args = {command[2:]!r}",
                "last_cpu = time.process_time()",
                "original_sleep = time.sleep",
                "def measured_sleep(seconds):",
                "    global last_cpu",
                "    now = time.process_time()",
                "    with open(sample_path, 'a', encoding='utf-8') as out:",
                "        out.write(f'{now - last_cpu:.9f}\\n')",
                "    last_cpu = now",
                "    return original_sleep(seconds)",
                "time.sleep = measured_sleep",
                f"sys.path.insert(0, {str(SCRIPTS)!r})",
                "import goalflight_messages",
                *materialization_setup,
                *extra_setup,
                "raise SystemExit(goalflight_messages.main(args))",
            ]
        ),
        encoding="utf-8",
    )
    return bootstrap


def _materialization_count(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0


def _post_pending_backlog(
    project: Path,
    env: dict[str, str],
    lease: journal.LeaseIdentity,
    *,
    prefix: str,
    count: int = 250,
) -> None:
    for index in range(count):
        messages.post_message(
            dispatch_id=f"{prefix}-{index:03d}",
            msg_type="controller-notice",
            payload={"text": f"pending {index}"},
            messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
            source={"node": "peer", "adapter": "pytest", "transport": "controller"},
            addressee=messages.controller_addressee(lease.label, project_root=project),
            event_ts="2020-01-01T00:00:00Z",
        )


class _JsonLineReader:
    def __init__(self, stream) -> None:
        self.stream = stream
        self.buffer = b""

    def read(self, timeout_s: float = 10.0) -> tuple[bytes, dict[str, object]]:
        deadline = time.monotonic() + timeout_s
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("timed out waiting for a live follow line")
            readable, _, _ = select.select([self.stream.fileno()], [], [], remaining)
            if not readable:
                raise AssertionError("timed out waiting for a live follow line")
            chunk = os.read(self.stream.fileno(), 4096)
            if not chunk:
                raise AssertionError("follow stream closed before the expected line")
            self.buffer += chunk
        raw, self.buffer = self.buffer.split(b"\n", 1)
        return raw + b"\n", json.loads(raw)


def _spawn_follow(
    project: Path,
    env: dict[str, str],
    lease: journal.LeaseIdentity,
    *,
    heartbeat_s: float,
    poll_s: float = 0.01,
    mode: str | None = None,
) -> subprocess.Popen[bytes]:
    child_env = dict(env)
    if mode is not None:
        child_env["GOALFLIGHT_WAKE_MODE"] = mode
    return subprocess.Popen(
        _follow_command(
            project,
            lease,
            heartbeat_s=heartbeat_s,
            poll_s=poll_s,
        ),
        cwd=project,
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _watch_command(
    project: Path,
    lease: journal.LeaseIdentity,
    *,
    timeout_s: float = 3,
) -> list[str]:
    return [
        sys.executable,
        str(SCRIPTS / "goalflight_messages.py"),
        "listen",
        "--project-root",
        str(project),
        "--controller-label",
        lease.label,
        "--lease-nonce",
        lease.nonce,
        "--watch-follow",
        "--json",
        "--poll-secs",
        "0.01",
        "--timeout-s",
        str(timeout_s),
    ]


def _backup_command(
    project: Path,
    lease: journal.LeaseIdentity,
    *,
    timeout_s: float = 60,
) -> list[str]:
    return [
        sys.executable,
        str(SCRIPTS / "goalflight_messages.py"),
        "listen",
        "--project-root",
        str(project),
        "--controller-label",
        lease.label,
        "--lease-nonce",
        lease.nonce,
        "--listener-slots",
        "1",
        "--report-pending",
        "--json",
        "--poll-secs",
        "0.01",
        "--timeout-s",
        str(timeout_s),
    ]


def _wait_for_waiter_kind(
    project: Path,
    label: str,
    kind: str,
    pid: int,
    *,
    timeout_s: float = 60,
) -> None:
    def _matched() -> bool:
        waiters = wake.live_waiters(
            project,
            controller_label=label,
            kinds={kind},
        ) or []
        return any(row.pid == pid for row in waiters)

    wait_until(
        _matched,
        timeout_s=timeout_s,
        interval_s=0.01,
        message=f"{kind} waiter for pid={pid}",
    )


def _mac_disk_bytes_written(pid: int) -> int:
    """Read macOS proc_pid_rusage V4's cumulative disk-write counter."""
    if sys.platform != "darwin":
        raise RuntimeError("macOS-only disk-write counter")
    proc = ctypes.CDLL(None, use_errno=True).proc_pid_rusage
    proc.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    proc.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(512)
    if proc(pid, 4, ctypes.byref(buffer)) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    # uuid[16], followed by 17 uint64 fields through diskio_bytesread;
    # diskio_byteswritten is the next field in rusage_info_v4.
    return struct.unpack_from("<Q", buffer.raw, 16 + (17 * 8))[0]


def _wait_for_monitor_slot(project: Path, label: str, pid: int) -> None:
    _wait_for_waiter_kind(project, label, wake.MONITOR_KIND, pid)


@pytest.mark.parametrize("mode", ["poll", "gated", "notify"])
def test_follow_reader_preserves_row_order_and_cursor_cas(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    mode: str,
) -> None:
    project, env, lease = isolated
    writer = journal.Journal(project)
    for index in range(3):
        messages.post_message(
            dispatch_id=f"reader-equivalence-{index}",
            msg_type="controller-notice",
            payload={"text": f"row {index}"},
            messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
            source={"node": "peer", "adapter": "pytest", "transport": "controller"},
            addressee=messages.controller_addressee(lease.label, project_root=project),
        )
    before = writer.cursor_peek(lease.label, nonce=lease.nonce)
    expected = [
        (str(item["stream_id"]), int(item["stream_seq"]))
        for item in before.items
        if item.get("wake_class") == "waking"
    ]
    assert len(expected) == 3

    proc = _spawn_follow(
        project,
        env,
        lease,
        heartbeat_s=30,
        poll_s=0.1,
        mode=mode,
    )
    assert proc.stdout is not None
    reader = _JsonLineReader(proc.stdout)
    observed: list[tuple[str, int]] = []
    try:
        deadline = time.monotonic() + 5
        while len(observed) < len(expected):
            _raw, record = reader.read(timeout_s=max(0.01, deadline - time.monotonic()))
            if record["kind"] == "event":
                payload = record["payload"]
                observed.append((str(payload["stream_id"]), int(payload["stream_seq"])))
        assert observed == expected

        after_delivery = writer.cursor_peek(lease.label, nonce=lease.nonce)
        assert after_delivery.cursor_version == before.cursor_version
        assert [
            (str(item["stream_id"]), int(item["stream_seq"]))
            for item in after_delivery.items
        ] == expected

        advances = {stream: seq for stream, seq in expected}
        advanced = writer.advance_cursor(
            lease.label,
            nonce=lease.nonce,
            expected_cursor_version=before.cursor_version,
            expected_stream_snapshots={
                stream: before.stream_snapshots[stream] for stream in advances
            },
            advances=advances,
            actor=f"{mode}-equivalence-test",
        )
        assert advanced.committed
        assert writer.cursor_peek(lease.label, nonce=lease.nonce).items == ()
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=3)


@pytest.mark.parametrize(
    ("mode", "persistent"),
    [("poll", False), ("gated", True), ("notify", True)],
)
def test_follow_reader_mode_matches_wake_mode(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    persistent: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, env, lease = isolated
    monkeypatch.setenv("GOALFLIGHT_WAKE_MODE", mode)
    messages.post_message(
        dispatch_id="persistent-reader-event",
        msg_type="controller-notice",
        payload={"text": "read through one persistent connection"},
        messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
        source={"node": "peer", "adapter": "pytest", "transport": "controller"},
        addressee=messages.controller_addressee(lease.label, project_root=project),
    )
    open_reader = journal.Journal.open_reader
    persistent_modes: list[bool] = []

    def track_reader(cls, project_root, **kwargs):
        persistent_modes.append(bool(kwargs.get("persistent", False)))
        return open_reader(project_root, **kwargs)

    monkeypatch.setattr(journal.Journal, "open_reader", classmethod(track_reader))
    original_connect = journal.Journal._connect
    connections: list[sqlite3.Connection] = []
    persistent_connection_ids: set[int] = set()

    def track_connect(self, *args, **kwargs):
        connection = original_connect(self, *args, **kwargs)
        connections.append(connection)
        if getattr(self, "_persistent_reader", False):
            persistent_connection_ids.add(id(connection))
        return connection

    monkeypatch.setattr(journal.Journal, "_connect", track_connect)
    _pin_listener_resolution(monkeypatch, lease)
    monkeypatch.setattr(messages, "_follow_stdout_refusal", lambda _stream: None)
    monkeypatch.setattr(
        messages,
        "_write_follow_record",
        lambda record, **_kwargs: record.get("kind") != "event",
    )
    monkeypatch.setattr(messages, "_silence_broken_stdout", lambda _stream: None)
    assert messages.main(_follow_argv(project, lease)) == 0
    assert "stdout must be owned" not in capsys.readouterr().err
    assert persistent_modes == [persistent]
    if persistent:
        assert len(persistent_connection_ids) == 1
    else:
        assert not persistent_connection_ids
        assert len(connections) > 1


@pytest.mark.skipif(not hasattr(select, "kqueue"), reason="native kqueue is unavailable")
def test_follow_notify_wakes_on_wal_write_before_safety_poll(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    project, env, lease = isolated
    proc = _spawn_follow(
        project,
        env,
        lease,
        heartbeat_s=30,
        poll_s=5,
        mode="notify",
    )
    assert proc.stdout is not None
    reader = _JsonLineReader(proc.stdout)
    try:
        _raw, first = reader.read()
        assert first["kind"] == "heartbeat"
        _wait_for_monitor_slot(project, lease.label, proc.pid)
        started = time.monotonic()
        messages.post_message(
            dispatch_id="notify-wal-wake",
            msg_type="controller-notice",
            payload={"text": "the WAL write wakes follow"},
            messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
            source={"node": "peer", "adapter": "pytest", "transport": "controller"},
            addressee=messages.controller_addressee(lease.label, project_root=project),
        )
        while True:
            _raw, record = reader.read(timeout_s=1.5)
            if record["kind"] == "event":
                break
        assert record["payload"]["dispatch_id"] == "notify-wal-wake"
        assert time.monotonic() - started < 1.5
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=3)


def test_follow_poll_delivers_after_wal_inode_replacement(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    project, env, lease = isolated
    proc = _spawn_follow(
        project,
        env,
        lease,
        heartbeat_s=30,
        poll_s=0.05,
        mode="poll",
    )
    assert proc.stdout is not None
    reader = _JsonLineReader(proc.stdout)
    try:
        _raw, first = reader.read()
        assert first["kind"] == "heartbeat"
        _wait_for_monitor_slot(project, lease.label, proc.pid)

        wal = Path(f"{journal.resolve_journal_path(project)}-wal")
        connection = sqlite3.connect(
            journal.resolve_journal_path(project),
            timeout=5,
            isolation_level=None,
        )
        try:
            checkpoint = tuple(
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            )
        finally:
            connection.close()
        assert checkpoint[0] == 0, checkpoint
        wal.touch(exist_ok=True)
        prior = wal.stat()
        replacement = wal.with_name(f"{wal.name}.replacement")
        replacement.write_bytes(b"")
        os.replace(replacement, wal)
        replaced = wal.stat()
        assert (prior.st_dev, prior.st_ino) != (replaced.st_dev, replaced.st_ino)

        messages.post_message(
            dispatch_id="poll-wal-replacement-event",
            msg_type="controller-notice",
            payload={"text": "deliver after replacing the live WAL inode"},
            messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
            source={"node": "peer", "adapter": "pytest", "transport": "controller"},
            addressee=messages.controller_addressee(lease.label, project_root=project),
        )
        while True:
            _raw, record = reader.read(timeout_s=5)
            if record.get("kind") == "event":
                break
        assert record["payload"]["dispatch_id"] == "poll-wal-replacement-event"
        assert proc.poll() is None, proc.stderr.read().decode(errors="replace")
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=3)


def test_follow_gated_reader_reopens_once_after_wal_replacement(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _env, lease = isolated
    monkeypatch.setenv("GOALFLIGHT_WAKE_MODE", "gated")
    monkeypatch.setenv("GOALFLIGHT_TEST_MODE", "1")
    monkeypatch.setattr(messages, "_follow_stdout_refusal", lambda _stream: None)
    _pin_listener_resolution(monkeypatch, lease)
    monkeypatch.setattr(messages._ListenerDeathWatch, "install", lambda _self: None)
    monkeypatch.setattr(messages._ListenerDeathWatch, "restore", lambda _self: None)
    monkeypatch.setattr(
        messages,
        "_follow_frontier_snapshot",
        lambda _store: {"kind": "frontier"},
    )
    records: list[dict[str, object]] = []
    monkeypatch.setattr(
        messages,
        "_write_follow_record",
        lambda record, **_kwargs: records.append(record) or True,
    )
    messages.post_message(
        dispatch_id="gated-wal-replacement-recovery",
        msg_type="controller-notice",
        payload={"text": "keep the live WAL present for replacement"},
        messages_dir=Path(_env["GOALFLIGHT_MESSAGES_DIR"]),
        source={"node": "peer", "adapter": "pytest", "transport": "controller"},
        addressee=messages.controller_addressee(lease.label, project_root=project),
    )

    wal = Path(f"{journal.resolve_journal_path(project)}-wal")
    peek_calls = 0

    def fail_cursor_peek(authority, *args, **kwargs):
        nonlocal peek_calls
        peek_calls += 1
        if peek_calls == 1:
            before = wal.stat()
            replacement = wal.with_name(f"{wal.name}.replacement")
            replacement.write_bytes(b"")
            os.replace(replacement, wal)
            after = wal.stat()
            assert (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(journal.Journal, "cursor_peek", fail_cursor_peek)
    original_reset = messages._reset_persistent_journal_reader
    resets = []

    def track_reset(authority):
        resets.append(authority)
        original_reset(authority)

    monkeypatch.setattr(messages, "_reset_persistent_journal_reader", track_reset)
    sleep_calls: list[float] = []
    original_sleep = time.sleep

    def track_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        original_sleep(seconds)

    monkeypatch.setattr(time, "sleep", track_sleep)
    assert messages.main(_follow_argv(project, lease)) == 2
    assert peek_calls == 2
    assert len(resets) == 1
    assert sleep_calls and sleep_calls[0] > 0
    assert any(record.get("kind") == "heartbeat" for record in records)
    assert any(
        record.get("payload", {}).get("type") == "listener-fault"
        for record in records
    )


@pytest.mark.skipif(not hasattr(select, "kqueue"), reason="native kqueue is unavailable")
def test_listen_notify_wakes_on_wal_write_before_safety_poll(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    project, env, lease = isolated
    child_env = dict(env)
    child_env["GOALFLIGHT_WAKE_MODE"] = "notify"
    command = _backup_command(project, lease, timeout_s=8)
    command[command.index("--poll-secs") + 1] = "5"
    proc = subprocess.Popen(
        command,
        cwd=project,
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_waiter_kind(project, lease.label, "listener", proc.pid)
        # Let the arm snapshot complete so this measures the steady-state wait.
        time.sleep(0.25)
        started = time.monotonic()
        messages.post_message(
            dispatch_id="notify-listen-wal-wake",
            msg_type="controller-notice",
            payload={"text": "the WAL write wakes listen"},
            messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
            source={"node": "peer", "adapter": "pytest", "transport": "controller"},
            addressee=messages.controller_addressee(lease.label, project_root=project),
        )
        assert proc.wait(timeout=2) == 0
        assert time.monotonic() - started < 2
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=3)


def test_listen_gated_keeps_delivery_on_the_existing_poll_cadence(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    project, env, lease = isolated
    child_env = dict(env)
    child_env["GOALFLIGHT_WAKE_MODE"] = "gated"
    command = _backup_command(project, lease, timeout_s=8)
    command[command.index("--poll-secs") + 1] = "0.2"
    proc = subprocess.Popen(
        command,
        cwd=project,
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_waiter_kind(project, lease.label, "listener", proc.pid)
        time.sleep(0.25)
        messages.post_message(
            dispatch_id="gated-listen-poll-wake",
            msg_type="controller-notice",
            payload={"text": "gated listen preserves poll latency"},
            messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
            source={"node": "peer", "adapter": "pytest", "transport": "controller"},
            addressee=messages.controller_addressee(lease.label, project_root=project),
        )
        assert proc.wait(timeout=2) == 0
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=3)


@pytest.mark.skipif(not hasattr(select, "kqueue"), reason="native kqueue is unavailable")
def test_wal_notifier_rearms_after_replacement_and_deletion(tmp_path: Path) -> None:
    database = tmp_path / "journal.sqlite"
    database.write_bytes(b"database")
    wal = Path(f"{database}-wal")
    wal.write_bytes(b"old wal")
    notifier = messages._JournalChangeNotifier(database)
    try:
        old_identity = notifier._wal_identity
        replacement = tmp_path / "replacement-wal"
        replacement.write_bytes(b"new wal")
        os.replace(replacement, wal)
        assert notifier.wait(1.0)
        assert notifier._wal_identity != old_identity
        assert notifier.take_wal_replacement()
        assert not notifier.take_wal_replacement()

        with wal.open("ab") as stream:
            stream.write(b"commit")
        assert notifier.wait(1.0)

        wal.unlink()
        assert notifier.wait(1.0)
        assert notifier._wal_fd is None
        wal.write_bytes(b"recreated wal")
        assert notifier.wait(1.0)
        assert notifier._wal_fd is not None
    finally:
        notifier.close()


@pytest.mark.skipif(not hasattr(select, "kqueue"), reason="native kqueue is unavailable")
def test_wal_notifier_failure_disables_notify_and_logs_once(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "journal.sqlite"
    database.write_bytes(b"database")
    notifier = messages._JournalChangeNotifier(database)

    class BrokenQueue:
        def control(self, *_args, **_kwargs):
            raise OSError("injected kqueue failure")

        def close(self) -> None:
            pass

    notifier._queue = BrokenQueue()
    try:
        assert not notifier.wait(0.01)
        assert not notifier.active
        first_error = capsys.readouterr().err
        assert first_error.count("poll mode") == 1
        assert not notifier.wait(0.01)
        assert capsys.readouterr().err == ""
    finally:
        notifier.close()


def test_journal_change_gate_uses_file_stats_with_unchanged_data_version(
    tmp_path: Path,
) -> None:
    database = tmp_path / "journal.sqlite"
    wal = Path(f"{database}-wal")
    shm = Path(f"{database}-shm")
    database.write_bytes(b"database")
    wal.write_bytes(b"first")
    gate = messages._JournalChangeGate(database)

    assert gate.changed(7)
    assert not gate.changed(7)
    shm.write_bytes(b"first lock metadata")
    assert not gate.changed(7)
    shm.write_bytes(b"changed lock metadata")
    assert not gate.changed(7)
    first_wal = wal.stat().st_ino
    replacement = tmp_path / "replacement-wal"
    replacement.write_bytes(b"replacement")
    os.replace(replacement, wal)
    assert wal.stat().st_ino != first_wal
    assert gate.changed(7)
    assert gate.wal_replaced
    assert not gate.changed(7)
    assert not gate.wal_replaced
    wal.write_bytes(b"second version")
    assert gate.changed(7)
    assert not gate.changed(7)


def test_journal_change_gate_resets_when_reader_connection_changes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "journal.sqlite"
    database.write_bytes(b"database")
    gate = messages._JournalChangeGate(database)
    first_reader = object()
    replacement_reader = object()

    assert gate.changed(7, reader_connection=first_reader)
    assert not gate.changed(7, reader_connection=first_reader)
    assert gate.changed(7, reader_connection=replacement_reader)
    assert not gate.changed(7, reader_connection=replacement_reader)


def test_gated_follow_retries_a_lost_ring_claim_without_a_journal_write(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, env, lease = isolated
    monkeypatch.setenv(messages.WAKE_MODE_ENV, "gated")
    claim_calls: list[float] = []
    real_claim_ring = wake.claim_ring

    def claim_ring(*args, **kwargs):
        claim_calls.append(time.monotonic())
        if len(claim_calls) == 1:
            return False
        return real_claim_ring(*args, **kwargs)

    monkeypatch.setattr(wake, "claim_ring", claim_ring)
    monkeypatch.setattr(messages, "_follow_stdout_refusal", lambda _stream: None)
    cap = _LiveCapture(capsys)
    thread, results = _run_in_thread(_follow_argv(project, lease))
    try:
        cap.await_count(_is_heartbeat, 1)
        messages.post_message(
            dispatch_id="gated-ring-claim-retry",
            msg_type="controller-notice",
            payload={"text": "retry on the listener poll deadline"},
            messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
            source={"node": "peer", "adapter": "pytest", "transport": "controller"},
            addressee=messages.controller_addressee(
                lease.label,
                project_root=project,
            ),
        )
        cap.await_count(
            lambda record: _is_follow_event(record, "controller-notice"),
            1,
        )
        assert len(claim_calls) >= 2, "the failed ring claim was not retried"
        assert claim_calls[1] - claim_calls[0] <= 0.25
    finally:
        _release_lease(project, lease)
        thread.join(5)
    cap.pump()
    assert not thread.is_alive(), cap.stderr
    assert results == [3]


def test_wal_notifier_rearms_before_returning_signal_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signal_filter = object()
    directory_fd = 23
    notifier = messages._JournalChangeNotifier.__new__(messages._JournalChangeNotifier)
    notifier.active = True
    notifier._queue = type(
        "Queue",
        (),
        {
            "control": lambda _self, *_args: [
                type("Event", (), {"filter": signal_filter, "ident": 2})(),
                type("Event", (), {"filter": 999, "ident": directory_fd})(),
            ],
        },
    )()
    notifier._signal_filter = signal_filter
    notifier._directory_fd = directory_fd
    notifier._wal_fd = 24
    rearmed: list[bool] = []
    monkeypatch.setattr(notifier, "_rearm_wal", lambda: rearmed.append(True) or True)

    assert notifier.wait(0.1)
    assert rearmed == [True]


def test_poll_wake_mode_is_default_and_does_not_create_notifier(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("GOALFLIGHT_WAKE_MODE", raising=False)
    assert messages._wake_mode() == "poll"

    def unexpected_notifier(*_args, **_kwargs):
        pytest.fail("poll mode must not create a notifier")

    monkeypatch.setattr(messages, "_JournalChangeNotifier", unexpected_notifier)
    assert messages._new_journal_notifier("poll", tmp_path / "journal.sqlite") is None
    monkeypatch.setenv("GOALFLIGHT_WAKE_MODE", "gated")
    assert messages._wake_mode() == "gated"
    assert messages._new_journal_notifier("gated", tmp_path / "journal.sqlite") is None


def test_live_lines_flush_before_exit_and_heartbeat_cadence_carries_mail(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    project, env, lease = isolated
    proc = _spawn_follow(project, env, lease, heartbeat_s=0.12)
    assert proc.stdout is not None
    reader = _JsonLineReader(proc.stdout)
    try:
        first_raw, first = reader.read()
        first_at = time.monotonic()
        assert first["kind"] == "heartbeat"
        assert proc.poll() is None, "live output must arrive before process exit"
        assert len(first_raw) < messages.STREAM_PIPE_BUF_BYTES

        frontier_raw, frontier = reader.read()
        assert frontier["kind"] == "frontier"
        assert frontier["payload"]["advisory"] == "information-only"
        assert len(frontier_raw) < messages.STREAM_PIPE_BUF_BYTES

        second_raw, second = reader.read()
        second_at = time.monotonic()
        assert second["kind"] == "heartbeat"
        assert second["payload"]["seq"] == 2
        assert 0.07 <= second_at - first_at <= 0.8
        assert proc.poll() is None
        assert len(second_raw) < messages.STREAM_PIPE_BUF_BYTES

        messages.post_message(
            dispatch_id="follow-live-event",
            msg_type="controller-notice",
            payload={"text": "payload arrived while the stream stayed alive"},
            messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
            source={"node": "peer", "adapter": "pytest", "transport": "controller"},
            addressee=messages.controller_addressee(lease.label, project_root=project),
        )
        deadline = time.monotonic() + 2
        while True:
            event_raw, event = reader.read(timeout_s=max(0.01, deadline - time.monotonic()))
            if event["kind"] == "event":
                break
            assert time.monotonic() < deadline
        assert event["kind"] == "event"
        assert event["payload"]["dispatch_id"] == "follow-live-event"
        assert event["payload"]["data"]["text"] == (
            "payload arrived while the stream stayed alive"
        )
        assert len(event_raw) < messages.STREAM_PIPE_BUF_BYTES
        assert proc.poll() is None

        after_event_raw, after_event = reader.read()
        assert after_event["kind"] == "heartbeat"
        assert len(after_event_raw) < messages.STREAM_PIPE_BUF_BYTES

        # A consumer dispatches only on the structural tag. Batched lines do
        # not require prose parsing or a one-line-equals-one-wake assumption.
        handlers = {kind: object() for kind in ("event", "heartbeat", "frontier")}
        assert handlers[first["kind"]]
        assert handlers[frontier["kind"]]
        assert handlers[event["kind"]]
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=3)


def test_follow_hides_terminal_behind_stuck_until_carrier_repair(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    project, env, lease = isolated
    stream_id = "follow-stuck-prefix"
    addressee = messages.controller_addressee(lease.label, project_root=project)
    source = {"node": "peer", "adapter": "pytest", "transport": "controller"}
    first = messages.post_message(
        dispatch_id=stream_id,
        msg_type="controller-notice",
        payload={"text": "unreadable head"},
        messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
        source=source,
        addressee=addressee,
    )
    terminal = messages.post_message(
        dispatch_id=stream_id,
        msg_type="result",
        payload={"complete": True, "text": "terminal behind stuck row"},
        messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
        source=source,
        addressee=addressee,
    )
    carrier = Path(first["path"])
    original_lines = carrier.read_text(encoding="utf-8").splitlines()
    assert len(original_lines) == 2
    carrier.write_text("{invalid head\n" + original_lines[1] + "\n", encoding="utf-8")

    proc = _spawn_follow(project, env, lease, heartbeat_s=0.1, poll_s=0.02)
    assert proc.stdout is not None
    reader = _JsonLineReader(proc.stdout)
    observed: list[dict[str, object]] = []
    try:
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            _raw, record = reader.read(timeout_s=max(0.01, deadline - time.monotonic()))
            observed.append(record)
            payload = record.get("payload")
            if (
                isinstance(payload, dict)
                and payload.get("reason") == "pending_behind_stuck"
            ):
                break
        else:
            pytest.fail("follow did not report the row behind the stuck head")

        assert any(
            isinstance(record.get("payload"), dict)
            and record["payload"].get("type") == "listener-fault"
            and record["payload"].get("reason") == "carrier-corrupt"
            and record["payload"].get("stream_id") == stream_id
            and record["payload"].get("stream_seq") == 1
            and str(record["payload"].get("stuck_reason") or "").startswith("STUCK:")
            for record in observed
        ), observed
        pending = [
            record["payload"]["pending_behind_stuck"]
            for record in observed
            if isinstance(record.get("payload"), dict)
            and record["payload"].get("reason") == "pending_behind_stuck"
        ]
        assert pending == [
            {
                "id": terminal["envelope"]["id"],
                "stream_id": stream_id,
                "stream_seq": 2,
                "type": "result",
            }
        ]

        def terminal_was_emitted(records: list[dict[str, object]]) -> bool:
            return any(
                isinstance(record.get("payload"), dict)
                and record["payload"].get("dispatch_id") == stream_id
                and record["payload"].get("stream_seq") == 2
                and record["payload"].get("type") == "result"
                for record in records
            )

        until = time.monotonic() + 0.15
        while time.monotonic() < until:
            if b"\n" in reader.buffer:
                _raw, record = reader.read(timeout_s=0.01)
                observed.append(record)
                continue
            readable, _, _ = select.select(
                [proc.stdout.fileno()], [], [], max(0.0, until - time.monotonic())
            )
            if not readable:
                break
            _raw, record = reader.read(timeout_s=0.1)
            observed.append(record)
        assert not terminal_was_emitted(observed), observed

        carrier.write_text("\n".join(original_lines) + "\n", encoding="utf-8")
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and not terminal_was_emitted(observed):
            _raw, record = reader.read(timeout_s=max(0.01, deadline - time.monotonic()))
            observed.append(record)
        assert terminal_was_emitted(observed), observed
        assert sum(terminal_was_emitted([record]) for record in observed) == 1
        assert proc.poll() is None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_follow_does_not_replay_advanced_streams_across_polls_and_restart(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    """Guard the reported stale-snapshot hypothesis, which fresh peeks refute."""
    project, env, lease = isolated
    authority = journal.Journal(project)
    streams = {f"follow-worker-{index}" for index in range(10)}
    for restart in range(2):
        proc = _spawn_follow(project, env, lease, heartbeat_s=0.1)
        assert proc.stdout is not None
        reader = _JsonLineReader(proc.stdout)
        try:
            for batch in range(3):
                seq = restart * 3 + batch + 1
                for stream in sorted(streams):
                    messages.post_message(
                        dispatch_id=stream,
                        msg_type="controller-notice",
                        payload={"text": f"batch {seq}"},
                        messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
                        source={"node": "peer", "adapter": "pytest", "transport": "controller"},
                        addressee=messages.controller_addressee(lease.label, project_root=project),
                    )
                peek = authority.cursor_peek(lease.label, nonce=lease.nonce)
                remaining = {(stream, seq) for stream in streams}
                deadline = time.monotonic() + 10
                while remaining:
                    _raw, record = reader.read(timeout_s=max(0.01, deadline - time.monotonic()))
                    if record["kind"] != "event":
                        continue
                    payload = record["payload"]
                    identity = (payload["stream_id"], payload["stream_seq"])
                    assert identity[0] in streams and identity[1] == seq, (
                        f"replayed acknowledged envelope: {identity}"
                    )
                    # A delivery from any cursor version counts; the final
                    # journal snapshot below still owns the acknowledgement CAS.
                    remaining.discard(identity)
                advanced = authority.advance_cursor(
                    lease.label,
                    nonce=lease.nonce,
                    expected_cursor_version=peek.cursor_version,
                    advances={stream: seq for stream in streams},
                    expected_stream_snapshots=peek.stream_snapshots,
                    actor="follow-replay-test",
                )
                assert advanced.committed
                assert authority.cursor_peek(lease.label, nonce=lease.nonce).items == ()
                # Cross an idle poll before the next batch or process restart.
                while True:
                    _raw, record = reader.read()
                    assert record["kind"] != "event", f"replayed after advance: {record}"
                    if record["kind"] == "heartbeat":
                        break
                assert proc.poll() is None
        finally:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=3)


@pytest.mark.parametrize("event_type", ["controller-notice", "blocked", "user_need", "user_confirm"])
def test_follow_new_arrival_does_not_replay_delivered_unread_mail(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
) -> None:
    project, env, lease = isolated

    def post(stream: str) -> None:
        kind = event_type if stream == "first-unread" else "controller-notice"
        messages.post_message(
            dispatch_id=stream, msg_type=kind,
            payload={"text": "remain unread", "project_root": str(project)},
            messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
            source={"node": "peer", "adapter": "pytest", "transport": "controller"},
            addressee=(messages.controller_addressee(lease.label, project_root=project)
                       if kind == "controller-notice" else None),
        )

    post("first-unread")
    delivered = []

    def write(record, **_kwargs):
        if record["kind"] != "event":
            return True
        stream = record["payload"]["stream_id"]
        delivered.append(stream)
        if len(delivered) == 1:
            post("new-arrival")
        return stream != "new-arrival"

    _pin_listener_resolution(monkeypatch, lease)
    monkeypatch.setattr(messages, "_follow_stdout_refusal", lambda _stream: None)
    monkeypatch.setattr(messages, "_write_follow_record", write)
    monkeypatch.setattr(messages, "_silence_broken_stdout", lambda _stream: None)
    assert messages.main(_follow_argv(project, lease)) == 0
    assert delivered == ["first-unread", "new-arrival"]
    assert len(journal.Journal(project).cursor_peek(lease.label, nonce=lease.nonce).items) == 2


def test_live_follow_redelivers_after_explicit_rewind_only(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    project, env, lease = isolated
    for stream in ("rewind-stream", "rewind-stream", "unrelated-stream"):
        messages.post_message(
            dispatch_id=stream, msg_type="controller-notice",
            payload={"text": "replay only on rewind"},
            messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
            source={"node": "peer", "adapter": "pytest", "transport": "controller"},
            addressee=messages.controller_addressee(lease.label, project_root=project),
        )
    proc = _spawn_follow(project, env, lease, heartbeat_s=0.12)
    assert proc.stdout is not None
    reader = _JsonLineReader(proc.stdout)
    try:
        observed = []

        def three_beats() -> None:
            beats = 0
            while beats < 3:
                record = reader.read()[1]
                if record["kind"] == "event":
                    observed.append(record["payload"]["stream_id"])
                beats += record["kind"] == "heartbeat"

        three_beats()
        assert sorted(observed) == ["rewind-stream", "rewind-stream", "unrelated-stream"], (
            "continuously unread mail replayed"
        )
        authority = journal.Journal(project)
        for _ in range(2):
            assert authority.set_cursor(lease.label, positions={"rewind-stream": 1}, actor="test").committed
            three_beats()
            assert len(observed) == 3, "forward/same cursor writes replayed unread mail"
        # Both writes can occur between polls; observing the forward position
        # in an intermediate heartbeat is not a prerequisite for a rewind.
        assert authority.set_cursor(lease.label, positions={"rewind-stream": 2}, actor="test").committed
        assert authority.set_cursor(lease.label, positions={"rewind-stream": 0}, actor="test").committed
        assert len(authority.cursor_peek(lease.label, nonce=lease.nonce).items) == 3
        three_beats()
        assert proc.poll() is None, "the same follow owner must deliver the replay"
        assert observed.count("rewind-stream") == 4, "explicit rewind was suppressed"
        assert observed.count("unrelated-stream") == 1, "rewind resurrected unrelated mail"
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=3)


@pytest.mark.parametrize(
    "event_type", ["result", "user_confirm", "user_need", "blocked"]
)
def test_follow_hides_terminal_behind_unprojected_head_until_projection(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    event_type: str,
) -> None:
    project, env, lease = isolated
    stream_id = "follow-unprojected-prefix"
    addressee = messages.controller_addressee(lease.label, project_root=project)
    source = {"node": "peer", "adapter": "pytest", "transport": "controller"}
    first = messages.post_message(
        dispatch_id=stream_id,
        msg_type=event_type,
        payload={"complete": event_type == "result", "text": "unprojected head"},
        messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
        source=source,
        addressee=addressee,
    )
    terminal = messages.post_message(
        dispatch_id=stream_id,
        msg_type="result",
        payload={"complete": True, "text": "terminal behind unprojected row"},
        messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
        source=source,
        addressee=addressee,
    )
    authority = journal.open_or_create_journal(project)
    projected_at = authority.read_all(
        "SELECT projected_at FROM delivery_events WHERE event_uuid = ?",
        (first["envelope"]["id"],),
    )[0]["projected_at"]
    connection = authority._connect()
    try:
        connection.execute(
            "UPDATE delivery_events SET projected_at = NULL WHERE event_uuid = ?",
            (first["envelope"]["id"],),
        )
    finally:
        connection.close()

    proc = _spawn_follow(project, env, lease, heartbeat_s=0.1, poll_s=0.02)
    assert proc.stdout is not None
    reader = _JsonLineReader(proc.stdout)
    observed: list[dict[str, object]] = []
    try:
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and not any(
            record.get("kind") == "heartbeat" for record in observed
        ):
            _raw, record = reader.read(timeout_s=max(0.01, deadline - time.monotonic()))
            observed.append(record)
        else:
            if not any(record.get("kind") == "heartbeat" for record in observed):
                pytest.fail("follow did not reach the pending-projection heartbeat")
        assert not any(
            isinstance(record.get("payload"), dict)
            and record["payload"].get("dispatch_id") == stream_id
            and record["payload"].get("stream_seq") == 2
            and record["payload"].get("type") == "result"
            for record in observed
        ), observed
        assert any(
            isinstance(record.get("payload"), dict)
            and record["payload"].get("type") == "pending-projection"
            and record["payload"].get("reason") == "pending-projection"
            and record["payload"].get("pending_projection") == {
                "stream_id": stream_id,
                "stream_seq": 1,
            }
            for record in observed
        ), observed
        assert any(record.get("kind") == "heartbeat" for record in observed), observed
        assert any(
            isinstance(record.get("payload"), dict)
            and record["payload"].get("reason") == "pending_behind_stuck"
            for record in observed
        ), observed
        assert not any(
            isinstance(record.get("payload"), dict)
            and record["payload"].get("type") == "listener-fault"
            for record in observed
        ), observed

        if event_type == "result":
            repaired_at = len(observed)
            connection = authority._connect()
            try:
                connection.execute(
                    "UPDATE delivery_events SET projected_at = ? WHERE event_uuid = ?",
                    (projected_at, first["envelope"]["id"]),
                )
            finally:
                connection.close()
            emitted: list[tuple[int, str]] = []
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and len(emitted) < 2:
                _raw, record = reader.read(timeout_s=max(0.01, deadline - time.monotonic()))
                observed.append(record)
                payload = record.get("payload")
                if (
                    isinstance(payload, dict)
                    and payload.get("dispatch_id") == stream_id
                    and payload.get("stream_seq") in {1, 2}
                    and payload.get("type") in {"controller-notice", "result"}
                ):
                    emitted.append((int(payload["stream_seq"]), str(payload["type"])))
            assert emitted == [(1, "result"), (2, "result")], observed
            assert not any(
                isinstance(record.get("payload"), dict)
                and record["payload"].get("type") == "pending-projection"
                for record in observed[repaired_at:]
            ), observed[repaired_at:]
            assert proc.poll() is None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_follow_rewind_between_evidence_read_and_peek_releases_ring(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, env, lease = isolated
    messages.post_message(
        dispatch_id="rewind-race", msg_type="controller-notice",
        payload={"text": "rewind between polls"},
        messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
        source={"node": "peer", "adapter": "pytest", "transport": "controller"},
        addressee=messages.controller_addressee(lease.label, project_root=project),
    )
    authority = journal.Journal(project)
    original = journal.Journal._cursor_rewinds
    original_gate_changed = messages._JournalChangeGate.changed
    observed = []
    rewound = False
    forced_scan = False
    beats = 0

    def force_scan_after_first_event(gate, data_version, **kwargs):
        nonlocal forced_scan
        changed = original_gate_changed(gate, data_version, **kwargs)
        if observed and not forced_scan:
            forced_scan = True
            return True
        return changed

    def rewind_after_read(self, label):
        nonlocal rewound
        evidence = original(self, label)
        if observed and not rewound:
            rewound = True
            assert authority.set_cursor(label, positions={"rewind-race": 1}, actor="test").committed
            assert authority.set_cursor(label, positions={"rewind-race": 0}, actor="test").committed
        return evidence

    def write(record, **_kwargs):
        nonlocal beats
        if record["kind"] == "event":
            observed.append(record["payload"]["stream_id"])
        if record["kind"] == "heartbeat":
            beats += 1
        return beats < 4 and len(observed) < 2

    _pin_listener_resolution(monkeypatch, lease)
    monkeypatch.setattr(journal.Journal, "_cursor_rewinds", rewind_after_read)
    monkeypatch.setattr(messages._JournalChangeGate, "changed", force_scan_after_first_event)
    monkeypatch.setattr(messages, "_follow_stdout_refusal", lambda _stream: None)
    monkeypatch.setattr(messages, "_write_follow_record", write)
    monkeypatch.setattr(messages, "_silence_broken_stdout", lambda _stream: None)
    assert messages.main(_follow_argv(project, lease)) == 0
    assert observed == ["rewind-race", "rewind-race"], "new-version ring blocked the rewind replay"


@pytest.mark.parametrize("event_type", ["blocked", "user_need", "user_confirm"])
def test_follow_batch_escalation_collision_then_exact_repeat(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity], event_type: str,
) -> None:
    project, _env, lease = isolated
    row = {"stream_id": "collision", "stream_seq": 1}
    routine = {"type": "controller-notice", "payload": {"text": "routine"}}
    escalation = {"type": event_type, "payload": {"text": "ruling required"}}
    emitted = []
    assert messages._emit_claimed_follow_events(
        project, authority=journal.Journal(project), controller_label=lease.label,
        cursor_version=77, visible=[(row, routine), (row, escalation), (row, escalation)],
        delivered=set(), emit=lambda record: emitted.append(record["payload"]["type"]) or True,
    ) == (True, True, True)
    assert emitted == ["controller-notice", event_type]


def test_follow_rewind_invalidates_acknowledged_arm_watermark(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, env, lease = isolated
    messages.post_message(
        dispatch_id="acknowledged-stream", msg_type="controller-notice",
        payload={"text": "replay past the arm watermark"},
        messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
        source={"node": "peer", "adapter": "pytest", "transport": "controller"},
        addressee=messages.controller_addressee(lease.label, project_root=project),
    )
    authority = journal.Journal(project)
    assert authority.set_cursor(lease.label, positions={"acknowledged-stream": 1}, actor="test").committed
    monkeypatch.setattr(wake, "recover_pending_report_state", lambda *_args, **_kwargs:
                        SimpleNamespace(phase="acknowledged", positions={"acknowledged-stream": 1}))
    observed = []
    beats = 0

    def write(record, **_kwargs):
        nonlocal beats
        if record["kind"] == "event":
            observed.append(record["payload"]["stream_id"])
            return False
        if record["kind"] == "heartbeat":
            beats += 1
            if beats == 1:
                assert authority.set_cursor(lease.label, positions={"acknowledged-stream": 0}, actor="test").committed
        return beats < 4

    _pin_listener_resolution(monkeypatch, lease)
    monkeypatch.setattr(messages, "_follow_stdout_refusal", lambda _stream: None)
    monkeypatch.setattr(messages, "_write_follow_record", write)
    monkeypatch.setattr(messages, "_silence_broken_stdout", lambda _stream: None)
    assert messages.main(_follow_argv(project, lease)) == 0
    assert observed == ["acknowledged-stream"]


@pytest.mark.parametrize("event_type", ["blocked", "user_need", "user_confirm", "controller-notice"])
def test_real_follow_backlog_delivers_40_unique_envelopes_once(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity], event_type: str,
) -> None:
    from test_supervised_wake import FakeHost, PlannedExit, _items, _records, _run

    project, env, lease = isolated

    def post(stream, kind):
        messages.post_message(
            dispatch_id=stream, msg_type=kind,
            payload={"text": "unread backlog", "project_root": str(project)},
            messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
            source={"node": "peer", "adapter": "pytest", "transport": "controller"},
            addressee=(messages.controller_addressee(lease.label, project_root=project)
                       if kind == "controller-notice" else None),
        )

    for index in range(20):
        post(f"backlog-{index:02d}", event_type)
    proc = _spawn_follow(project, env, lease, heartbeat_s=0.12)
    assert proc.stdout is not None
    reader = _JsonLineReader(proc.stdout)
    lines = []

    def read_through(stream):
        while True:
            raw, record = reader.read()
            if record["kind"] == "event":
                lines.append(raw.decode().strip())
                if record["payload"].get("stream_id") == stream:
                    return

    try:
        read_through("backlog-19")
        for index in range(20):
            stream = f"arrival-{index:02d}"
            post(stream, "controller-notice")
            read_through(stream)
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=3)
    host = FakeHost(scripts={"stream": [PlannedExit(
        80.0, 0, armed=True, stdout_lines=[(i * 0.001, line) for i, line in enumerate(lines)],
    )]}, stop_after_waits=len(lines) + 4)
    _run(host, _items("stream"))
    forwarded = [r for r in _records(host) if r.get("kind") == "event"]
    assert len(lines) == 40, "follow replayed unread envelopes on cursor-version changes"
    assert len(forwarded) == 40
    assert len({(r["payload"]["stream_id"], r["payload"]["stream_seq"]) for r in forwarded}) == 40
    assert journal.Journal(project).cursor_status(lease.label)["positions"] == {}


def test_follow_recovers_corrupt_pending_report_and_stays_armed(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    project, env, lease = isolated
    messages.post_message(
        dispatch_id="follow-corrupt-state",
        msg_type="controller-notice",
        payload={"text": "recover corrupt follow state"},
        messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
        source={"node": "peer", "adapter": "pytest", "transport": "controller"},
        addressee=messages.controller_addressee(lease.label, project_root=project),
    )
    path = wake._pending_report_path(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"schema":"goalflight.pending-report.v3",', encoding="utf-8")

    proc = _spawn_follow(project, env, lease, heartbeat_s=0.2)
    assert proc.stdout is not None
    reader = _JsonLineReader(proc.stdout)
    try:
        _wait_for_monitor_slot(project, lease.label, proc.pid)
        _raw, record = reader.read()
        assert record["kind"] == "event"
        assert record["payload"]["dispatch_id"] == "follow-corrupt-state"
        assert proc.poll() is None
        assert list(path.parent.glob(f".{path.name}.*.corrupt"))
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_follow_emits_unacked_reported_backlog(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    """Follow cannot act on ``reported``, so that phase is not a skip watermark."""
    project, env, lease = isolated
    messages.post_message(
        dispatch_id="follow-unacked-reported",
        msg_type="controller-notice",
        payload={"text": "unread reported flush"},
        messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
        source={"node": "peer", "adapter": "pytest", "transport": "controller"},
        addressee=messages.controller_addressee(lease.label, project_root=project),
    )
    listener_env = {
        **env,
        "GOALFLIGHT_CONTROLLER_LABEL": lease.label,
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE": lease.nonce,
    }
    listener = subprocess.Popen(
        [
            sys.executable,
            str(SCRIPTS / "goalflight_messages.py"),
            "listen",
            "--project-root",
            str(project),
            "--controller-label",
            lease.label,
            "--lease-nonce",
            lease.nonce,
            "--report-pending",
            "--json",
            "--poll-secs",
            "0.01",
        ],
        cwd=project,
        env=listener_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        report_deadline = time.monotonic() + 5
        while time.monotonic() < report_deadline:
            state = wake.pending_report_state(
                project,
                controller_label=lease.label,
                lease_nonce=lease.nonce,
            )
            if state is not None and state.phase == "reported":
                break
            time.sleep(0.005)
        else:
            pytest.fail("listener never reached reported phase")
        listener.kill()
        listener.wait(timeout=5)
        if listener.stdout is not None:
            listener.stdout.close()
    finally:
        if listener.poll() is None:
            listener.kill()
            listener.wait()

    state = wake.pending_report_state(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    )
    assert state is not None
    assert state.phase == "reported"

    proc = _spawn_follow(project, env, lease, heartbeat_s=0.2)
    assert proc.stdout is not None
    reader = _JsonLineReader(proc.stdout)
    try:
        _wait_for_monitor_slot(project, lease.label, proc.pid)
        deadline = time.monotonic() + 2
        event = None
        while time.monotonic() < deadline:
            _raw, record = reader.read(timeout_s=max(0.01, deadline - time.monotonic()))
            if record["kind"] == "event":
                event = record
                break
        assert event is not None, "follow-only monitor never emitted the unacked backlog"
        assert event["payload"]["dispatch_id"] == "follow-unacked-reported"
        assert proc.poll() is None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


@pytest.mark.parametrize("advance_during", ["materialization", "emission"])
def test_follow_drops_replays_advanced_while_batch_is_in_flight(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    advance_during: str,
) -> None:
    """A live follower must discard a batch consumed since its last peek."""
    project, env, lease = isolated
    authority = journal.Journal(project)
    streams = [f"replay-worker-{index:02d}" for index in range(10)]
    for stream in streams:
        messages.post_message(
            dispatch_id=stream,
            msg_type="controller-notice",
            payload={"text": "consume this while follow stays alive"},
            messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
            source={"node": "peer", "adapter": "pytest", "transport": "controller"},
            addressee=messages.controller_addressee(lease.label, project_root=project),
        )

    delivered: list[tuple[str, int]] = []
    replayed: list[tuple[str, int]] = []
    versions: list[int] = []
    drained = False
    fresh_delivered = False
    polls = 0
    original_materialize = messages._envelopes_with_rows

    def advance(stream_id: str) -> None:
        snapshot = authority.cursor_peek(lease.label, nonce=lease.nonce)
        position = max(
            int(row["stream_seq"])
            for row in snapshot.items
            if row["stream_id"] == stream_id
        )
        result = authority.advance_cursor(
            lease.label,
            nonce=lease.nonce,
            expected_cursor_version=snapshot.cursor_version,
            expected_stream_snapshots={stream_id: snapshot.stream_snapshots[stream_id]},
            advances={stream_id: position},
            actor="pytest",
        )
        assert result.committed
        versions.append(authority.cursor_status(lease.label)["cursor_version"])

    def drain_remaining() -> None:
        nonlocal drained
        for stream_id in streams:
            advance(stream_id)
        drained = True
        assert not authority.cursor_peek(lease.label, nonce=lease.nonce).items
        messages.post_message(
            dispatch_id=streams[-1],
            msg_type="controller-notice",
            payload={"text": "new mail after the drain"},
            messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
            source={"node": "peer", "adapter": "pytest", "transport": "controller"},
            addressee=messages.controller_addressee(lease.label, project_root=project),
        )

    def materialize(current_authority, rows, **kwargs):
        nonlocal polls
        polls += 1
        assert polls < 20, "follow never settled after the controller drained"
        visible = original_materialize(current_authority, rows, **kwargs)
        if polls == 1 and advance_during == "materialization":
            # Real carrier reads can outlive a controller advance. The returned
            # rows were pending at peek time, but are acknowledged on return.
            drain_remaining()
        return visible

    def write(record, **_kwargs):
        nonlocal fresh_delivered
        if record["kind"] != "event":
            return True
        payload = record["payload"]
        assert payload["type"] == "controller-notice", record
        identity = (payload["dispatch_id"], payload["stream_seq"])
        positions = authority.cursor_status(lease.label)["positions"]
        if payload["stream_seq"] <= positions.get(payload["stream_id"], 0):
            replayed.append(identity)
            return False
        delivered.append(identity)
        if identity == (streams[-1], 2):
            fresh_delivered = True
            return False
        if len(delivered) == 1 and not drained:
            assert advance_during == "emission"
            drain_remaining()
        return True

    _pin_listener_resolution(monkeypatch, lease)
    monkeypatch.setattr(messages, "_follow_stdout_refusal", lambda _stream: None)
    monkeypatch.setattr(messages, "_envelopes_with_rows", materialize)
    monkeypatch.setattr(messages, "_write_follow_record", write)
    monkeypatch.setattr(messages, "_silence_broken_stdout", lambda _stream: None)
    assert messages.main(_follow_argv(project, lease)) == 0
    assert drained
    assert len(versions) == len(streams)
    assert versions == sorted(set(versions)), "cursor must keep advancing"
    assert not replayed, f"follow re-emitted already-advanced envelopes: {replayed}"
    assert fresh_delivered, "discarding stale rows must not suppress new mail"
    expected = {(streams[-1], 2)}
    if advance_during == "emission":
        expected.add((streams[0], 1))
    assert set(delivered) == expected


@pytest.mark.parametrize("failure", ["busy", "unreadable"])
def test_follow_delivery_cursor_failure_does_not_consume_ring(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    project, env, lease = isolated
    messages.post_message(
        dispatch_id="delivery-cursor-failure",
        msg_type="controller-notice",
        payload={"text": "keep unread mail deliverable after a cursor read failure"},
        messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
        source={"node": "peer", "adapter": "pytest", "transport": "controller"},
        addressee=messages.controller_addressee(lease.label, project_root=project),
    )
    snapshot = journal.Journal(project).cursor_peek(lease.label, nonce=lease.nonce)
    original_status = journal.Journal.cursor_status
    reads = 0
    records = []

    def status(authority, label):
        nonlocal reads
        reads += 1
        if reads == 1:
            assert not wake.claim_ring(
                project, controller_label=label, cursor_version=snapshot.cursor_version
            ), "inject the read failure only after follow owns the ring"
            if failure == "busy":
                raise journal.JournalBusy("delivery cursor temporarily busy")
            return None
        return original_status(authority, label)

    def write(record, **_kwargs):
        records.append(record)
        assert len(records) < 20, "follow never retried mail after releasing the failed ring"
        return record["kind"] != "heartbeat" or reads < 2

    _pin_listener_resolution(monkeypatch, lease)
    monkeypatch.setattr(messages, "_follow_stdout_refusal", lambda _stream: None)
    monkeypatch.setattr(messages, "_silence_broken_stdout", lambda _stream: None)
    monkeypatch.setattr(messages, "_write_follow_record", write)
    monkeypatch.setattr(journal.Journal, "cursor_status", status)
    code = messages.main(_follow_argv(project, lease))
    types = [row["payload"]["type"] for row in records if row["kind"] == "event"]
    if failure == "busy":
        assert code == 0
        assert reads == 2
        assert types.count("controller-notice") == 1
        assert "listener-degraded" in types
        assert "listener-recovered" in types
    else:
        assert code == 2
        assert types == ["listener-fault"], "unknown consumption must not emit mail"
        assert wake.claim_ring(
            project, controller_label=lease.label, cursor_version=snapshot.cursor_version
        ), "unreadability must leave the ring reclaimable by a replacement"
    assert journal.Journal(project).cursor_peek(lease.label, nonce=lease.nonce).items


def test_epipe_exits_and_releases_persistent_monitor_slot(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    project, env, lease = isolated
    proc = _spawn_follow(project, env, lease, heartbeat_s=0.05)
    assert proc.stdout is not None
    reader = _JsonLineReader(proc.stdout)
    reader.read()
    _wait_for_monitor_slot(project, lease.label, proc.pid)

    try:
        proc.stdout.close()
        assert proc.wait(timeout=3) == 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=3)
    assert not wake.live_waiters(
        project,
        controller_label=lease.label,
        kinds={wake.MONITOR_KIND},
    )


def test_epipe_before_first_event_releases_ring_for_replacement(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    project, _env, lease = isolated
    visible = [
        (
            {"stream_id": "peer", "stream_seq": 1},
            {
                "dispatch_id": "redeliver-me",
                "type": "controller-notice",
                "payload": {"text": "the first reader disappeared"},
            },
        )
    ]
    alive, emitted, ring_claimed = messages._emit_claimed_follow_events(
        project,
        authority=journal.Journal(project),
        controller_label=lease.label,
        cursor_version=77,
        visible=visible,
        emit=lambda _record: False,
    )
    assert (alive, emitted, ring_claimed) == (False, False, True)
    assert wake.claim_ring(
        project,
        controller_label=lease.label,
        cursor_version=77,
    ), "the replacement listener must be able to deliver the same cursor"


def test_follow_does_not_retry_ring_already_claimed_for_delivered_rows(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    project, _env, lease = isolated
    visible = [
        (
            {"stream_id": "already-rung", "stream_seq": 1},
            {
                "dispatch_id": "already-rung",
                "type": "controller-notice",
                "payload": {"text": "this row was emitted by the current owner"},
            },
        )
    ]
    delivered: set[tuple[str, int, str]] = set()
    authority = journal.Journal(project)
    assert messages._emit_claimed_follow_events(
        project,
        authority=authority,
        controller_label=lease.label,
        cursor_version=78,
        visible=visible,
        emit=lambda _record: True,
        delivered=delivered,
    ) == (True, True, True)
    assert messages._emit_claimed_follow_events(
        project,
        authority=authority,
        controller_label=lease.label,
        cursor_version=78,
        visible=visible,
        emit=lambda _record: pytest.fail("already delivered row was emitted again"),
        delivered=delivered,
    ) == (True, False, True)


@pytest.mark.parametrize("failure", ["false", "partial", "exception"])
def test_partial_follow_batch_is_replayable_by_replacement_owner(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    failure: str,
) -> None:
    project, _env, lease = isolated
    visible = [({"stream_id": "peer", "stream_seq": i + 1},
                {"dispatch_id": "peer", "type": "controller-notice",
                 "payload": {"text": f"notice {i}"}}) for i in range(10)]
    delivered: set[tuple[str, int, str]] = set()
    output = []

    def emit(record):
        if len(output) == 3:
            if failure == "exception":
                raise BrokenPipeError("reader left mid-batch")
            if failure == "partial":
                output.append(json.dumps(record)[:20])
            return False
        output.append(record)
        return True

    kwargs = dict(project_root=project, authority=journal.Journal(project),
                  controller_label=lease.label, cursor_version=77, visible=visible,
                  delivered=delivered)
    if failure == "exception":
        with pytest.raises(BrokenPipeError):
            messages._emit_claimed_follow_events(**kwargs, emit=emit)
    else:
        assert messages._emit_claimed_follow_events(**kwargs, emit=emit) == (
            False,
            False,
            True,
        )
    assert len([item for item in output if isinstance(item, dict)]) == 3
    assert delivered == {
        ("peer", seq, "") for seq in range(1, 4)
    }
    replacement = []
    replacement_kwargs = {**kwargs, "delivered": set()}
    wake.release_ring_claim(
        project,
        controller_label=lease.label,
        cursor_version=77,
    )
    assert messages._emit_claimed_follow_events(
        **replacement_kwargs,
        emit=lambda record: replacement.append(record) or True,
    ) == (True, True, True)
    assert [r["payload"]["stream_seq"] for r in replacement] == list(range(1, 11))
    assert journal.Journal(project).cursor_status(lease.label)["positions"] == {}


def test_partial_follow_recovery_does_not_reemit_successful_rows(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _env, lease = isolated
    messages.post_message(
        dispatch_id="partial-batch-wal-replacement",
        msg_type="controller-notice",
        payload={"text": "keep the live WAL present for replacement"},
        messages_dir=Path(_env["GOALFLIGHT_MESSAGES_DIR"]),
        source={"node": "peer", "adapter": "pytest", "transport": "controller"},
        addressee=messages.controller_addressee(lease.label, project_root=project),
    )
    visible = [
        (
            {"stream_id": "peer", "stream_seq": seq},
            {
                "dispatch_id": "peer",
                "type": "controller-notice",
                "payload": {"text": f"notice {seq}"},
            },
        )
        for seq in range(1, 4)
    ]
    authority = journal.Journal(project)
    delivered: set[tuple[str, int, str]] = set()
    first_output: list[dict[str, object]] = []
    original_positions = messages._journal_cursor_positions
    position_reads = 0
    wal = Path(f"{journal.resolve_journal_path(project)}-wal")
    wal.touch(exist_ok=True)

    def fail_after_first_emit(authority, controller_label: str):
        nonlocal position_reads
        position_reads += 1
        if position_reads == 2:
            before = wal.stat()
            replacement = wal.with_name(f"{wal.name}.replacement")
            replacement.write_bytes(b"")
            os.replace(replacement, wal)
            after = wal.stat()
            assert (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            raise sqlite3.OperationalError("disk I/O error after WAL replacement")
        return {}

    monkeypatch.setattr(messages, "_journal_cursor_positions", fail_after_first_emit)
    with pytest.raises(sqlite3.OperationalError):
        messages._emit_claimed_follow_events(
            project,
            authority=authority,
            controller_label=lease.label,
            cursor_version=79,
            visible=visible,
            emit=lambda record: first_output.append(record) or True,
            delivered=delivered,
        )
    assert [record["payload"]["stream_seq"] for record in first_output] == [1]
    assert delivered == {("peer", 1, "")}

    monkeypatch.setattr(messages, "_journal_cursor_positions", original_positions)
    resumed_output: list[dict[str, object]] = []
    assert messages._emit_claimed_follow_events(
        project,
        authority=authority,
        controller_label=lease.label,
        cursor_version=79,
        visible=visible,
        emit=lambda record: resumed_output.append(record) or True,
        delivered=delivered,
    ) == (True, True, True)
    assert [record["payload"]["stream_seq"] for record in resumed_output] == [
        2,
        3,
    ]
    assert delivered == {
        ("peer", seq, "") for seq in range(1, 4)
    }


@pytest.mark.parametrize("partial", [False, True])
def test_new_follow_owner_recovers_batch_lost_after_child_pipe_flush(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    partial: bool,
) -> None:
    from test_supervised_wake import FakeHost, PlannedExit, _items, _records, _run

    project, env, lease = isolated
    for i in range(10):
        messages.post_message(
            dispatch_id="pipe-recovery", msg_type="controller-notice",
            payload={"text": f"notice {i}"}, messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
            source={"node": "peer", "adapter": "pytest", "transport": "controller"},
            addressee=messages.controller_addressee(lease.label, project_root=project),
        )
    _pin_listener_resolution(monkeypatch, lease)
    monkeypatch.setattr(messages, "_follow_stdout_refusal", lambda _stream: None)
    monkeypatch.setattr(messages, "_silence_broken_stdout", lambda _stream: None)

    def child_pipe() -> list[str]:
        output = []
        beats = 0

        def write(record, **_kwargs):
            nonlocal beats
            if record["kind"] == "event":
                output.append(json.dumps(record))
            if record["kind"] == "heartbeat":
                beats += 1
            return beats < 4

        monkeypatch.setattr(messages, "_write_follow_record", write)
        assert messages.main(_follow_argv(project, lease)) == 0
        return output

    first_pipe = child_pipe()
    assert len(first_pipe) == 10, "producer must flush the full batch before host failure"

    class FailingHost(FakeHost):
        accepted = 0

        def write_stdout(self, line: str) -> bool:
            if json.loads(line).get("kind") == "event":
                if self.accepted == 3:
                    if partial:
                        self.lines.append(line[:len(line) // 2])
                    return False
                self.accepted += 1
            return super().write_stdout(line)

    def plan(lines):
        return {"backup": [PlannedExit(80.0, 0, armed=True,
            stdout_lines=[(i * .001, line) for i, line in enumerate(lines)])]}

    first = FailingHost(scripts=plan(first_pipe), stop_after_waits=20)
    _run(first, _items("backup"))
    assert first.accepted == 3
    assert journal.Journal(project).cursor_status(lease.label)["positions"] == {}, (
        "the failed delivery did not acknowledge any cursor positions"
    )
    replacement_pipe = child_pipe()
    replacement_source = [json.loads(line) for line in replacement_pipe]
    assert [record["payload"]["stream_seq"] for record in replacement_source] == list(
        range(1, 11)
    ), "follow must expose the full batch while the journal cursor is empty"
    replacement = FakeHost(scripts=plan(replacement_pipe), stop_after_waits=20)
    _run(replacement, _items("backup"))
    events = [r for r in _records(replacement) if r.get("kind") == "event"]
    assert [r["payload"]["stream_seq"] for r in events] == list(range(1, 11))
    assert all(
        record.get("replay") is True and record["payload"].get("replay") is True
        for record in events
    ), f"replacement events were not marked as replays: {events}"
    assert journal.Journal(project).cursor_status(lease.label)["positions"] == {}


class _BackpressuredStream:
    def __init__(self, failures: list[int]) -> None:
        self.failures = list(failures)
        self.text = ""
        self.write_calls = 0
        self.flush_calls = 0

    def write(self, value: str) -> int:
        self.write_calls += 1
        self.text += value
        return len(value)

    def flush(self) -> None:
        self.flush_calls += 1
        if self.failures:
            code = self.failures.pop(0)
            raise OSError(code, os.strerror(code))


def test_eagain_waits_without_exit_or_duplicate_write_and_eintr_retries() -> None:
    record = messages._follow_heartbeat_record(1, 120.0)
    stream = _BackpressuredStream([errno.EAGAIN, errno.EINTR])
    waits: list[float] = []

    assert messages._write_follow_record(
        record,
        stream=stream,
        retry_s=0.25,
        wait_writable=lambda _stream, wait_s: waits.append(wait_s),
    )
    assert stream.write_calls == 1
    assert stream.flush_calls == 3
    assert waits == [0.25]
    assert stream.text.count("\n") == 1


def test_persistent_eagain_is_bounded_without_spinning() -> None:
    record = messages._follow_heartbeat_record(1, 120.0)
    stream = _BackpressuredStream([errno.EAGAIN])
    times = iter((0.0, 61.0))
    waits: list[float] = []

    with pytest.raises(messages.FollowWriteStalled):
        messages._write_follow_record(
            record,
            stream=stream,
            retry_s=0.25,
            stall_s=60.0,
            wait_writable=lambda _stream, wait_s: waits.append(wait_s),
            clock=lambda: next(times),
        )
    assert stream.write_calls == 1
    assert stream.flush_calls == 1
    assert waits == []


def test_backpressure_fault_releases_monitor_before_watchdog_rearm(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _env, lease = isolated
    monkeypatch.setattr(messages, "_follow_stdout_refusal", lambda _stream: None)
    monkeypatch.setattr(
        messages,
        "_write_follow_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            messages.FollowWriteStalled("measured full pipe")
        ),
    )
    result = messages._run_cli(
        [
            "follow",
            "--project-root",
            str(project),
            "--controller-label",
            lease.label,
            "--lease-nonce",
            lease.nonce,
            "--heartbeat-secs",
            "0.01",
            "--poll-secs",
            "0.01",
        ]
    )
    assert result == 2
    assert not wake.live_waiters(
        project,
        controller_label=lease.label,
        kinds={wake.MONITOR_KIND},
    )
    status = wake.monitor_status(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    )
    assert status is not None
    assert status["state"] == "fault"
    assert status["fault"]["reason"] == "stdout-backpressure"


@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    (
        (journal.JournalBusy("measured journal busy"), "journal-unavailable"),
        (journal.JournalIOError("measured present-path IO fault"), "journal-io-failure"),
    ),
)
def test_journal_failure_is_a_waking_stdout_record(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: journal.JournalUnavailable,
    expected_reason: str,
) -> None:
    project, _env, lease = isolated
    monkeypatch.setattr(messages, "_follow_stdout_refusal", lambda _stream: None)
    # Persistent busy must still fail after the shrunken tolerance window; the
    # non-busy JournalIOError case remains immediately fatal.
    monkeypatch.setattr(
        messages, "LISTENER_JOURNAL_TOLERANCE_S", 0.2, raising=False
    )
    monkeypatch.setattr(
        journal.Journal,
        "cursor_peek",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )
    result = messages._run_cli(
        [
            "follow",
            "--project-root",
            str(project),
            "--controller-label",
            lease.label,
            "--lease-nonce",
            lease.nonce,
            "--heartbeat-secs",
            "0.01",
            "--poll-secs",
            "0.01",
        ]
    )
    assert result == 2
    records = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    assert records[-1]["kind"] == "event"
    assert records[-1]["payload"]["type"] == "listener-fault"
    assert records[-1]["payload"]["reason"] == expected_reason
    assert not wake.live_waiters(
        project,
        controller_label=lease.label,
        kinds={wake.MONITOR_KIND},
    )


@pytest.mark.parametrize(
    "failure",
    [
        journal.JournalBusy("materialization busy"),
        journal.JournalUpgradeRequired("materialization upgrade required"),
    ],
)
def test_materialization_propagates_journal_failures(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    failure: RuntimeError,
) -> None:
    project, _env, lease = isolated
    authority = journal.Journal(project)
    row = {
        "carrier_path": str(project / "carrier.jsonl"),
        "stream_id": "materialization-failure",
        "stream_seq": 1,
    }

    def fail_materialization(*_args: object, **_kwargs: object) -> dict:
        raise failure

    monkeypatch.setattr(messages, "_listener_envelope", fail_materialization)
    with pytest.raises(type(failure), match=str(failure)):
        messages._envelopes_with_rows(
            authority,
            [row],
            controller_label=lease.label,
            carrier_errors=[],
        )


def test_cursor_boundary_excludes_unshown_rows() -> None:
    first = {"stream_id": "stream", "stream_seq": 1}
    second = {"stream_id": "stream", "stream_seq": 2}
    other = {"stream_id": "other", "stream_seq": 1}
    shown = [(second, {"id": "second"}), (other, {"id": "other"})]

    assert messages._cursor_positions_for_shown_rows(
        [first, second, other], shown
    ) == {"other": 1}


def test_listener_survives_present_journal_open_failure_and_times_out(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, _env, lease = isolated
    real_connect = journal._sqlite_connect
    failed_opens = 0

    def fail_first_rw_open(database: object, *args: object, **kwargs: object):
        nonlocal failed_opens
        if "?mode=rw" in str(database) and failed_opens == 0:
            failed_opens += 1
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(journal, "_sqlite_connect", fail_first_rw_open)
    result = messages._run_cli(
        [
            "listen",
            "--project-root",
            str(project),
            "--controller-label",
            lease.label,
            "--lease-nonce",
            lease.nonce,
            "--listener-slots",
            "1",
            "--json",
            "--poll-secs",
            "0.01",
            "--timeout-s",
            "0.05",
        ]
    )

    records = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    assert failed_opens == 1
    assert result == 1
    assert records[-1]["reason"] == "timeout"
    assert all(record.get("reason") != "journal-unavailable" for record in records)


def test_watchdog_dead_audit_reason_is_registered() -> None:
    assert "watchdog-dead" in journal.LISTENER_EXIT_REASONS
    assert "journal-io-failure" in journal.LISTENER_EXIT_REASONS


def test_every_record_is_structural_and_below_pipe_buf_with_long_frontier(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(wake, "_process_listing", lambda **_kwargs: [])
    frontier = messages._follow_frontier_record(
        {
            "id": "t-very-long-frontier",
            "title": "sample project 🚀 " * 10_000,
            "derived_status": "pending",
        }
    )
    records = [
        messages._follow_heartbeat_record(1, messages.FOLLOW_HEARTBEAT_SECS),
        frontier,
        messages._follow_event_record(
            {"stream_id": "stream", "stream_seq": 7},
            {
                "dispatch_id": "long-event",
                "type": "controller-notice",
                "payload": {"text": "mail " * 10_000},
            },
        ),
        messages._follow_fault_record("journal-unavailable", "x" * 10_000),
        messages._follow_dead_record(
            {"state": "stale", "age_s": 999, "dead_after_s": 360},
            project_root=tmp_path,
            controller_label="structural-test",
            lease_nonce="structural-test-nonce",
            rearm_command="python3 goalflight_messages.py follow " + "x" * 10_000,
        ),
        messages._watchdog_dead_record(
            {
                "live_waiters": 0,
                "target_waiters": 3,
                "missing_components": ["stream", "backup", "watchdog"],
            },
            project_root=tmp_path,
            controller_label="structural-test",
            lease_nonce="structural-test-nonce",
            rearm_command=(
                "python3 goalflight_messages.py listen --watch-follow "
                + "x" * 10_000
            ),
        ),
    ]
    assert {record["kind"] for record in records} == {
        "event",
        "heartbeat",
        "frontier",
    }
    for record in records:
        raw = messages._follow_line_bytes(record)
        assert len(raw) < messages.STREAM_PIPE_BUF_BYTES
        assert json.loads(raw)["kind"] in {"event", "heartbeat", "frontier"}
    assert frontier["payload"]["state"] == "ready"
    assert frontier["payload"]["advisory"] == "information-only"
    assert frontier["payload"]["truncated"] is True


def test_frontier_disabled_mirror_advises_task_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOALFLIGHT_DASHBOARD_EXPORT_ENABLED", "0")
    # No projection attributes: disabled mirrors must return before accessing files.
    frontier = messages._follow_frontier_snapshot(SimpleNamespace())
    assert frontier["payload"]["state"] == "unavailable"
    assert frontier["payload"]["detail"] == (
        "next-task hint unavailable; run goalflight_task.py next"
    )


def test_frontier_reads_only_materialized_projection_and_marks_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_DASHBOARD_EXPORT_ENABLED", "1")
    projection = tmp_path / "tasks-data.js"
    projection.write_text(
        "// generated\nwindow.GF_ITEMS = "
        + json.dumps(
            [
                {
                    "id": "t-projected",
                    "kind": "task",
                    "title": "projected frontier",
                    "derived_status": "pending",
                    "lane": "default",
                }
            ]
        )
        + ";\n",
        encoding="utf-8",
    )
    canonical = tmp_path / "tasks.jsonl"
    store = SimpleNamespace(
        data_js_path=projection,
        export_dashboard_dir=tmp_path,
        tasks_path=canonical,
    )

    ready = messages._follow_frontier_snapshot(store)
    assert ready["payload"]["id"] == "t-projected"
    assert ready["payload"]["state"] == "projected"
    assert ready["payload"]["source"] == "materialized-projection"
    assert isinstance(ready["payload"]["age_s"], float)

    old = time.time() - messages.FOLLOW_FRONTIER_STALE_SECS - 10
    os.utime(projection, (old, old))
    aged = messages._follow_frontier_snapshot(store)
    assert aged["payload"]["state"] == "stale"
    assert aged["payload"]["stale_reason"] == "projection-age"
    assert aged["payload"]["age_s"] >= messages.FOLLOW_FRONTIER_STALE_SECS

    canonical.write_text("{}\n", encoding="utf-8")
    projection_ns = projection.stat().st_mtime_ns
    os.utime(canonical, ns=(projection_ns + 1_000_000, projection_ns + 1_000_000))
    stale = messages._follow_frontier_snapshot(store)
    assert stale["payload"]["state"] == "stale"
    assert stale["payload"]["advisory"] == "information-only"

    projection.write_text(
        "// generated\nwindow.GF_ITEMS = "
        + json.dumps(
            [
                {
                    "id": "t-working",
                    "kind": "task",
                    "title": "worker remains in flight",
                    "derived_status": "working",
                    "lane": "default",
                    "dispatches": [
                        {
                            "dispatch_id": "working-child",
                            "state": "working",
                            "ts": "2026-08-26T00:00:00+00:00",
                        }
                    ],
                }
            ]
        )
        + ";\n",
        encoding="utf-8",
    )
    canonical.unlink()
    working_child = messages._follow_frontier_snapshot(store)
    assert working_child["payload"]["state"] == "empty"
    assert "id" not in working_child["payload"]
    working_forwarded = messages._supervisor_frontier_snapshot(store)
    assert working_forwarded["payload"]["id"] == "t-working"
    assert working_forwarded["payload"]["state"] == "working"
    assert working_forwarded["payload"]["title"] == "worker remains in flight"

    projection.write_text(
        "// generated\nwindow.GF_ITEMS = "
        + json.dumps(
            [
                {
                    "id": "t-complete",
                    "kind": "task",
                    "title": "historical dispatch is terminal",
                    "derived_status": "done-reviewed",
                    "lane": "default",
                    "dispatches": [
                        {
                            "dispatch_id": "completed-child",
                            "state": "completed",
                            "ts": "2026-08-26T00:00:00+00:00",
                        }
                    ],
                }
            ]
        )
        + ";\n",
        encoding="utf-8",
    )
    complete = messages._follow_frontier_snapshot(store)
    assert complete["payload"]["state"] == "empty"

    projection.write_text(
        "// generated\nwindow.GF_ITEMS = "
        + json.dumps(
            [
                {
                    "id": "q-decision",
                    "kind": "decision",
                    "title": "owner choice remains pending",
                    "derived_status": "decision",
                    "lane": "default",
                }
            ]
        )
        + ";\n",
        encoding="utf-8",
    )
    decision_child = messages._follow_frontier_snapshot(store)
    assert decision_child["payload"]["state"] == "empty"
    assert "id" not in decision_child["payload"]
    decision_forwarded = messages._supervisor_frontier_snapshot(store)
    assert decision_forwarded["payload"]["id"] == "q-decision"
    assert decision_forwarded["payload"]["state"] == "decision"

    projection.write_text(
        "// generated\nwindow.GF_ITEMS = [];\n",
        encoding="utf-8",
    )
    empty = messages._follow_frontier_snapshot(store)
    assert empty["payload"]["state"] == "empty"


def test_persistent_monitor_suppresses_pool_shortage_but_keeps_backup_depth(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _env, lease = isolated
    # Portable-only knobs must not poison persistent coverage, even when their
    # values would be invalid for the pool path. The follow command warns.
    monkeypatch.setenv("GOALFLIGHT_LISTENER_SLOTS", "not-a-pool-size")
    monkeypatch.setenv("GOALFLIGHT_LISTENER_LOW_WATER", "not-a-low-water")
    wake.activate_monitor_state(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        heartbeat_s=120,
        dead_after_s=messages.FOLLOW_DEAD_AFTER_SECS,
    )
    with wake.register_waiter(
        project,
        controller_label=lease.label,
        kind=wake.MONITOR_KIND,
        generation_key=lease.nonce,
    ):
        stream_only = wake.coverage_status(
            project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
        )
        backup_target = wake.persistent_backup_slot_count()
        wake_target = wake.persistent_wake_target()
        assert stream_only["wake_mode"] == "persistent"
        assert stream_only["live_waiters"] == 1
        assert stream_only["target_waiters"] == wake_target
        assert stream_only["missing_components"] == ["backup", "watchdog"]
        assert stream_only["portable_live_waiters"] == 0
        assert stream_only["portable_target_waiters"] == backup_target
        claim_depth = sessions._listener_depth_after_claim(
            project,
            lease.label,
            lease.nonce,
        )
        assert claim_depth is not None
        assert claim_depth["live"] == 1
        assert claim_depth["target"] == wake_target
        assert claim_depth["missing"] == wake_target - 1
        assert claim_depth["missing_components"] == (
            ["backup"] * backup_target + ["watchdog"]
        )
        assert all(
            "--watch-follow" not in command
            for command in claim_depth["commands"][:-1]
        )
        assert "--watch-follow" in claim_depth["commands"][-1]
        claim_hint_plan = {**claim_depth, "work_in_flight": True}
        assert "own tracked background task" in wake.coverage_rearm_hint(
            claim_hint_plan
        )

        with ExitStack() as pool:
            pool.enter_context(
                wake.register_listener_waiter(
                    project,
                    controller_label=lease.label,
                    generation_key=lease.nonce,
                    slots=backup_target,
                )
            )
            with_backup = wake.coverage_status(
                project,
                controller_label=lease.label,
                lease_nonce=lease.nonce,
            )
            assert with_backup["live_waiters"] == 2
            assert with_backup["target_waiters"] == wake_target
            assert with_backup["backup"]["state"] == "degraded"
            assert with_backup["missing_components"] == ["backup", "watchdog"]
            assert with_backup["portable_live_waiters"] == 1
            with wake.register_watchdog_waiter(
                project,
                controller_label=lease.label,
                generation_key=lease.nonce,
            ):
                partial = wake.coverage_status(
                    project,
                    controller_label=lease.label,
                    lease_nonce=lease.nonce,
                )
                assert partial["covered"] is True
                assert partial["backup"]["state"] == "degraded"
                assert partial["live_waiters"] == 3
                assert partial["target_waiters"] == wake_target
                assert partial["missing_components"] == ["backup"]
                for _ in range(backup_target - 1):
                    pool.enter_context(
                        wake.register_listener_waiter(
                            project,
                            controller_label=lease.label,
                            generation_key=lease.nonce,
                            slots=backup_target,
                        )
                    )
                complete = wake.coverage_status(
                    project,
                    controller_label=lease.label,
                    lease_nonce=lease.nonce,
                )
                assert complete["covered"] is True
                assert complete["backup"]["state"] == "live"
                assert complete["live_waiters"] == complete["target_waiters"] == wake_target
                assert complete["missing_components"] == []

    with wake.register_listener_waiter(
        project,
        controller_label=lease.label,
        generation_key=lease.nonce,
        slots=1,
    ):
        stream_gone = wake.coverage_status(
            project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
        )
        assert stream_gone["wake_mode"] == "persistent"
        assert stream_gone["live_waiters"] == 1
        assert stream_gone["target_waiters"] == wake.persistent_wake_target()
        assert stream_gone["backup"]["state"] == "degraded"
        assert stream_gone["missing_components"] == ["stream", "backup", "watchdog"]
        stream_plan = wake.coverage_rearm_plan(
            stream_gone,
            project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
            work_in_flight=True,
        )
        assert "host persistent stdout monitor" in wake.coverage_rearm_hint(
            stream_plan
        )


def test_coverage_excludes_waiters_from_previous_lease_generation(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    project, _env, lease = isolated
    replacement_nonce = "replacement-generation-nonce"
    wake.activate_monitor_state(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        heartbeat_s=120,
        dead_after_s=messages.FOLLOW_DEAD_AFTER_SECS,
    )
    with wake.register_waiter(
        project,
        controller_label=lease.label,
        kind=wake.MONITOR_KIND,
        generation_key=lease.nonce,
    ):
        with wake.register_listener_waiter(
            project,
            controller_label=lease.label,
            generation_key=lease.nonce,
            slots=1,
        ):
            with wake.register_watchdog_waiter(
                project,
                controller_label=lease.label,
                generation_key=lease.nonce,
            ):
                replacement = wake.coverage_status(
                    project,
                    controller_label=lease.label,
                    lease_nonce=replacement_nonce,
                )

    assert replacement["covered"] is False
    assert replacement["wake_mode"] == "persistent"
    assert replacement["live_waiters"] == 0
    assert replacement["target_waiters"] == wake.persistent_wake_target()
    assert replacement["missing_components"] == ["stream", "backup", "watchdog"]
    assert replacement["waiters"] == []


@pytest.mark.parametrize("damage", ("corrupt", "missing"))
def test_persistent_coverage_fails_closed_when_monitor_state_is_unavailable(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    damage: str,
) -> None:
    project, _env, lease = isolated
    wake.activate_monitor_state(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        heartbeat_s=120,
        dead_after_s=messages.FOLLOW_DEAD_AFTER_SECS,
    )
    with wake.register_waiter(
        project,
        controller_label=lease.label,
        kind=wake.MONITOR_KIND,
        generation_key=lease.nonce,
    ):
        pass
    state_path = wake._monitor_state_path(project, controller_label=lease.label)
    if damage == "corrupt":
        state_path.write_text("{not-json\n", encoding="utf-8")
    else:
        state_path.unlink()

    with wake.register_listener_waiter(
        project,
        controller_label=lease.label,
        generation_key=lease.nonce,
        slots=1,
    ):
        status = wake.coverage_status(
            project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
        )

    assert status["covered"] is False
    assert status["wake_mode"] == "persistent"
    assert status["reason"] == "persistent-monitor-state-unavailable"
    assert status["live_waiters"] == 1
    assert status["backup"]["state"] == "degraded"
    assert status["missing_components"] == ["stream", "backup", "watchdog"]


def test_watchdog_generation_lock_is_independent_of_listener_slot(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    project, _env, lease = isolated
    with wake.register_listener_waiter(
        project,
        controller_label=lease.label,
        generation_key=lease.nonce,
        slots=1,
    ):
        with wake.register_watchdog_waiter(
            project,
            controller_label=lease.label,
            generation_key=lease.nonce,
        ):
            kinds = {
                row.kind
                for row in wake.live_waiters(
                    project,
                    controller_label=lease.label,
                    kinds={"listener", "watchdog"},
                )
                or []
            }
            assert kinds == {"listener", "watchdog"}
            extra = wake.register_listener_waiter(
                project,
                controller_label=lease.label,
                generation_key=lease.nonce,
                slots=2,
            )
            try:
                assert extra.slot_index == 1
            finally:
                extra.close()
            with pytest.raises(BlockingIOError):
                wake.register_watchdog_waiter(
                    project,
                    controller_label=lease.label,
                    generation_key=lease.nonce,
                )


def test_fleet_console_uses_shared_persistent_coverage_predicate(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    project, _env, lease = isolated
    authority = journal.Journal(project)
    wake.activate_monitor_state(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        heartbeat_s=120,
        dead_after_s=messages.FOLLOW_DEAD_AFTER_SECS,
    )
    with wake.register_waiter(
        project,
        controller_label=lease.label,
        kind=wake.MONITOR_KIND,
        generation_key=lease.nonce,
    ):
        contexts = fleet._controller_contexts_by_session(
            project,
            [{"controller_session_id": lease.nonce}],
            include_all=True,
            authority=authority,
            open_if_missing=False,
        )
    context = contexts[lease.nonce]
    assert context["wake_mode"] == "persistent"
    coverage = context["wake_coverage"]
    assert isinstance(coverage, dict)
    assert coverage["live_waiters"] == 1
    assert coverage["target_waiters"] == wake.persistent_wake_target()


def test_follow_backup_and_watchdog_coexist_and_sigkill_wakes(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    project, env, lease = isolated
    err_handles: list = []

    def _stderr(name: str):
        handle = (project.parent / f"{name}.stderr").open("w", encoding="utf-8")
        err_handles.append(handle)
        return handle

    follow = subprocess.Popen(
        _follow_command(project, lease, heartbeat_s=2.0, poll_s=0.25),
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=_stderr("follow"),
    )
    assert follow.stdout is not None
    follow_reader = _JsonLineReader(follow.stdout)
    assert follow_reader.read()[1]["kind"] == "heartbeat"
    assert follow_reader.read()[1]["kind"] == "frontier"

    # Watchdog --timeout-s is process lifetime, not the assertion. Isolation
    # already shares ledger/pidfile/locks across these three processes (same
    # env). A 3s self-timeout expires before backup can stay armed, so the
    # backup observes a missing watchdog lock and coexistence is untestable.
    # stderr must not be an unread PIPE: journal-degraded lines fill the
    # buffer and the backup never writes the ring.
    watchdog = subprocess.Popen(
        _watch_command(project, lease, timeout_s=60),
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=_stderr("watchdog"),
    )
    assert watchdog.stdout is not None
    watchdog_reader = _JsonLineReader(watchdog.stdout)
    _wait_for_waiter_kind(project, lease.label, wake.MONITOR_KIND, follow.pid)
    _wait_for_waiter_kind(project, lease.label, "watchdog", watchdog.pid)
    backup = subprocess.Popen(
        _backup_command(project, lease, timeout_s=300),
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=_stderr("backup"),
    )
    assert backup.stdout is not None
    backup_reader = _JsonLineReader(backup.stdout)
    replacement_backup: subprocess.Popen[bytes] | None = None
    try:
        try:
            _wait_for_waiter_kind(project, lease.label, "listener", backup.pid)
        except AssertionError:
            stderr_path = project.parent / "backup.stderr"
            detail = stderr_path.read_text(encoding="utf-8") if stderr_path.exists() else ""
            raise AssertionError(
                f"backup waiter missing poll={backup.poll()} stderr={detail!r}"
            ) from None
        _wait_for_waiter_kind(project, lease.label, "watchdog", watchdog.pid)
        assert watchdog.poll() is None, "watchdog exited before backup armed"

        listener_pids = [
            row.pid
            for row in wake.live_waiters(
                project,
                controller_label=lease.label,
                kinds={"listener"},
            )
            or []
        ]
        assert listener_pids == [backup.pid]
        assert backup.poll() is None, "arming the watchdog displaced the backup"
        extra = wake.register_listener_waiter(
            project,
            controller_label=lease.label,
            generation_key=lease.nonce,
            slots=2,
        )
        try:
            assert extra.slot_index == 1
            extra_pids = [
                row.pid
                for row in wake.live_waiters(
                    project,
                    controller_label=lease.label,
                    kinds={"listener"},
                )
                or []
            ]
            assert backup.pid in extra_pids
            assert len(extra_pids) == 2
            assert backup.poll() is None, "a second doorbell displaced the live backup"
        finally:
            extra.close()
        assert backup.poll() is None, "releasing the extra doorbell displaced the backup"

        messages.post_message(
            dispatch_id="backup-rings-with-watchdog",
            msg_type="controller-notice",
            payload={"text": "the backup must still deliver"},
            messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
            source={"node": "peer", "adapter": "pytest", "transport": "controller"},
            addressee=messages.controller_addressee(
                lease.label,
                project_root=project,
            ),
        )
        _raw, backup_result = backup_reader.read(timeout_s=30)
        if backup_result["kind"] == "pending-at-arm":
            pending_items = backup_result.get("items")
            assert isinstance(pending_items, list)
            assert any(
                isinstance(item, dict)
                and item.get("dispatch_id") == "backup-rings-with-watchdog"
                for item in pending_items
            ), "waiter lock became visible before the report-pending snapshot"
            messages.post_message(
                dispatch_id="backup-rings-after-arm",
                msg_type="controller-notice",
                payload={"text": "post-arm mail must ring the backup"},
                messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
                source={
                    "node": "peer",
                    "adapter": "pytest",
                    "transport": "controller",
                },
                addressee=messages.controller_addressee(
                    lease.label,
                    project_root=project,
                ),
            )
            _raw, backup_result = backup_reader.read(timeout_s=30)
        assert backup_result["kind"] == "ring", backup_result
        assert backup_result["reason"] == "event"
        assert backup.wait(timeout=15) == 0

        authority = journal.Journal(project)
        pending = authority.cursor_peek(
            lease.label,
            nonce=lease.nonce,
            waking_only=False,
        )
        advances: dict[str, int] = {}
        for item in pending.items:
            stream_id = str(item["stream_id"])
            advances[stream_id] = max(
                advances.get(stream_id, 0),
                int(item["stream_seq"]),
            )
        assert advances
        advanced = authority.advance_cursor(
            lease.label,
            nonce=lease.nonce,
            expected_cursor_version=pending.cursor_version,
            expected_stream_snapshots=pending.stream_snapshots,
            advances=advances,
            actor="follow-listener-test",
        )
        assert advanced.committed, advanced.reason

        replacement_backup = subprocess.Popen(
            _backup_command(project, lease),
            cwd=project,
            env=env,
            stdout=subprocess.PIPE,
            stderr=_stderr("replacement-backup"),
        )
        _wait_for_waiter_kind(
            project,
            lease.label,
            "listener",
            replacement_backup.pid,
        )
        os.kill(follow.pid, signal.SIGKILL)
        assert follow.wait(timeout=2) == -signal.SIGKILL
        _raw, dead = watchdog_reader.read(timeout_s=15)
        assert dead["kind"] == "event"
        assert dead["payload"]["type"] == "listener-dead"
        assert dead["payload"]["reason"] == "stale"
        assert replacement_backup.poll() is None
        assert watchdog.wait(timeout=2) == 0
    finally:
        processes = [backup, watchdog, follow]
        if replacement_backup is not None:
            processes.append(replacement_backup)
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
        for proc in processes:
            if proc.poll() is None:
                proc.wait(timeout=3)
        for handle in err_handles:
            handle.close()


def test_backup_wakes_when_watchdog_is_sigkilled_with_stream_alive(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    project, env, lease = isolated
    follow = _spawn_follow(project, env, lease, heartbeat_s=2.0, poll_s=0.25)
    assert follow.stdout is not None
    follow_reader = _JsonLineReader(follow.stdout)
    assert follow_reader.read()[1]["kind"] == "heartbeat"
    assert follow_reader.read()[1]["kind"] == "frontier"
    backup = subprocess.Popen(
        _backup_command(project, lease, timeout_s=300),
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    watchdog = subprocess.Popen(
        _watch_command(project, lease, timeout_s=60),
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert backup.stdout is not None
    backup_reader = _JsonLineReader(backup.stdout)
    try:
        _wait_for_waiter_kind(project, lease.label, wake.MONITOR_KIND, follow.pid)
        _wait_for_waiter_kind(project, lease.label, "listener", backup.pid)
        _wait_for_waiter_kind(project, lease.label, wake.WATCHDOG_KIND, watchdog.pid)

        os.kill(watchdog.pid, signal.SIGKILL)
        assert watchdog.wait(timeout=5) == -signal.SIGKILL
        def _watchdog_missing() -> bool:
            waiters = wake.live_waiters(
                project,
                controller_label=lease.label,
                kinds={wake.WATCHDOG_KIND},
            ) or []
            return all(row.pid != watchdog.pid for row in waiters)

        wait_until(
            _watchdog_missing,
            timeout_s=15,
            interval_s=0.02,
            message="watchdog lock to be missing after SIGKILL",
        )
        status = wake.coverage_status(
            project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
        )
        assert status["watchdog"]["state"] == "missing"
        assert all(
            row["kind"] != wake.WATCHDOG_KIND for row in status["waiters"]
        )

        _raw, dead = backup_reader.read(timeout_s=15)
        assert dead["kind"] == "event"
        assert dead["payload"]["type"] == "watchdog-dead"
        assert dead["payload"]["reason"] == "missing-lock"
        assert dead["payload"]["live"] == 1
        assert dead["payload"]["target"] == wake.persistent_wake_target()
        assert dead["payload"]["missing_components"] == ["backup", "watchdog"]
        assert dead["payload"]["rearm_command"] == (
            wake.follow_watchdog_start_command(
                project,
                controller_label=lease.label,
                lease_nonce=lease.nonce,
            )
        )
        assert follow.poll() is None
        assert backup.wait(timeout=2) == 0
    finally:
        for proc in (backup, watchdog, follow):
            if proc.poll() is None:
                proc.terminate()
        for proc in (backup, watchdog, follow):
            if proc.poll() is None:
                proc.wait(timeout=3)


def test_backup_wakes_when_watchdog_never_arms_after_grace(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    project, env, lease = isolated
    follow = _spawn_follow(project, env, lease, heartbeat_s=2.0, poll_s=0.25)
    assert follow.stdout is not None
    follow_reader = _JsonLineReader(follow.stdout)
    assert follow_reader.read()[1]["kind"] == "heartbeat"
    assert follow_reader.read()[1]["kind"] == "frontier"
    backup = subprocess.Popen(
        _backup_command(project, lease, timeout_s=300),
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert backup.stdout is not None
    backup_reader = _JsonLineReader(backup.stdout)
    try:
        _wait_for_waiter_kind(project, lease.label, "listener", backup.pid)
        _raw, dead = backup_reader.read(timeout_s=2)
        assert dead["kind"] == "event"
        assert dead["payload"]["type"] == "watchdog-dead"
        assert dead["payload"]["missing_components"] == ["backup", "watchdog"]
        assert follow.poll() is None
        assert backup.wait(timeout=2) == 0
    finally:
        for proc in (backup, follow):
            if proc.poll() is None:
                proc.terminate()
        for proc in (backup, follow):
            if proc.poll() is None:
                proc.wait(timeout=3)


def test_backup_witnesses_correlated_stream_and_watchdog_death(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    project, env, lease = isolated
    follow = _spawn_follow(project, env, lease, heartbeat_s=2.0, poll_s=0.25)
    assert follow.stdout is not None
    follow_reader = _JsonLineReader(follow.stdout)
    assert follow_reader.read()[1]["kind"] == "heartbeat"
    assert follow_reader.read()[1]["kind"] == "frontier"
    backup = subprocess.Popen(
        _backup_command(project, lease, timeout_s=300),
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    watchdog = subprocess.Popen(
        _watch_command(project, lease),
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert backup.stdout is not None
    backup_reader = _JsonLineReader(backup.stdout)
    try:
        _wait_for_waiter_kind(project, lease.label, wake.MONITOR_KIND, follow.pid)
        _wait_for_waiter_kind(project, lease.label, "listener", backup.pid)
        _wait_for_waiter_kind(project, lease.label, wake.WATCHDOG_KIND, watchdog.pid)

        os.kill(follow.pid, signal.SIGKILL)
        os.kill(watchdog.pid, signal.SIGKILL)
        assert follow.wait(timeout=2) == -signal.SIGKILL
        assert watchdog.wait(timeout=2) == -signal.SIGKILL

        _raw, dead = backup_reader.read(timeout_s=2)
        assert dead["kind"] == "event"
        assert dead["payload"]["type"] == "watchdog-dead"
        assert dead["payload"]["live"] == 0
        assert dead["payload"]["missing_components"] == [
            "stream",
            "backup",
            "watchdog",
        ]
        assert backup.wait(timeout=2) == 0
    finally:
        for proc in (backup, watchdog, follow):
            if proc.poll() is None:
                proc.terminate()
        for proc in (backup, watchdog, follow):
            if proc.poll() is None:
                proc.wait(timeout=3)


def test_watchdog_reads_durable_age_and_wakes_with_exact_rearm(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    project, env, lease = isolated
    wake.activate_monitor_state(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        heartbeat_s=0.05,
        dead_after_s=0.15,
        now_epoch=time.time() - 1,
    )
    env["GOALFLIGHT_TEST_LISTENER_START_TOKEN"] = "watchdog-test-token"
    completed = subprocess.run(
        _watch_command(project, lease),
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert completed.returncode == 0, completed.stderr
    lines = [json.loads(line) for line in completed.stdout.splitlines() if line]
    assert lines[-1]["kind"] == "event"
    assert lines[-1]["payload"]["type"] == "listener-dead"
    assert lines[-1]["payload"]["reason"] == "stale"
    assert lines[-1]["payload"]["backup_required"] is True
    assert lines[-1]["payload"]["rearm_command"] == wake.follow_start_command(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    )
    assert wake.live_waiters(
        project,
        controller_label=lease.label,
        kinds={"listener", "watchdog"},
    ) == []


def test_watchdog_releases_lock_before_listener_dead_flush(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _env, lease = isolated
    wake.activate_monitor_state(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        heartbeat_s=0.05,
        dead_after_s=0.15,
        now_epoch=time.time() - 1,
    )
    observed_watchdogs: list[list[int]] = []

    def observe_flush(record: dict[str, object], **_kwargs: object) -> bool:
        payload = record.get("payload")
        if isinstance(payload, dict) and payload.get("type") == "listener-dead":
            observed_watchdogs.append(
                [
                    row.pid
                    for row in wake.live_waiters(
                        project,
                        controller_label=lease.label,
                        kinds={wake.WATCHDOG_KIND},
                    )
                    or []
                ]
            )
        return True

    monkeypatch.setattr(messages, "_follow_stdout_refusal", lambda _stream: None)
    monkeypatch.setattr(messages, "_stdio_peer_gone", lambda _stream: False)
    monkeypatch.setattr(messages, "_write_follow_record", observe_flush)
    result = messages._run_cli(_watch_command(project, lease)[2:])

    assert result == 0
    assert observed_watchdogs == [[]]


def test_watchdog_graces_preexisting_stale_state(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _env, lease = isolated
    wake.activate_monitor_state(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        heartbeat_s=0.05,
        dead_after_s=0.15,
        now_epoch=time.time() - 1,
    )
    monkeypatch.setattr(messages, "_follow_stdout_refusal", lambda _stream: None)
    monkeypatch.setattr(messages, "_stdio_peer_gone", lambda _stream: False)

    started = time.monotonic()
    result = messages._run_cli(_watch_command(project, lease)[2:])
    elapsed = time.monotonic() - started

    assert result == 0
    assert elapsed >= 0.08, f"pre-existing stale state bypassed grace: {elapsed:.3f}s"


def test_watchdog_grace_does_not_hide_a_new_follow_fault(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    project, env, lease = isolated
    env = dict(env)
    env.pop("GOALFLIGHT_TEST_MODE", None)
    wake.activate_monitor_state(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        heartbeat_s=60,
        dead_after_s=180,
        now_epoch=time.time() - 181,
    )
    watchdog = subprocess.Popen(
        _watch_command(project, lease),
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert watchdog.stdout is not None
    reader = _JsonLineReader(watchdog.stdout)
    try:
        _wait_for_waiter_kind(
            project,
            lease.label,
            wake.WATCHDOG_KIND,
            watchdog.pid,
        )
        time.sleep(0.1)
        wake.activate_monitor_state(
            project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
            heartbeat_s=60,
            dead_after_s=180,
        )
        wake.record_monitor_fault(
            project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
            reason="new-follow-fault",
        )
        _raw, dead = reader.read(timeout_s=2)
        assert dead["payload"]["reason"] == "new-follow-fault"
        assert watchdog.wait(timeout=2) == 0
    finally:
        if watchdog.poll() is None:
            watchdog.terminate()
            watchdog.wait(timeout=3)


def test_watchdog_wakes_when_durable_follow_state_never_appears(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    project, env, lease = isolated
    env["GOALFLIGHT_TEST_LISTENER_START_TOKEN"] = "missing-state-token"
    completed = subprocess.run(
        _watch_command(project, lease),
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert completed.returncode == 0, completed.stderr
    lines = [json.loads(line) for line in completed.stdout.splitlines() if line]
    assert lines[-1]["kind"] == "event"
    assert lines[-1]["payload"]["type"] == "listener-dead"
    assert lines[-1]["payload"]["reason"] == "state-unavailable"
    assert lines[-1]["payload"]["backup_required"] is True


def test_monitor_status_distinguishes_unreadable_from_missing(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    project, _env, lease = isolated
    assert (
        wake.monitor_status(
            project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
        )
        is None
    )
    wake.activate_monitor_state(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        heartbeat_s=60,
        dead_after_s=180,
    )
    path = wake._monitor_state_path(project, controller_label=lease.label)
    path.write_text("{not-json", encoding="utf-8")
    status = wake.monitor_status(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    )
    assert status is not None
    assert status["state"] == "unreadable"
    assert status["reason"] == "monitor-state-io"
    assert status.get("age_s") is None


def test_watchdog_unreadable_follow_state_does_not_set_backup_required(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    project, env, lease = isolated
    wake.activate_monitor_state(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        heartbeat_s=60,
        dead_after_s=180,
    )
    path = wake._monitor_state_path(project, controller_label=lease.label)
    path.write_text("{not-json", encoding="utf-8")
    completed = subprocess.run(
        _watch_command(project, lease, timeout_s=1),
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert completed.returncode == 1, completed.stderr
    lines = [json.loads(line) for line in completed.stdout.splitlines() if line]
    dead = [
        row
        for row in lines
        if row.get("kind") == "event"
        and isinstance(row.get("payload"), dict)
        and row["payload"].get("type") == "listener-dead"
    ]
    assert not dead, dead
    degraded = [
        row
        for row in lines
        if row.get("kind") == "event"
        and isinstance(row.get("payload"), dict)
        and row["payload"].get("type") == "listener-degraded"
    ]
    assert degraded
    assert degraded[0]["payload"]["reason"] == "monitor-state-io"
    assert "backup_required" not in degraded[0]["payload"]
    assert "rearm_command" not in degraded[0]["payload"]


def test_default_death_threshold_requires_three_missed_heartbeats() -> None:
    assert messages.FOLLOW_DEAD_AFTER_INTERVALS >= 3
    assert messages.FOLLOW_DEAD_AFTER_SECS == (
        messages.FOLLOW_HEARTBEAT_SECS
        * messages.FOLLOW_DEAD_AFTER_INTERVALS
    )


@pytest.mark.parametrize("heartbeat_s", (30, 301))
def test_production_heartbeat_rejects_volume_and_deafness_extremes(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    heartbeat_s: int,
) -> None:
    project, _env, lease = isolated
    monkeypatch.delenv("GOALFLIGHT_TEST_MODE", raising=False)
    result = messages._run_cli(
        [
            "follow",
            "--project-root",
            str(project),
            "--controller-label",
            lease.label,
            "--lease-nonce",
            lease.nonce,
            "--heartbeat-secs",
            str(heartbeat_s),
        ]
    )
    assert result == 2
    assert "between 60 and 300 seconds" in capsys.readouterr().err
    assert not wake.live_waiters(
        project,
        controller_label=lease.label,
        kinds={wake.MONITOR_KIND},
    )


def test_follow_rejects_regular_file_stdout_before_claiming_monitor(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    tmp_path: Path,
) -> None:
    project, env, lease = isolated
    output = tmp_path / "not-a-monitor.jsonl"
    with output.open("w", encoding="utf-8") as stream:
        refused = subprocess.run(
            _follow_command(project, lease, heartbeat_s=0.1),
            cwd=project,
            env=env,
            stdout=stream,
            stderr=subprocess.PIPE,
            text=True,
            timeout=3,
        )
    assert refused.returncode == 2
    assert "stdout is a regular file" in refused.stderr
    assert "stdout must be owned" in refused.stderr
    assert not wake.live_waiters(
        project,
        controller_label=lease.label,
        kinds={wake.MONITOR_KIND},
    )


def test_follow_rejects_pool_flag_and_warns_for_inert_pool_environment(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    project, env, lease = isolated
    refused = subprocess.run(
        [
            *_follow_command(project, lease, heartbeat_s=0.1),
            "--listener-slots",
            "2",
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert refused.returncode == 2
    assert "unrecognized arguments: --listener-slots 2" in refused.stderr

    env["GOALFLIGHT_LISTENER_SLOTS"] = "9"
    env["GOALFLIGHT_LISTENER_LOW_WATER"] = "4"
    previous = dict(os.environ)
    try:
        os.environ.update(env)
        warnings = messages._follow_inert_knob_warnings()
    finally:
        os.environ.clear()
        os.environ.update(previous)
    assert any("GOALFLIGHT_LISTENER_SLOTS affects only" in line for line in warnings)
    assert any("GOALFLIGHT_LISTENER_LOW_WATER affects only" in line for line in warnings)


def test_watchdog_warns_that_delivery_flags_are_ignored(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    project, env, lease = isolated
    wake.activate_monitor_state(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        heartbeat_s=0.05,
        dead_after_s=0.15,
        now_epoch=time.time() - 1,
    )
    completed = subprocess.run(
        [*_watch_command(project, lease), "--listener-slots", "2", "--report-pending"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=8,
    )

    assert completed.returncode == 0, completed.stderr
    assert "ignoring --listener-slots, --report-pending" in completed.stderr
    assert wake.persistent_backup_start_command(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    ) in completed.stderr


def test_direct_watchdog_beside_detected_supervisor_omits_backup_command(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    tmp_path: Path,
) -> None:
    project, env, lease = isolated
    wake.activate_monitor_state(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        heartbeat_s=0.05,
        dead_after_s=0.15,
        now_epoch=time.time() - 1,
    )
    supervisor = wake.coverage_supervise_command(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    )
    listed_supervisor = shlex.join(
        [*shlex.split(supervisor), "--lease-nonce", lease.nonce]
    )
    shim_dir = tmp_path / "running-supervisor-process-listing"
    shim_dir.mkdir()
    ps_shim = shim_dir / "ps"
    process_row = f"4242 {listed_supervisor}"
    ps_shim.write_text(
        "#!/bin/sh\nprintf '%s\\n' " + shlex.quote(process_row) + "\n",
        encoding="utf-8",
    )
    ps_shim.chmod(0o755)
    direct_env = {**env, "PATH": f"{shim_dir}:{env.get('PATH', '')}"}
    direct_env.pop("GOALFLIGHT_SUPERVISED", None)

    completed = subprocess.run(
        [*_watch_command(project, lease), "--listener-slots", "2"],
        cwd=project,
        env=direct_env,
        capture_output=True,
        text=True,
        timeout=8,
    )

    forbidden = wake.persistent_backup_start_command(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    )
    assert completed.returncode == 0, completed.stderr
    assert "ignoring --listener-slots; the supervisor owns backup replacement" in completed.stderr
    assert forbidden not in completed.stderr


@pytest.mark.parametrize("mode", ["direct-with-supervisor", "orphaned-child"])
def test_detached_watchdog_uses_detected_supervisor_ownership(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    project, _env, lease = isolated
    supervise_command = wake.coverage_supervise_command(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    )
    listed_supervise_command = shlex.join(
        [*shlex.split(supervise_command), "--lease-nonce", lease.nonce]
    )
    monkeypatch.setenv("GOALFLIGHT_LISTENER_STARTUP_GRACE_S", "0.05")
    monkeypatch.setattr(messages.os, "getppid", lambda: 1)
    monkeypatch.setattr(messages, "_follow_stdout_refusal", lambda _stream: None)
    if mode == "direct-with-supervisor":
        monkeypatch.delenv("GOALFLIGHT_SUPERVISED", raising=False)
        monkeypatch.setattr(
            wake,
            "_process_listing",
            lambda **_kwargs: [(4242, listed_supervise_command)],
        )
    else:
        monkeypatch.setenv("GOALFLIGHT_SUPERVISED", "1")
        monkeypatch.setattr(wake, "_process_listing", lambda **_kwargs: [])

    code = messages._cmd_watch_follow(
        SimpleNamespace(timeout_s=2),
        project_root=project,
        label=lease.label,
        nonce=lease.nonce,
        poll=0.01,
        journal_tolerance=messages._JournalBusyTolerance(
            messages.LISTENER_JOURNAL_TOLERANCE_S,
            messages.LISTENER_JOURNAL_BACKOFF_CAP_S,
        ),
    )
    captured = capsys.readouterr()

    assert code == messages.DETACHED_LISTENER_EXIT_CODE
    assert "goalflight_messages.py listen" not in captured.err
    assert "goalflight_messages.py follow" not in captured.err
    if mode == "direct-with-supervisor":
        assert "supervisor owns replacement" in captured.err
        assert "Restart the supervisor" not in captured.err
    else:
        assert "supervisor parent is gone" in captured.err
        assert "Restart the supervisor" in captured.err
        assert supervise_command in captured.err


def test_supervised_watchdog_stdout_loss_during_orphan_grace_restarts_supervisor(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, _env, lease = isolated
    supervise_command = wake.coverage_supervise_command(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    )
    released = False
    real_register = wake.register_watchdog_waiter

    class TrackedWaiter:
        def __init__(self, inner) -> None:
            self.inner = inner

        def close(self) -> None:
            nonlocal released
            self.inner.close()
            released = True

    def register_tracked(*args, **kwargs):
        return TrackedWaiter(real_register(*args, **kwargs))

    def listing_after_release(**_kwargs):
        assert released, "watchdog recovery was emitted before lock release"
        return []

    monkeypatch.setenv("GOALFLIGHT_SUPERVISED", "1")
    monkeypatch.setenv("GOALFLIGHT_LISTENER_STARTUP_GRACE_S", "60")
    monkeypatch.setattr(messages.os, "getppid", lambda: 1)
    monkeypatch.setattr(messages, "_follow_stdout_refusal", lambda _stream: None)
    monkeypatch.setattr(messages, "_stdio_peer_gone", lambda _stream: True)
    monkeypatch.setattr(wake, "register_watchdog_waiter", register_tracked)
    monkeypatch.setattr(wake, "_process_listing", listing_after_release)

    code = messages._cmd_watch_follow(
        SimpleNamespace(timeout_s=2),
        project_root=project,
        label=lease.label,
        nonce=lease.nonce,
        poll=0.01,
        journal_tolerance=messages._JournalBusyTolerance(
            messages.LISTENER_JOURNAL_TOLERANCE_S,
            messages.LISTENER_JOURNAL_BACKOFF_CAP_S,
        ),
    )
    captured = capsys.readouterr()

    assert code == 3
    assert released
    assert "supervisor parent is gone" in captured.err
    assert "Restart the supervisor" in captured.err
    assert supervise_command in captured.err
    assert "goalflight_messages.py listen" not in captured.err
    assert "goalflight_messages.py follow" not in captured.err


# --- b-214: a transient journal-busy must not kill a persistent listener ---
#
# The busy condition is injected deterministically by gating _sqlite_connect on
# the temp journal's URI (the same injection style as
# test_listener_survives_present_journal_open_failure_and_times_out), so the
# tests drive goalflight_journal._connect's real _is_busy/_retry_delay path —
# the exact code that produced the observed "journal connection remained busy
# after 34 attempts within 1.000s" fault. No fixed sleeps are used for
# synchronization: every wait is an event-driven poll with a generous bound,
# safe on a heavily loaded box.


class _LiveCapture:
    """Accumulate capsys output while a listener runs in a thread."""

    def __init__(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._capsys = capsys
        self._pending = ""
        self.records: list[dict[str, object]] = []
        self.stdout = ""
        self.stderr = ""

    def pump(self) -> None:
        captured = self._capsys.readouterr()
        self.stderr += captured.err
        self.stdout += captured.out
        self._pending += captured.out
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # non-JSON noise is not a follow record
            if isinstance(record, dict):
                self.records.append(record)

    def count(self, predicate) -> int:
        self.pump()
        return sum(1 for record in self.records if predicate(record))

    def await_count(self, predicate, minimum: int, timeout_s: float = 15.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.count(predicate) >= minimum:
                return
            time.sleep(0.02)
        raise AssertionError(
            f"timed out waiting for {minimum} records; saw {self.records!r} "
            f"stderr={self.stderr!r}"
        )

    def await_stderr(self, needle: str, timeout_s: float = 15.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self.pump()
            if needle in self.stderr:
                return
            time.sleep(0.02)
        raise AssertionError(
            f"timed out waiting for stderr {needle!r}; saw {self.stderr!r}"
        )

    def await_stdout(self, needle: str, timeout_s: float = 15.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self.pump()
            if needle in self.stdout:
                return
            time.sleep(0.02)
        raise AssertionError(
            f"timed out waiting for stdout {needle!r}; saw {self.stdout!r}"
        )


def _run_in_thread(argv: list[str]) -> tuple[threading.Thread, list[int]]:
    results: list[int] = []
    thread = threading.Thread(
        target=lambda: results.append(messages._run_cli(argv)),
        daemon=True,
    )
    thread.start()
    return thread, results


def _gate_journal_connects(
    monkeypatch: pytest.MonkeyPatch,
    project: Path,
    gate: threading.Event,
) -> list[str]:
    """While `gate` is set, every fresh journal connection reports busy."""
    journal_uri = journal.resolve_journal_path(project).as_uri()
    real_connect = journal._sqlite_connect
    hits: list[str] = []

    def gated_connect(database: object, *args: object, **kwargs: object):
        if gate.is_set() and str(database).startswith(journal_uri):
            hits.append(str(database))
            raise sqlite3.OperationalError("database is locked")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(journal, "_sqlite_connect", gated_connect)
    return hits


def _count_journal_connects(
    monkeypatch: pytest.MonkeyPatch,
    project: Path,
    *,
    phase: threading.Event | None = None,
) -> list[str]:
    """Count journal opens at the shared connection seam during ``phase``."""
    journal_uri = journal.resolve_journal_path(project).as_uri()
    real_connect = journal._sqlite_connect
    opens: list[str] = []

    def counted_connect(database: object, *args: object, **kwargs: object):
        if str(database).startswith(journal_uri) and (
            phase is None or phase.is_set()
        ):
            opens.append(str(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(journal, "_sqlite_connect", counted_connect)
    return opens


class _QueryBusyConnection:
    """Connection proxy that injects busy only after _connect setup succeeds."""

    def __init__(self, connection: sqlite3.Connection, hits: list[str]) -> None:
        self._connection = connection
        self._hits = hits

    def execute(self, sql: str, *args: object, **kwargs: object):
        self._hits.append(" ".join(sql.split())[:80])
        raise sqlite3.OperationalError("database is locked")

    def __getattr__(self, name: str):
        return getattr(self._connection, name)


def _gate_journal_queries(
    monkeypatch: pytest.MonkeyPatch,
    project: Path,
    gate: threading.Event,
) -> list[str]:
    """Inject busy on SQL after each real connection completed its PRAGMAs."""
    journal_path = journal.resolve_journal_path(project)
    real_connect = journal.Journal._connect
    hits: list[str] = []

    def gated_connect(authority: journal.Journal, **kwargs: object):
        connection = real_connect(authority, **kwargs)
        if gate.is_set() and authority.path == journal_path:
            return _QueryBusyConnection(connection, hits)
        return connection

    monkeypatch.setattr(journal.Journal, "_connect", gated_connect)
    return hits


def _gate_attention_reads(
    monkeypatch: pytest.MonkeyPatch,
    gate: threading.Event,
) -> list[str]:
    """Inject typed query-stage busy in synthetic envelope materialization."""
    real_attention_items = journal.Journal.attention_items
    hits: list[str] = []

    def gated_attention_items(authority: journal.Journal, *args: object, **kwargs: object):
        if gate.is_set():
            hits.append("busy")
            raise journal.JournalBusy("injected attention query busy")
        hits.append("success")
        return real_attention_items(authority, *args, **kwargs)

    monkeypatch.setattr(journal.Journal, "attention_items", gated_attention_items)
    return hits


def _materialize_synthetic_attention(
    project: Path,
    lease: journal.LeaseIdentity,
) -> None:
    """Create a journal-backed carrier without touching the real journal."""
    authority = journal.Journal(project)
    prepared = authority.prepare_attempt("round3-attention-work")
    assert prepared.committed
    armed = authority.arm_listener(
        lease.label,
        nonce=lease.nonce,
        pid=os.getpid(),
        start_token="round3-attention-source",
        parent_pid=os.getppid() or os.getpid(),
    )
    assert armed.committed and armed.value is not None
    exited = authority.exit_listener(
        str(armed.value["coverage_id"]),
        reason="orphaned",
    )
    assert exited.committed
    assert authority.attention_items()


def _await_live_waiter(project: Path, label: str, kind: str, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        waiters = wake.live_waiters(project, controller_label=label, kinds={kind})
        if waiters:
            return
        time.sleep(0.02)
    raise AssertionError(f"no live {kind} waiter appeared")


def _await_armed_listener(project: Path, label: str, timeout_s: float = 30.0) -> None:
    """Wait on the kernel slot lock. Journal coverage is a single-row audit."""
    _await_live_waiter(project, label, "listener", timeout_s=timeout_s)


def _is_heartbeat(record: dict[str, object]) -> bool:
    return record.get("kind") == "heartbeat"


def _is_follow_event(record: dict[str, object], event_type: str) -> bool:
    payload = record.get("payload")
    return (
        record.get("kind") == "event"
        and isinstance(payload, dict)
        and payload.get("type") == event_type
    )


def _follow_argv(project: Path, lease: journal.LeaseIdentity) -> list[str]:
    return [
        "follow",
        "--project-root",
        str(project),
        "--controller-label",
        lease.label,
        "--lease-nonce",
        lease.nonce,
        "--heartbeat-secs",
        "0.01",
        "--poll-secs",
        "0.01",
    ]


def _release_lease(project: Path, lease: journal.LeaseIdentity) -> None:
    released = journal.Journal(project).release_lease(lease.label, nonce=lease.nonce)
    assert released.committed


def _pin_listener_resolution(
    monkeypatch: pytest.MonkeyPatch,
    lease: journal.LeaseIdentity,
) -> None:
    monkeypatch.setattr(
        messages,
        "_resolve_listen_auto_lease",
        lambda *_args, **_kwargs: {
            "claimed": True,
            "reason": "test-pinned",
            "label": lease.label,
            "nonce": lease.nonce,
            "lease_generation": lease.generation,
        },
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS proc_pid_rusage only")
def test_idle_listener_does_not_write_journal_each_poll(isolated) -> None:
    project, env, lease = isolated
    command = _backup_command(project, lease, timeout_s=3)
    command[command.index("--listener-slots") + 1] = "2"
    listener = subprocess.Popen(
        command,
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_waiter_kind(project, lease.label, "listener", listener.pid)
        time.sleep(0.05)
        before = _mac_disk_bytes_written(listener.pid)
        started = time.monotonic()
        time.sleep(0.25)
        elapsed = time.monotonic() - started
        after = _mac_disk_bytes_written(listener.pid)
        assert listener.poll() is None
    finally:
        if listener.poll() is None:
            listener.terminate()
        stdout, stderr = listener.communicate(timeout=5)
    assert listener.returncode in {0, 1, -signal.SIGTERM, 128 + signal.SIGTERM}
    cycles = max(1, round(elapsed / 0.01))
    written = after - before
    assert written < 64 * 1024, (
        f"idle listener wrote {written} bytes over ~{cycles} poll cycles "
        f"({written / cycles:.0f} bytes/cycle); stdout={stdout!r}; "
        f"stderr={stderr!r}"
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS proc_pid_rusage only")
def test_listener_start_to_exit_writes_are_bounded(
    isolated,
    tmp_path: Path,
) -> None:
    project, env, lease = isolated
    counter = tmp_path / "listener-connects.log"
    env = dict(env)
    env["GOALFLIGHT_TEST_SQLITE_CONNECT_COUNTER"] = str(counter)
    command = _backup_command(project, lease, timeout_s=3)
    command.remove("--report-pending")
    listener = subprocess.Popen(
        command,
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    total_written = None
    try:
        _wait_for_waiter_kind(project, lease.label, "listener", listener.pid)
        time.sleep(0.15)
        _release_lease(project, lease)
        assert listener.stdout is not None
        exit_record = _JsonLineReader(listener.stdout).read(timeout_s=5)[1]
        assert exit_record["kind"] == "exit", exit_record
        total_written = _mac_disk_bytes_written(listener.pid)
        stdout, stderr = listener.communicate(timeout=5)
    finally:
        if listener.poll() is None:
            listener.terminate()
        if listener.poll() is None:
            listener.wait(timeout=5)
    assert total_written is not None
    assert listener.returncode == 3, (
        f"unexpected listener exit {listener.returncode}; "
        f"stdout={stdout!r}; stderr={stderr!r}"
    )
    entries = counter.read_text(encoding="utf-8").splitlines()
    child_pids = {int(entry.split("\t", 1)[0]) for entry in entries}
    reader_entries = [entry for entry in entries if "?mode=ro" in entry]
    assert child_pids == {listener.pid}, (
        f"counter did not observe the listener child: {entries!r}"
    )
    assert 1 <= len(reader_entries) <= 4, (
        f"listener opened {len(reader_entries)} readonly journal connections: "
        f"{reader_entries!r}"
    )
    assert total_written < 1024 * 1024, (
        f"listener start-to-exit wrote {total_written} bytes"
    )


def test_reader_closes_connection_when_replaced_after_open(
    isolated,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _env, _lease = isolated
    reader = journal.Journal.open_reader(project, persistent=True)
    connections = []
    real_open = journal._open_readonly_connection

    def open_then_replace(*args, **kwargs):
        connection = real_open(*args, **kwargs)
        connections.append(connection)
        replacement = reader.path.with_suffix(".replacement")
        replacement.write_bytes(reader.path.read_bytes())
        replacement.replace(reader.path)
        return connection

    monkeypatch.setattr(journal, "_open_readonly_connection", open_then_replace)
    try:
        with pytest.raises(journal.JournalIntegrityError, match="database was replaced"):
            reader._connect()
        assert len(connections) == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connections[0].execute("SELECT 1")
        assert reader._reader_connection is None
    finally:
        for connection in connections:
            connection.close()


def test_listener_ring_broken_pipe_closes_reader(
    isolated,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, env, lease = isolated
    args = SimpleNamespace(
        project_root=str(project),
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        poll_secs=0.01,
        listener_slots=2,
        timeout_s=5.0,
        json=True,
        report_pending=False,
        watch_follow=False,
    )
    connections = []
    real_data_version = journal.Journal._data_version

    def post_after_reader_opens(authority):
        version = real_data_version(authority)
        if not connections:
            connections.append(authority._reader_connection)
            messages.post_message(
                dispatch_id="broken-ring-output",
                msg_type="controller-notice",
                payload={"text": "ring output fails"},
                messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
                source={"node": "peer", "adapter": "pytest", "transport": "controller"},
                addressee=messages.controller_addressee(lease.label, project_root=project),
            )
        return version

    class BrokenRingOutput(io.StringIO):
        def write(self, value):
            if '"kind": "ring"' in value:
                raise BrokenPipeError("ring consumer closed")
            return super().write(value)

    monkeypatch.setattr(journal.Journal, "_data_version", post_after_reader_opens)
    monkeypatch.setattr(messages._ListenerDeathWatch, "install", lambda _self: None)
    monkeypatch.setattr(messages._ListenerDeathWatch, "restore", lambda _self: None)
    try:
        with contextlib.redirect_stdout(BrokenRingOutput()):
            with pytest.raises(BrokenPipeError, match="ring consumer closed"):
                messages.cmd_listen(args)
        assert len(connections) == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connections[0].execute("SELECT 1")
        snapshot = journal.Journal.open_reader(project).cursor_peek(
            lease.label, nonce=lease.nonce,
        )
        assert wake.claim_ring(
            project, controller_label=lease.label, cursor_version=snapshot.cursor_version,
        )
    finally:
        for connection in connections:
            connection.close()


def test_idle_listener_reuses_journal_connection(
    isolated,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _env, lease = isolated
    args = SimpleNamespace(
        project_root=str(project),
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        poll_secs=0.01,
        listener_slots=2,
        timeout_s=5.0,
        json=True,
        report_pending=False,
        watch_follow=False,
    )
    poll_phase = threading.Event()
    poll_count = 0
    reader_ready = threading.Event()
    polls_ready = threading.Event()
    real_data_version = journal.Journal._data_version

    def counted_data_version(authority):
        nonlocal poll_count
        version = real_data_version(authority)
        if not reader_ready.is_set():
            reader_ready.set()
        elif poll_phase.is_set():
            poll_count += 1
            if poll_count >= 5:
                polls_ready.set()
        return version

    monkeypatch.setattr(journal.Journal, "_data_version", counted_data_version)
    opens = _count_journal_connects(monkeypatch, project, phase=poll_phase)
    monkeypatch.setattr(messages._ListenerDeathWatch, "install", lambda _self: None)
    monkeypatch.setattr(messages._ListenerDeathWatch, "restore", lambda _self: None)
    stdout = io.StringIO()
    stderr = io.StringIO()
    result: list[int] = []

    def run_listener() -> None:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result.append(messages.cmd_listen(args))

    thread = threading.Thread(target=run_listener)
    thread.start()
    try:
        _wait_for_waiter_kind(project, lease.label, "listener", os.getpid())
        assert reader_ready.wait(5), "listener did not establish its reader"
        poll_phase.set()
        assert polls_ready.wait(5), f"listener did not reach poll phase: {poll_count}"
        opens_after_idle = len(opens)
    finally:
        poll_phase.clear()
        if thread.is_alive():
            _release_lease(project, lease)
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert result == [3], stderr.getvalue()
    assert opens_after_idle == 0, (
        f"idle poll phase opened {opens_after_idle} journal connections: "
        f"{opens!r}"
    )


def test_pending_unclaimed_ring_obeys_poll_interval(
    isolated,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, env, lease = isolated
    args = SimpleNamespace(
        project_root=str(project),
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        poll_secs=0.2,
        listener_slots=2,
        timeout_s=1.5,
        json=True,
        report_pending=False,
        watch_follow=False,
    )
    checks = 0
    real_data_version = journal.Journal._data_version

    def counted_data_version(authority):
        nonlocal checks
        checks += 1
        return real_data_version(authority)

    monkeypatch.setattr(journal.Journal, "_data_version", counted_data_version)
    monkeypatch.setattr(wake, "claim_ring", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(messages._ListenerDeathWatch, "install", lambda _self: None)
    monkeypatch.setattr(messages._ListenerDeathWatch, "restore", lambda _self: None)
    stdout = io.StringIO()
    stderr = io.StringIO()
    result: list[int] = []

    def run_listener() -> None:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result.append(messages.cmd_listen(args))

    thread = threading.Thread(target=run_listener)
    thread.start()
    try:
        _wait_for_waiter_kind(project, lease.label, "listener", os.getpid())
        time.sleep(0.25)
        before_event = checks
        messages.post_message(
            dispatch_id="unclaimed-ring",
            msg_type="controller-notice",
            payload={"text": "pending but not claimed"},
            messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
            source={"node": "peer", "adapter": "pytest", "transport": "controller"},
            addressee=messages.controller_addressee(lease.label, project_root=project),
        )
        time.sleep(0.65)
        after_event = checks
    finally:
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert result == [1], stderr.getvalue()
    assert after_event > before_event
    assert after_event - before_event <= 5


def test_pending_ring_retries_after_failed_claim(
    isolated,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, env, lease = isolated
    poll_secs = 0.05
    args = SimpleNamespace(
        project_root=str(project),
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        poll_secs=poll_secs,
        listener_slots=2,
        timeout_s=2.0,
        json=True,
        report_pending=False,
        watch_follow=False,
    )
    claim_calls: list[float] = []

    def claim_ring(*_args: object, **_kwargs: object) -> bool:
        claim_calls.append(time.monotonic())
        return len(claim_calls) > 1

    monkeypatch.setattr(wake, "claim_ring", claim_ring)
    monkeypatch.setattr(messages._ListenerDeathWatch, "install", lambda _self: None)
    monkeypatch.setattr(messages._ListenerDeathWatch, "restore", lambda _self: None)
    stdout = io.StringIO()
    stderr = io.StringIO()
    result: list[int] = []

    def run_listener() -> None:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result.append(messages.cmd_listen(args))

    thread = threading.Thread(target=run_listener)
    thread.start()
    try:
        _wait_for_waiter_kind(project, lease.label, "listener", os.getpid())
        messages.post_message(
            dispatch_id="retry-after-ring-claim",
            msg_type="controller-notice",
            payload={"text": "ring release must be observed"},
            messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
            source={"node": "peer", "adapter": "pytest", "transport": "controller"},
            addressee=messages.controller_addressee(
                lease.label,
                project_root=project,
            ),
        )
        thread.join(timeout=5)
    finally:
        if thread.is_alive():
            _release_lease(project, lease)
            thread.join(timeout=5)

    assert not thread.is_alive(), stderr.getvalue()
    assert result == [0], stderr.getvalue()
    assert len(claim_calls) == 2, claim_calls
    assert claim_calls[0] < claim_calls[1], claim_calls
    assert json.loads(stdout.getvalue())["kind"] == "ring"


def test_follow_survives_busy_during_constructor_startup(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, _env, lease = isolated
    monkeypatch.setattr(messages, "_follow_stdout_refusal", lambda _stream: None)
    monkeypatch.setattr(messages, "LISTENER_JOURNAL_TOLERANCE_S", 30.0)
    monkeypatch.setattr(messages, "LISTENER_JOURNAL_BUSY_BUDGET_S", 0.05)
    _pin_listener_resolution(monkeypatch, lease)
    gate = threading.Event()
    gate.set()
    hits = _gate_journal_connects(monkeypatch, project, gate)
    cap = _LiveCapture(capsys)

    thread, results = _run_in_thread(_follow_argv(project, lease))
    try:
        cap.await_count(
            lambda record: _is_follow_event(record, "listener-degraded"), 1
        )
        assert hits, "constructor connect-stage busy injection did not bind"
        assert thread.is_alive()
        gate.clear()
        cap.await_count(
            lambda record: _is_follow_event(record, "listener-recovered"), 1
        )
        cap.await_count(_is_heartbeat, 1)
    finally:
        gate.clear()
        _release_lease(project, lease)
    thread.join(15.0)
    assert results == [3]


def test_watchdog_survives_busy_during_constructor_startup(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, _env, lease = isolated
    monkeypatch.setattr(messages, "_follow_stdout_refusal", lambda _stream: None)
    monkeypatch.setattr(messages, "LISTENER_JOURNAL_TOLERANCE_S", 30.0)
    monkeypatch.setattr(messages, "LISTENER_JOURNAL_BUSY_BUDGET_S", 0.05)
    _pin_listener_resolution(monkeypatch, lease)
    wake.activate_monitor_state(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        heartbeat_s=60.0,
        dead_after_s=180.0,
    )
    gate = threading.Event()
    gate.set()
    hits = _gate_journal_connects(monkeypatch, project, gate)
    cap = _LiveCapture(capsys)

    thread, results = _run_in_thread(
        [
            "listen",
            "--watch-follow",
            "--project-root",
            str(project),
            "--controller-label",
            lease.label,
            "--lease-nonce",
            lease.nonce,
            "--poll-secs",
            "0.01",
            "--timeout-s",
            "60",
        ]
    )
    try:
        cap.await_stderr("watchdog degraded")
        assert hits, "watchdog constructor busy injection did not bind"
        assert thread.is_alive()
        gate.clear()
        cap.await_stderr("watchdog recovered")
        _await_live_waiter(project, lease.label, wake.WATCHDOG_KIND)
    finally:
        gate.clear()
        _release_lease(project, lease)
    thread.join(15.0)
    assert results == [3]


def test_listen_survives_busy_during_journal_coverage_arm(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, _env, lease = isolated
    monkeypatch.setattr(messages, "LISTENER_JOURNAL_TOLERANCE_S", 30.0)
    gate = threading.Event()
    gate.set()
    hits: list[str] = []
    real_try_acquire = journal.goalflight_task.FileLock.try_acquire

    def gated_try_acquire(
        _cls: type,
        path: Path,
        *,
        deadline_s: float,
        poll_s: float = 0.010,
    ):
        if gate.is_set():
            if not hits:
                hits.append("domain_write_lock")
            return None
        return real_try_acquire(path, deadline_s=deadline_s, poll_s=poll_s)

    monkeypatch.setattr(
        journal.goalflight_task.FileLock,
        "try_acquire",
        classmethod(gated_try_acquire),
    )
    cap = _LiveCapture(capsys)
    thread, results = _run_in_thread(
        [
            "listen",
            "--project-root",
            str(project),
            "--controller-label",
            lease.label,
            "--lease-nonce",
            lease.nonce,
            "--listener-slots",
            "1",
            "--json",
            "--poll-secs",
            "0.01",
            "--timeout-s",
            "60",
        ]
    )
    try:
        cap.await_stderr("listener degraded")
        assert hits == ["domain_write_lock"], "_domain_write retryable path did not bind"
        assert thread.is_alive()
        gate.clear()
        cap.await_stderr("listener recovered")
        _await_armed_listener(project, lease.label)
    finally:
        gate.clear()
        _release_lease(project, lease)
    thread.join(15.0)
    assert results == [3]


def test_listen_coverage_arm_exits_promptly_when_journal_vanishes(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Deletion after construction but before arm must bypass busy tolerance."""
    project, _env, lease = isolated
    monkeypatch.setattr(messages, "LISTENER_JOURNAL_TOLERANCE_S", 3600.0)
    _pin_listener_resolution(monkeypatch, lease)
    real_arm = journal.Journal.arm_listener
    hits: list[str] = []

    def vanish_then_arm(authority: journal.Journal, *args: object, **kwargs: object):
        if not hits:
            hits.append("arm_listener")
            authority.path.unlink()
        return real_arm(authority, *args, **kwargs)

    monkeypatch.setattr(journal.Journal, "arm_listener", vanish_then_arm)
    cap = _LiveCapture(capsys)
    thread, results = _run_in_thread(
        [
            "listen",
            "--project-root",
            str(project),
            "--controller-label",
            lease.label,
            "--lease-nonce",
            lease.nonce,
            "--listener-slots",
            "1",
            "--json",
            "--poll-secs",
            "0.01",
            "--timeout-s",
            "60",
        ]
    )
    thread.join(5.0)
    cap.pump()

    assert hits == ["arm_listener"]
    assert not thread.is_alive(), "vanished journal entered the 3600s busy window"
    assert results == [2]
    assert "journal-unavailable" in cap.stderr
    assert "listener degraded" not in cap.stderr


def test_listen_finishes_when_restored_journal_replaces_live_path(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    tmp_path: Path,
) -> None:
    """A validated restore must not strand the listener coverage row ARMED."""
    project, env, lease = isolated
    command = _backup_command(project, lease, timeout_s=60)
    listener = subprocess.Popen(
        command,
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    snapshot = tmp_path / "restored-journal.sqlite3"
    coverage = None
    try:
        _wait_for_waiter_kind(project, lease.label, "listener", listener.pid)

        def coverage_is_armed() -> bool:
            nonlocal coverage
            observed = journal.Journal(project).active_coverage(lease.label)
            if observed is None or observed["state"] != journal.COVERAGE_ARMED:
                return False
            coverage = observed
            return True

        wait_until(
            coverage_is_armed,
            timeout_s=15,
            interval_s=0.01,
            message="listener coverage arm",
        )
        assert coverage is not None
        path = journal.resolve_journal_path(project)
        with sqlite3.connect(path) as source, sqlite3.connect(snapshot) as target:
            source.backup(target)
        os.replace(snapshot, path)

        stdout, stderr = listener.communicate(timeout=15)
    finally:
        if listener.poll() is None:
            listener.terminate()
        if listener.poll() is None:
            listener.wait(timeout=5)

    assert listener.returncode == 2, (stdout, stderr)
    records = [
        json.loads(line)
        for line in stdout.decode().splitlines()
        if line.strip()
    ]
    exits = [record for record in records if record.get("kind") == "exit"]
    assert len(exits) == 1, (stdout, stderr)
    assert exits[0]["reason"] == "journal-unavailable", exits[0]
    assert "journal database was replaced" in exits[0]["detail"]
    restored = journal.Journal(project).coverage(str(coverage["coverage_id"]))
    assert restored is not None
    assert restored["state"] == journal.COVERAGE_EXITED
    assert restored["exit_reason"] == "journal-unavailable"


def test_listen_coverage_arm_keeps_journal_io_failure_fatal(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, _env, lease = isolated
    monkeypatch.setattr(messages, "LISTENER_JOURNAL_TOLERANCE_S", 3600.0)
    _pin_listener_resolution(monkeypatch, lease)
    hits: list[str] = []

    def fail_arm(*_args: object, **_kwargs: object):
        hits.append("arm_listener")
        raise journal.JournalIOError("injected arm path I/O failure")

    monkeypatch.setattr(journal.Journal, "arm_listener", fail_arm)
    cap = _LiveCapture(capsys)
    result = messages._run_cli(
        [
            "listen",
            "--project-root",
            str(project),
            "--controller-label",
            lease.label,
            "--lease-nonce",
            lease.nonce,
            "--listener-slots",
            "1",
            "--json",
            "--poll-secs",
            "0.01",
            "--timeout-s",
            "60",
        ]
    )
    cap.pump()

    assert result == 2
    assert hits == ["arm_listener"]
    assert "journal-io-failure" in cap.stderr
    assert "listener degraded" not in cap.stderr


def test_report_pending_one_shot_cursor_io_failure_is_fatal(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A recovered second peek must not hide the fatal arm-time failure."""
    project, _env, lease = isolated
    monkeypatch.setattr(messages, "LISTENER_JOURNAL_TOLERANCE_S", 3600.0)
    _pin_listener_resolution(monkeypatch, lease)
    real_peek = journal.Journal.cursor_peek
    calls: list[str] = []

    def fail_once(authority: journal.Journal, *args: object, **kwargs: object):
        calls.append("cursor_peek")
        if len(calls) == 1:
            raise journal.JournalIOError("one-shot arm snapshot I/O failure")
        return real_peek(authority, *args, **kwargs)

    monkeypatch.setattr(journal.Journal, "cursor_peek", fail_once)
    cap = _LiveCapture(capsys)
    result = messages._run_cli(
        [
            "listen",
            "--project-root",
            str(project),
            "--controller-label",
            lease.label,
            "--lease-nonce",
            lease.nonce,
            "--listener-slots",
            "1",
            "--report-pending",
            "--json",
            "--poll-secs",
            "0.01",
            "--timeout-s",
            "0.1",
        ]
    )
    cap.pump()

    assert result == 2
    assert calls == ["cursor_peek"], "listener retried after a fatal one-shot failure"
    assert "journal-io-failure" in cap.stderr
    assert "one-shot arm snapshot I/O failure" in cap.stderr
    assert "listener degraded" not in cap.stderr


def test_non_json_arm_materializes_attention_before_pending_claim(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Query busy cannot persist high-water before the human report exists."""
    project, _env, lease = isolated
    _materialize_synthetic_attention(project, lease)
    monkeypatch.setattr(messages, "LISTENER_JOURNAL_TOLERANCE_S", 30.0)
    gate = threading.Event()
    gate.set()
    hits = _gate_attention_reads(monkeypatch, gate)
    cap = _LiveCapture(capsys)
    thread, results = _run_in_thread(
        [
            "listen",
            "--project-root",
            str(project),
            "--controller-label",
            lease.label,
            "--lease-nonce",
            lease.nonce,
            "--listener-slots",
            "1",
            "--report-pending",
            "--poll-secs",
            "0.01",
            "--timeout-s",
            "60",
        ]
    )
    try:
        cap.await_stderr("listener degraded")
        assert "busy" in hits, "non-JSON arm attention query injection did not bind"
        assert thread.is_alive()
        assert wake.pending_report_high_water(
            project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
        ) is None, "a dying arm persisted an undelivered high-water"
        gate.clear()
        cap.await_stderr("listener recovered")
        cap.await_stdout("advance:")
        assert wake.pending_report_high_water(
            project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
        ) is not None
    finally:
        gate.clear()
        _release_lease(project, lease)
    thread.join(15.0)
    cap.pump()

    assert results == [3]
    assert "[controller_attention]" in cap.stdout
    assert hits.count("success") == 1


def test_non_json_ring_materializes_attention_before_ring_claim(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A consumed ring is impossible while buffered attention reads are busy."""
    project, env, lease = isolated
    _materialize_synthetic_attention(project, lease)
    monkeypatch.setattr(messages, "LISTENER_JOURNAL_TOLERANCE_S", 30.0)
    gate = threading.Event()
    hits = _gate_attention_reads(monkeypatch, gate)
    cap = _LiveCapture(capsys)
    thread, results = _run_in_thread(
        [
            "listen",
            "--project-root",
            str(project),
            "--controller-label",
            lease.label,
            "--lease-nonce",
            lease.nonce,
            "--listener-slots",
            "1",
            "--report-pending",
            "--poll-secs",
            "0.01",
            "--timeout-s",
            "60",
        ]
    )
    try:
        cap.await_stdout("advance:")
        gate.set()
        messages.post_message(
            dispatch_id="ring-after-attention",
            msg_type="controller-notice",
            payload={"text": "ring only after every envelope is ready"},
            messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
            source={"node": "peer", "adapter": "pytest", "transport": "controller"},
            addressee=messages.controller_addressee(
                lease.label,
                project_root=project,
            ),
        )
        cap.await_stderr("listener degraded")
        assert "busy" in hits, "non-JSON ring attention query injection did not bind"
        assert thread.is_alive()
        assert not wake._ring_stamp_path(
            project,
            controller_label=lease.label,
        ).exists(), "ring was consumed before human-readable envelopes materialized"
        gate.clear()
        cap.await_stderr("listener recovered")
        thread.join(15.0)
    finally:
        gate.clear()
        if thread.is_alive():
            _release_lease(project, lease)
            thread.join(15.0)
    cap.pump()

    assert results == [0]
    assert hits.count("success") == 2
    assert cap.stdout.count("advance:") == 2
    assert "ring-after-attention" in cap.stdout
    assert wake._ring_stamp_path(
        project,
        controller_label=lease.label,
    ).exists()


@pytest.mark.parametrize("busy_stage", ("connect", "query"))
@pytest.mark.parametrize("mode", ("poll", "gated"))
def test_follow_survives_transient_journal_busy_and_recovers(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    busy_stage: str,
    mode: str,
) -> None:
    """A busy spell must degrade the follower, never kill it."""
    project, _env, lease = isolated
    monkeypatch.setattr(messages, "_follow_stdout_refusal", lambda _stream: None)
    monkeypatch.setattr(
        messages, "LISTENER_JOURNAL_TOLERANCE_S", 30.0, raising=False
    )
    # Surface each busy connect fast; the loop-level window is what is under
    # test, not the per-operation budget.
    monkeypatch.setattr(
        messages, "LISTENER_JOURNAL_BUSY_BUDGET_S", 0.05, raising=False
    )
    monkeypatch.setenv(messages.WAKE_MODE_ENV, mode)
    gate = threading.Event()
    hits = (
        _gate_journal_connects(monkeypatch, project, gate)
        if busy_stage == "connect"
        else _gate_journal_queries(monkeypatch, project, gate)
    )
    if busy_stage == "connect":
        real_read_all = journal.Journal.read_all

        def reconnect_reader_before_read_all(authority, *args, **kwargs):
            if gate.is_set():
                connection = getattr(authority, "_reader_connection", None)
                if connection is not None:
                    connection.close()
                    authority._reader_connection = None
                    authority._reader_pid = None
            return real_read_all(authority, *args, **kwargs)

        monkeypatch.setattr(
            journal.Journal,
            "read_all",
            reconnect_reader_before_read_all,
        )
    cap = _LiveCapture(capsys)

    thread, results = _run_in_thread(_follow_argv(project, lease))
    try:
        cap.await_count(_is_heartbeat, 1)
        gate.set()
        cap.await_count(
            lambda record: _is_follow_event(record, "listener-degraded"), 1
        )
        assert hits, f"{busy_stage} busy injection did not bind"
        # The degradation notice itself proves the loop survived a busy
        # failure; before the fix this was a listener-fault record and exit 2.
        assert thread.is_alive()
        gate.clear()
        cap.await_count(
            lambda record: _is_follow_event(record, "listener-recovered"), 1
        )
        assert thread.is_alive()
        # Still polling after recovery: another heartbeat lands.
        cap.await_count(_is_heartbeat, cap.count(_is_heartbeat) + 1)
    finally:
        gate.clear()
        _release_lease(project, lease)
    thread.join(15.0)
    cap.pump()
    assert results == [3]  # stale-lease shutdown, not a fault exit
    assert (
        sum(
            1
            for record in cap.records
            if _is_follow_event(record, "listener-degraded")
        )
        == 1
    )
    assert (
        sum(
            1
            for record in cap.records
            if _is_follow_event(record, "listener-recovered")
        )
        == 1
    )
    faults = [
        record
        for record in cap.records
        if _is_follow_event(record, "listener-fault")
    ]
    assert not faults, cap.records


def test_follow_still_exits_when_journal_vanishes(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """JournalDisappeared stays fatal — tolerance must never spin on it."""
    project, _env, lease = isolated
    monkeypatch.setattr(messages, "_follow_stdout_refusal", lambda _stream: None)
    # A huge window proves disappearance bypasses tolerance entirely: the
    # follower must exit promptly anyway.
    monkeypatch.setattr(
        messages, "LISTENER_JOURNAL_TOLERANCE_S", 3600.0, raising=False
    )
    cap = _LiveCapture(capsys)

    thread, results = _run_in_thread(_follow_argv(project, lease))
    try:
        cap.await_count(_is_heartbeat, 1)
        journal.resolve_journal_path(project).unlink()
        thread.join(15.0)
    finally:
        if thread.is_alive():
            _release_lease(project, lease)
            thread.join(15.0)
    cap.pump()
    assert results == [2]
    faults = [
        record for record in cap.records if _is_follow_event(record, "listener-fault")
    ]
    assert faults and faults[-1]["payload"]["reason"] == "journal-unavailable"
    assert not any(
        _is_follow_event(record, "listener-degraded") for record in cap.records
    )


@pytest.mark.parametrize("busy_stage", ("connect", "query"))
def test_watchdog_survives_transient_journal_busy(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    busy_stage: str,
) -> None:
    """The follow watchdog died the same way in the incident; audit-fix it too."""
    project, _env, lease = isolated
    monkeypatch.setattr(messages, "_follow_stdout_refusal", lambda _stream: None)
    monkeypatch.setattr(
        messages, "LISTENER_JOURNAL_TOLERANCE_S", 30.0, raising=False
    )
    monkeypatch.setattr(
        messages, "LISTENER_JOURNAL_BUSY_BUDGET_S", 0.05, raising=False
    )
    gate = threading.Event()
    hits = (
        _gate_journal_connects(monkeypatch, project, gate)
        if busy_stage == "connect"
        else _gate_journal_queries(monkeypatch, project, gate)
    )
    cap = _LiveCapture(capsys)
    # A live follow stream keeps the watchdog from its follow-dead exit (0);
    # the only exit left to observe is the lease check.
    wake.activate_monitor_state(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        heartbeat_s=60.0,
        dead_after_s=180.0,
    )

    thread, results = _run_in_thread(
        [
            "listen",
            "--watch-follow",
            "--project-root",
            str(project),
            "--controller-label",
            lease.label,
            "--lease-nonce",
            lease.nonce,
            "--poll-secs",
            "0.01",
            "--timeout-s",
            "60",
        ]
    )
    try:
        _await_live_waiter(project, lease.label, wake.WATCHDOG_KIND)
        gate.set()
        cap.await_stderr("watchdog degraded")
        assert hits, f"{busy_stage} busy injection did not bind"
        assert thread.is_alive()
        gate.clear()
        cap.await_stderr("watchdog recovered")
        assert thread.is_alive()
    finally:
        gate.clear()
        _release_lease(project, lease)
    thread.join(15.0)
    cap.pump()
    assert results == [3]  # stale-lease shutdown, not a fault exit
    assert cap.stderr.count("watchdog degraded") == 1
    assert cap.stderr.count("watchdog recovered") == 1
    assert "watchdog runtime failed" not in cap.stderr


@pytest.mark.parametrize("busy_stage", ("connect", "query"))
@pytest.mark.parametrize("mode", ("poll", "gated"))
def test_listen_survives_transient_journal_busy(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    busy_stage: str,
    mode: str,
) -> None:
    """A doorbell may exit after a ring — never from a transient busy."""
    project, _env, lease = isolated
    monkeypatch.setattr(
        messages, "LISTENER_JOURNAL_TOLERANCE_S", 30.0, raising=False
    )
    monkeypatch.setattr(
        messages, "LISTENER_JOURNAL_BUSY_BUDGET_S", 0.05, raising=False
    )
    monkeypatch.setenv(messages.WAKE_MODE_ENV, mode)
    gate = threading.Event()
    hits = (
        _gate_journal_connects(monkeypatch, project, gate)
        if busy_stage == "connect"
        else _gate_journal_queries(monkeypatch, project, gate)
    )
    if busy_stage == "connect":
        real_data_version = journal.Journal._data_version

        def reconnect_reader_before_data_version(authority):
            if gate.is_set():
                connection = getattr(authority, "_reader_connection", None)
                if connection is not None:
                    connection.close()
                    authority._reader_connection = None
                    authority._reader_pid = None
            return real_data_version(authority)

        monkeypatch.setattr(
            journal.Journal,
            "_data_version",
            reconnect_reader_before_data_version,
        )
    cap = _LiveCapture(capsys)

    thread, results = _run_in_thread(
        [
            "listen",
            "--project-root",
            str(project),
            "--controller-label",
            lease.label,
            "--lease-nonce",
            lease.nonce,
            "--listener-slots",
            "1",
            "--json",
            "--poll-secs",
            "0.01",
            "--timeout-s",
            "60",
        ]
    )
    try:
        _await_armed_listener(project, lease.label)
        gate.set()
        cap.await_stderr("listener degraded")
        assert hits, f"{busy_stage} busy injection did not bind"
        assert thread.is_alive()
        gate.clear()
        cap.await_stderr("listener recovered")
        assert thread.is_alive()
    finally:
        gate.clear()
        _release_lease(project, lease)
    thread.join(15.0)
    cap.pump()
    assert results == [3]  # stale-lease/superseded shutdown, not a fault exit
    assert cap.stderr.count("listener degraded") == 1
    assert cap.stderr.count("listener recovered") == 1


def test_listen_emits_structured_exit_when_journal_disappears(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Terminal audit/enrichment failure must not swallow the JSON fault."""
    project, _env, lease = isolated
    cap = _LiveCapture(capsys)
    thread, results = _run_in_thread(
        [
            "listen",
            "--project-root",
            str(project),
            "--controller-label",
            lease.label,
            "--lease-nonce",
            lease.nonce,
            "--listener-slots",
            "1",
            "--json",
            "--poll-secs",
            "0.01",
            "--timeout-s",
            "60",
        ]
    )
    _await_armed_listener(project, lease.label)
    journal.resolve_journal_path(project).unlink()
    thread.join(15.0)
    cap.pump()

    assert results == [2]
    exits = [record for record in cap.records if record.get("kind") == "exit"]
    assert exits, f"structured exit was lost: stderr={cap.stderr!r}"
    assert exits[-1]["reason"] == "journal-unavailable"
    assert "coverage_exit_error" in exits[-1]
    assert "rearm_error" in exits[-1]


def test_idle_listener_pass_cpu_stays_small_with_large_backlog(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    tmp_path: Path,
) -> None:
    project, env, lease = isolated
    _post_pending_backlog(project, env, lease, prefix="idle-backlog")

    for mode in ("listen-report", "listen", "follow", "watch-follow"):
        if mode == "follow":
            follow_cursor_version = journal.open_or_create_journal(project).cursor_peek(
                lease.label,
                nonce=lease.nonce,
                limit=1000,
            ).cursor_version
            command = _follow_command(project, lease, heartbeat_s=60, poll_s=0.01)
            waiter_kind = wake.MONITOR_KIND
        elif mode == "watch-follow":
            wake.activate_monitor_state(
                project,
                controller_label=lease.label,
                lease_nonce=lease.nonce,
                heartbeat_s=60,
                dead_after_s=300,
            )
            command = _watch_command(project, lease, timeout_s=60)
            waiter_kind = wake.WATCHDOG_KIND
        else:
            command = [
                sys.executable,
                str(SCRIPTS / "goalflight_messages.py"),
                "listen",
                "--project-root",
                str(project),
                "--controller-label",
                lease.label,
                "--lease-nonce",
                lease.nonce,
                "--listener-slots",
                "1",
                "--poll-secs",
                "0.01",
                "--timeout-s",
                "60",
            ]
            if mode == "listen-report":
                command.append("--report-pending")
            waiter_kind = "listener"

        samples_path = tmp_path / f"{mode}.cpu"
        extra_setup: tuple[str, ...] = ()
        if mode == "follow":
            ready = tmp_path / "follow-ring-ready"
            external_claim = tmp_path / "external-ring-claimed"
            claim_result = tmp_path / "follow-ring-result"
            extra_setup = (
                "from pathlib import Path",
                "import goalflight_wake",
                f"ready_path = {str(ready)!r}",
                f"claim_path = {str(external_claim)!r}",
                f"result_path = {str(claim_result)!r}",
                "original_claim_ring = goalflight_wake.claim_ring",
                "first_claim = True",
                "def wait_for_external_claim(*args, **kwargs):",
                "    global first_claim",
                "    if first_claim:",
                "        first_claim = False",
                "        Path(ready_path).write_text('ready')",
                "        deadline = time.monotonic() + 30",
                "        while not Path(claim_path).exists() and time.monotonic() < deadline:",
                "            time.sleep(0.01)",
                "        if not Path(claim_path).exists():",
                "            raise TimeoutError('test did not reserve the competing ring')",
                "        claimed = original_claim_ring(*args, **kwargs)",
                "        result_temp = Path(result_path + '.tmp')",
                "        result_temp.write_text(str(claimed), encoding='utf-8')",
                "        result_temp.replace(result_path)",
                "        return claimed",
                "    return original_claim_ring(*args, **kwargs)",
                "goalflight_wake.claim_ring = wait_for_external_claim",
            )
        materialization_count_path = tmp_path / f"{mode}.materializations"
        bootstrap = _cpu_sampled_bootstrap(
            tmp_path,
            f"{mode}-listener",
            command,
            samples_path,
            extra_setup=extra_setup,
            materialization_counter_path=materialization_count_path,
        )
        child_env = {**env, "GOALFLIGHT_WAKE_MODE": "poll"}
        captured_stdout = bytearray()
        proc = subprocess.Popen(
            [sys.executable, str(bootstrap)],
            cwd=project,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=(tmp_path / f"{mode}.stderr").open("wb"),
        )
        assert proc.stdout is not None

        def drain_stdout() -> None:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    return
                captured_stdout.extend(chunk)

        output_thread = threading.Thread(target=drain_stdout, daemon=True)
        output_thread.start()
        try:
            _wait_for_waiter_kind(
                project,
                lease.label,
                waiter_kind,
                proc.pid,
                timeout_s=15,
            )
            sample_floor = 0
            if mode != "follow":
                warmup_deadline = time.monotonic() + 30
                while time.monotonic() < warmup_deadline:
                    try:
                        if samples_path.read_text(encoding="utf-8").splitlines():
                            break
                    except FileNotFoundError:
                        pass
                    time.sleep(0.02)
                else:
                    raise AssertionError(f"{mode} did not complete its first pass")
            materialization_floor = _materialization_count(materialization_count_path)
            if mode == "follow":
                assert wait_until(ready.exists, timeout_s=10, interval_s=0.01)
                assert wake.claim_ring(
                    project,
                    controller_label=lease.label,
                    cursor_version=follow_cursor_version,
                )
                external_claim.touch()
                assert wait_until(claim_result.exists, timeout_s=10, interval_s=0.01)
                assert claim_result.read_text(encoding="utf-8") == "False"
                materialization_floor = _materialization_count(
                    materialization_count_path
                )
                sample_floor = (
                    len(samples_path.read_text().splitlines())
                    if samples_path.exists()
                    else 0
                )
            deadline = time.monotonic() + 30
            samples: list[float] = []
            while time.monotonic() < deadline:
                try:
                    samples = [float(line) for line in samples_path.read_text().splitlines()]
                except FileNotFoundError:
                    samples = []
                if len(samples) >= sample_floor + 8:
                    break
                time.sleep(0.02)
            assert len(samples) >= sample_floor + 8, (
                f"{mode} did not record enough passes: {samples}"
            )
            assert max(samples[-5:]) < 0.1, f"{mode} idle pass CPU spiked: {samples[-5:]}"
            assert _materialization_count(materialization_count_path) == materialization_floor, (
                f"{mode} rematerialized an unchanged candidate"
            )
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
            output_thread.join(timeout=2)
        if mode == "listen-report":
            assert b"pending backlog: count=250 oldest_age_s=" in captured_stdout


def test_contending_report_pending_listeners_retry_cached_snapshot(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    tmp_path: Path,
) -> None:
    project, env, lease = isolated
    _post_pending_backlog(project, env, lease, prefix="contention-backlog")

    processes: list[subprocess.Popen[bytes]] = []
    output_threads: list[threading.Thread] = []
    stderr_handles = []
    samples_paths: list[Path] = []
    materialization_paths: list[Path] = []
    claim_lost_paths: list[Path] = []
    stdout_buffers: list[bytearray] = []
    try:
        for index in range(2):
            samples_path = tmp_path / f"contender-{index}.cpu"
            samples_paths.append(samples_path)
            materialization_path = tmp_path / f"contender-{index}.materializations"
            materialization_paths.append(materialization_path)
            claim_lost_path = tmp_path / f"contender-{index}.claim-lost"
            claim_lost_paths.append(claim_lost_path)
            retry_pause_requested = tmp_path / f"contender-{index}.pause-requested"
            retry_pause_ready = tmp_path / f"contender-{index}.pause-ready"
            retry_pause_release = tmp_path / f"contender-{index}.pause-release"
            command = [
                sys.executable,
                str(SCRIPTS / "goalflight_messages.py"),
                "listen",
                "--project-root",
                str(project),
                "--controller-label",
                lease.label,
                "--lease-nonce",
                lease.nonce,
                "--listener-slots",
                "2",
                "--report-pending",
                "--json",
                "--poll-secs",
                "0.01",
                "--timeout-s",
                "60",
            ]
            extra_setup = (
                "import goalflight_wake",
                f"claim_lost_path = Path({str(claim_lost_path)!r})",
                "original_claim_ring = goalflight_wake.claim_ring",
                "def record_claim_loss(*args, **kwargs):",
                "    claimed = original_claim_ring(*args, **kwargs)",
                "    if not claimed:",
                "        temp = claim_lost_path.with_suffix('.tmp')",
                "        temp.write_text('lost', encoding='utf-8')",
                "        temp.replace(claim_lost_path)",
                "    return claimed",
                "goalflight_wake.claim_ring = record_claim_loss",
                f"retry_pause_requested = Path({str(retry_pause_requested)!r})",
                f"retry_pause_ready = Path({str(retry_pause_ready)!r})",
                f"retry_pause_release = Path({str(retry_pause_release)!r})",
                "pause_base_sleep = time.sleep",
                "def pause_at_retry_boundary(seconds):",
                "    if retry_pause_requested.exists() and not retry_pause_ready.exists():",
                "        temp = retry_pause_ready.with_suffix('.tmp')",
                "        temp.write_text('ready', encoding='utf-8')",
                "        temp.replace(retry_pause_ready)",
                "        deadline = time.monotonic() + 45",
                "        while not retry_pause_release.exists() and time.monotonic() < deadline:",
                "            pause_base_sleep(0.01)",
                "        if not retry_pause_release.exists():",
                "            raise TimeoutError('test did not release the ring retry pause')",
                "    return pause_base_sleep(seconds)",
                "time.sleep = pause_at_retry_boundary",
            )
            bootstrap = _cpu_sampled_bootstrap(
                tmp_path,
                f"contender-{index}",
                command,
                samples_path,
                extra_setup=extra_setup,
                materialization_counter_path=materialization_path,
            )
            stderr_handle = (tmp_path / f"contender-{index}.stderr").open("wb")
            stderr_handles.append(stderr_handle)
            proc = subprocess.Popen(
                [sys.executable, str(bootstrap)],
                cwd=project,
                env={**env, "GOALFLIGHT_WAKE_MODE": "poll"},
                stdout=subprocess.PIPE,
                stderr=stderr_handle,
            )
            processes.append(proc)
            assert proc.stdout is not None

            stdout_buffer = bytearray()
            stdout_buffers.append(stdout_buffer)

            def drain_stdout(stream=proc.stdout, buffer=stdout_buffer) -> None:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        return
                    buffer.extend(chunk)

            output_thread = threading.Thread(target=drain_stdout, daemon=True)
            output_thread.start()
            output_threads.append(output_thread)
            _wait_for_waiter_kind(
                project, lease.label, "listener", proc.pid, timeout_s=15
            )

        deadline = time.monotonic() + 30
        counts_before: list[int] = []
        while time.monotonic() < deadline:
            counts_before = [
                len(path.read_text().splitlines()) if path.exists() else 0
                for path in samples_paths
            ]
            if min(counts_before) >= 8:
                break
            time.sleep(0.02)
        assert min(counts_before) >= 8, (
            f"listeners did not settle before contention: {counts_before}; "
            f"polls={[proc.poll() for proc in processes]}; "
            f"stderr={[path.read_bytes() for path in (tmp_path / f'contender-{i}.stderr' for i in range(2))]}"
        )

        messages.post_message(
            dispatch_id="contention-trigger",
            msg_type="controller-notice",
            payload={"text": "force the listeners to contend for one cursor ring"},
            messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
            source={"node": "peer", "adapter": "pytest", "transport": "controller"},
            addressee=messages.controller_addressee(lease.label, project_root=project),
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not any(
            proc.poll() is not None for proc in processes
        ):
            time.sleep(0.02)
        winners = [proc for proc in processes if proc.poll() is not None]
        assert len(winners) == 1, [proc.poll() for proc in processes]
        winner = winners[0]
        assert winner.returncode == 0
        loser_index = next(index for index, proc in enumerate(processes) if proc is not winner)
        loser = processes[loser_index]
        assert wait_until(
            claim_lost_paths[loser_index].exists, timeout_s=30, interval_s=0.01
        )
        deadline = time.monotonic() + 30
        samples: list[float] = []
        while time.monotonic() < deadline:
            try:
                samples = [float(line) for line in samples_paths[loser_index].read_text().splitlines()]
            except FileNotFoundError:
                samples = []
            if len(samples) >= counts_before[loser_index] + 8:
                break
            time.sleep(0.02)
        assert len(samples) >= counts_before[loser_index] + 8, samples
        assert max(samples[-5:]) < 0.1, f"ring loser rescanned the backlog: {samples[-5:]}"
        # The winner's exit changes journal coverage and pending-report
        # ownership. Let those transitions settle, then require eight full idle
        # passes with no further materialization.
        observed_materializations = _materialization_count(
            materialization_paths[loser_index]
        )
        stable_pass_floor = len(samples)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                samples = [
                    float(line)
                    for line in samples_paths[loser_index].read_text().splitlines()
                ]
            except FileNotFoundError:
                samples = []
            current_materializations = _materialization_count(
                materialization_paths[loser_index]
            )
            if current_materializations != observed_materializations:
                observed_materializations = current_materializations
                stable_pass_floor = len(samples)
            if len(samples) >= stable_pass_floor + 8:
                break
            time.sleep(0.02)
        assert len(samples) >= stable_pass_floor + 8, samples
        assert max(samples[-5:]) < 0.1, f"ring loser rescanned the backlog: {samples[-5:]}"
        assert loser.poll() is None
        (tmp_path / f"contender-{loser_index}.pause-requested").touch()
        assert wait_until(
            (tmp_path / f"contender-{loser_index}.pause-ready").exists,
            timeout_s=30,
            interval_s=0.01,
        )
        assert _materialization_count(materialization_paths[loser_index]) == (
            observed_materializations
        ), "ring loser rematerialized an unchanged candidate"

        messages.post_message(
            dispatch_id="contention-between-retries",
            msg_type="controller-notice",
            payload={"text": "change the journal while the loser has a cached candidate"},
            messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
            source={"node": "peer", "adapter": "pytest", "transport": "controller"},
            addressee=messages.controller_addressee(lease.label, project_root=project),
        )
        refreshed = journal.open_or_create_journal(project).cursor_peek(
            lease.label, nonce=lease.nonce, limit=1000
        )
        new_stream = "contention-between-retries"
        new_rows = [
            row for row in refreshed.items if str(row.get("stream_id") or "") == new_stream
        ]
        assert len(new_rows) == 1, new_rows
        new_seq = int(new_rows[0]["stream_seq"])
        (tmp_path / f"contender-{loser_index}.pause-release").touch()

        try:
            loser.wait(timeout=30)
        except subprocess.TimeoutExpired:
            raise AssertionError("ring loser did not deliver the journal change")
        output_threads[loser_index].join(timeout=2)
        assert _materialization_count(materialization_paths[loser_index]) > (
            observed_materializations
        ), "journal change did not refresh the cached candidate"
        output_lines = [
            json.loads(line)
            for line in stdout_buffers[loser_index].decode("utf-8").splitlines()
            if line
        ]
        rings = [line for line in output_lines if line.get("kind") == "ring"]
        assert len(rings) == 1, output_lines
        ring = rings[0]
        command_args = shlex.split(ring["advance_command"])
        position_index = command_args.index("--position")
        position_args = [
            value
            for value in command_args[position_index + 1 :]
            if not value.startswith("--")
        ]
        assert sum(value.startswith(f"{new_stream}=") for value in position_args) == 1
        assert f"{new_stream}={new_seq}" in position_args, position_args
        advanced = subprocess.run(
            command_args,
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert advanced.returncode == 0, advanced.stderr
        after_advance = journal.open_or_create_journal(project).cursor_peek(
            lease.label, nonce=lease.nonce, limit=1000
        )
        assert not any(
            str(row.get("stream_id") or "") == new_stream
            for row in after_advance.items
        )
    finally:
        for index in range(2):
            (tmp_path / f"contender-{index}.pause-release").touch()
        for proc in processes:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in processes:
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        for thread in output_threads:
            thread.join(timeout=2)
        for handle in stderr_handles:
            handle.close()


@pytest.mark.parametrize("signal_name", ["SIGTERM", "SIGHUP"])
def test_follow_signal_deadline_covers_slow_notifier_close(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    tmp_path: Path,
    signal_name: str,
) -> None:
    project, env, lease = isolated
    signum = getattr(signal, signal_name)
    marker = tmp_path / f"notifier-close-started-{signal_name}"
    handlers_ready = tmp_path / f"handlers-installed-{signal_name}"
    bootstrap = tmp_path / "slow-notifier-close.py"
    bootstrap.write_text(
        "\n".join(
            [
                "import sys, time",
                "from pathlib import Path",
                f"sys.path.insert(0, {str(SCRIPTS)!r})",
                "import goalflight_messages",
                "original_install = goalflight_messages._ListenerDeathWatch.install",
                "def install_and_mark_ready(watch):",
                "    original_install(watch)",
                "    marker = Path(__import__('os').environ['HANDLERS_INSTALLED_MARKER'])",
                "    temp = marker.with_suffix('.tmp')",
                "    temp.write_text('installed', encoding='utf-8')",
                "    temp.replace(marker)",
                "goalflight_messages._ListenerDeathWatch.install = install_and_mark_ready",
                "class SlowNotifier:",
                "    active = False",
                "    def close(self):",
                "        Path(__import__('os').environ['SLOW_CLOSE_MARKER']).write_text('started')",
                "        time.sleep(10)",
                "goalflight_messages._new_journal_notifier = lambda *_args: SlowNotifier()",
                "raise SystemExit(goalflight_messages.main(sys.argv[1:]))",
            ]
        ),
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            str(bootstrap),
            *_follow_command(project, lease, heartbeat_s=60, poll_s=0.01)[2:],
        ],
        cwd=project,
        env={
            **env,
            "HANDLERS_INSTALLED_MARKER": str(handlers_ready),
            "SLOW_CLOSE_MARKER": str(marker),
            "GOALFLIGHT_WAKE_MODE": "poll",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_waiter_kind(project, lease.label, wake.MONITOR_KIND, proc.pid, timeout_s=10)
        assert wait_until(handlers_ready.exists, timeout_s=10, interval_s=0.01)
        started = time.monotonic()
        proc.send_signal(signum)
        assert wait_until(marker.exists, timeout_s=3, interval_s=0.01)
        assert wake.live_waiters(
            project, controller_label=lease.label, kinds={wake.MONITOR_KIND}
        ) == []
        try:
            stdout, stderr = proc.communicate(
                timeout=messages.LISTENER_SHUTDOWN_BOUND_S + 1
            )
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate(timeout=2)
            raise AssertionError(
                f"follow exceeded shutdown bound stdout={stdout!r} stderr={stderr!r}"
            )
        assert proc.returncode == messages.listener_posix_signal_exit_code(signum)
        assert time.monotonic() - started <= messages.LISTENER_SHUTDOWN_BOUND_S + 1
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
