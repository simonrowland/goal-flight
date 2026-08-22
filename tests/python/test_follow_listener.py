"""t-292: persistent stdout lines are live wakes, not exit-buffered output."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest


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
    for key in (
        "GOALFLIGHT_DISPATCH_ID",
        "GOALFLIGHT_PROMPT_FILE",
        "GOALFLIGHT_STEER_FILE",
        "GOALFLIGHT_CONTROLLER_SESSION_ID",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE",
        "GOALFLIGHT_CONTROLLER_PID",
        "GOALFLIGHT_LISTENER_SLOTS",
        "GOALFLIGHT_LISTENER_LOW_WATER",
    ):
        env.pop(key, None)
        monkeypatch.delenv(key, raising=False)
    env.update(
        {
            "GOALFLIGHT_MESSAGES_DIR": str(tmp_path / "messages"),
            "GOALFLIGHT_FLEET_DIR": str(tmp_path / "fleet"),
            "GOALFLIGHT_JOURNAL_DIR": str(tmp_path / "journals"),
            "GOALFLIGHT_TASK_STORE_DIR": str(tmp_path / "task-store"),
            "GOALFLIGHT_STATE_DIR": str(tmp_path / "state"),
            "GOALFLIGHT_DISPATCH_DIR": str(tmp_path / "state" / "dispatch"),
            "GOALFLIGHT_WAKE_LEDGER_DIR": str(tmp_path / "wake-ledger"),
            "GOAL_FLIGHT_PIDFILE_DIR": str(tmp_path / "pids"),
            "GOALFLIGHT_CAPACITY_CONF": os.devnull,
            "GOALFLIGHT_ROOT": str(ROOT),
            "GOALFLIGHT_CONTROLLER_LABEL": label,
            "GOALFLIGHT_PROCESS_ROLE": "controller",
            "GOALFLIGHT_TEST_MODE": "1",
            "GOALFLIGHT_WAKE_ENTRY_POLL_S": "0",
        }
    )
    for key, value in env.items():
        if key.startswith("GOAL"):
            monkeypatch.setenv(key, value)
    project = tmp_path / "project"
    project.mkdir()
    authority = journal.open_or_create_journal(project)
    claimed = authority.claim_or_renew_lease(
        label,
        principal={"principal_id": "follow-test-principal"},
    )
    assert claimed.committed and claimed.value is not None
    return project, env, claimed.value


def _follow_command(
    project: Path,
    lease: journal.LeaseIdentity,
    *,
    heartbeat_s: float,
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
        "0.01",
        "--heartbeat-secs",
        str(heartbeat_s),
        "--frontier-floor-secs",
        str(heartbeat_s * 20),
    ]


class _JsonLineReader:
    def __init__(self, stream) -> None:
        self.stream = stream
        self.buffer = b""

    def read(self, timeout_s: float = 2.0) -> tuple[bytes, dict[str, object]]:
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
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        _follow_command(project, lease, heartbeat_s=heartbeat_s),
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _watch_command(project: Path, lease: journal.LeaseIdentity) -> list[str]:
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
        "--watch-follow",
        "--json",
        "--poll-secs",
        "0.01",
        "--timeout-s",
        "1",
    ]


def _wait_for_monitor_slot(project: Path, label: str, pid: int) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        waiters = wake.live_waiters(
            project,
            controller_label=label,
            kinds={wake.MONITOR_KIND},
        ) or []
        if [row.pid for row in waiters] == [pid]:
            return
        time.sleep(0.01)
    raise AssertionError(f"persistent monitor slot for pid={pid} never appeared")


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


def test_journal_failure_is_a_waking_stdout_record(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, _env, lease = isolated
    monkeypatch.setattr(messages, "_follow_stdout_refusal", lambda _stream: None)
    monkeypatch.setattr(
        journal.Journal,
        "cursor_peek",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            journal.JournalError("measured journal fault")
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
    records = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    assert records[-1]["kind"] == "event"
    assert records[-1]["payload"]["type"] == "listener-fault"
    assert records[-1]["payload"]["reason"] == "journal-unavailable"
    assert not wake.live_waiters(
        project,
        controller_label=lease.label,
        kinds={wake.MONITOR_KIND},
    )


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
        assert stream_only["wake_mode"] == "persistent"
        assert stream_only["live_waiters"] == 1
        assert stream_only["target_waiters"] == 2
        assert stream_only["missing_components"] == ["backup"]
        assert stream_only["portable_live_waiters"] == 0
        assert stream_only["portable_target_waiters"] == 1
        claim_depth = sessions._listener_depth_after_claim(
            project,
            lease.label,
            lease.nonce,
        )
        assert claim_depth is not None
        assert claim_depth["live"] == 1
        assert claim_depth["target"] == 2
        assert claim_depth["missing"] == 1
        assert claim_depth["missing_components"] == ["backup"]
        assert "--watch-follow" in claim_depth["commands"][0]
        claim_hint_plan = {**claim_depth, "work_in_flight": True}
        assert "own tracked background task" in wake.coverage_rearm_hint(
            claim_hint_plan
        )

        with wake.register_listener_waiter(
            project,
            controller_label=lease.label,
            generation_key=lease.nonce,
            slots=1,
        ):
            with_backup = wake.coverage_status(
                project,
                controller_label=lease.label,
                lease_nonce=lease.nonce,
            )
            assert with_backup["live_waiters"] == with_backup["target_waiters"] == 2
            assert with_backup["portable_live_waiters"] == 1

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
        assert stream_gone["target_waiters"] == 2
        assert stream_gone["missing_components"] == ["stream"]
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
    assert context["listener_target"] == 2


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
        kinds={"listener"},
    ) == []


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
