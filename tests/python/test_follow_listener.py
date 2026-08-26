"""t-292: persistent stdout lines are live wakes, not exit-buffered output."""

from __future__ import annotations

from contextlib import ExitStack
import errno
import json
import os
from pathlib import Path
import select
import signal
import sqlite3
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
    for key, value in env.items():
        if key.startswith("GOAL") or key == "PYTHONUNBUFFERED":
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
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        _follow_command(
            project,
            lease,
            heartbeat_s=heartbeat_s,
            poll_s=poll_s,
        ),
        cwd=project,
        env=env,
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


def _wait_for_monitor_slot(project: Path, label: str, pid: int) -> None:
    _wait_for_waiter_kind(project, label, wake.MONITOR_KIND, pid)


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
    alive, emitted = messages._emit_claimed_follow_events(
        project,
        controller_label=lease.label,
        cursor_version=77,
        visible=visible,
        emit=lambda _record: False,
    )
    assert (alive, emitted) == (False, False)
    assert wake.claim_ring(
        project,
        controller_label=lease.label,
        cursor_version=77,
    ), "the replacement listener must be able to deliver the same cursor"


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


def test_listener_survives_present_journal_open_failure_and_times_out(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, _env, lease = isolated
    real_connect = journal.sqlite3.connect
    failed_opens = 0

    def fail_first_rw_open(database: object, *args: object, **kwargs: object):
        nonlocal failed_opens
        if "?mode=rw" in str(database) and failed_opens == 0:
            failed_opens += 1
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(journal.sqlite3, "connect", fail_first_rw_open)
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


def test_every_record_is_structural_and_below_pipe_buf_with_long_frontier() -> None:
    frontier = messages._follow_frontier_record(
        {
            "id": "t-very-long-frontier",
            "title": "regolith 🚀 " * 10_000,
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
            rearm_command="python3 goalflight_messages.py follow " + "x" * 10_000,
        ),
        messages._watchdog_dead_record(
            {
                "live_waiters": 0,
                "target_waiters": 3,
                "missing_components": ["stream", "backup", "watchdog"],
            },
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


def test_frontier_reads_only_materialized_projection_and_marks_stale(
    tmp_path: Path,
) -> None:
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
    working = messages._follow_frontier_snapshot(store)
    assert working["payload"]["id"] == "t-working"
    assert working["payload"]["state"] == "working"
    assert working["payload"]["title"] == "worker remains in flight"

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
    decision = messages._follow_frontier_snapshot(store)
    assert decision["payload"]["id"] == "q-decision"
    assert decision["payload"]["state"] == "decision"

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
                slots=1,
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
    assert context["listener_live"] == 1
    assert context["listener_target"] == wake.persistent_wake_target()


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
        _backup_command(project, lease),
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
            slots=1,
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
        _backup_command(project, lease),
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
        _backup_command(project, lease),
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
        _backup_command(project, lease),
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


# --- b-214: a transient journal-busy must not kill a persistent listener ---
#
# The busy condition is injected deterministically by gating sqlite3.connect on
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
    """While `gate` is set, every fresh connect to this journal reports busy."""
    journal_uri = journal.resolve_journal_path(project).as_uri()
    real_connect = journal.sqlite3.connect
    hits: list[str] = []

    def gated_connect(database: object, *args: object, **kwargs: object):
        if gate.is_set() and str(database).startswith(journal_uri):
            hits.append(str(database))
            raise sqlite3.OperationalError("database is locked")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(journal.sqlite3, "connect", gated_connect)
    return hits


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
def test_follow_survives_transient_journal_busy_and_recovers(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    busy_stage: str,
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
    gate = threading.Event()
    hits = (
        _gate_journal_connects(monkeypatch, project, gate)
        if busy_stage == "connect"
        else _gate_journal_queries(monkeypatch, project, gate)
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
def test_listen_survives_transient_journal_busy(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    busy_stage: str,
) -> None:
    """A doorbell may exit after a ring — never from a transient busy."""
    project, _env, lease = isolated
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
