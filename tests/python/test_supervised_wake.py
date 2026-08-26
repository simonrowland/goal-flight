"""t-323: one supervised wake feed owns the pool and re-arms children."""

from __future__ import annotations

import ctypes
from collections.abc import Callable
from dataclasses import dataclass, field
import errno
import json
import os
from pathlib import Path
import select
import shlex
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from machine_isolation import AMBIENT_IDENTITY_ENV, isolated_machine_env


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_session_status as sessions  # noqa: E402
import goalflight_task  # noqa: E402
import goalflight_wake as wake  # noqa: E402
import goalflight_wake_supervise as supervise  # noqa: E402
import goalflight_task as task  # noqa: E402


@dataclass
class PlannedExit:
    lifetime_s: float
    returncode: int
    output: str = ""
    armed: bool = False
    stdout_lines: list[tuple[float, str]] = field(default_factory=list)


@dataclass
class FakeChild:
    name: str
    kind: str
    command: str
    pid: int
    started_at: float
    exit_at: float | None
    returncode: int | None
    output: str
    armed: bool
    will_arm: bool
    stdout_lines: list[tuple[float, str]]
    emitted_through: float = -1.0
    alive: bool = True


@dataclass
class FakeHost:
    nonce: str = "nonce-1"
    lease_nonce: str = "nonce-1"
    now: float = 0.0
    lines: list[str] = field(default_factory=list)
    spawns: list[tuple[str, str]] = field(default_factory=list)
    scripts: dict[str, list[PlannedExit]] = field(default_factory=dict)
    children: list[FakeChild] = field(default_factory=list)
    next_pid: int = 7000
    stop: bool = False
    stop_after_spawns: int | None = None
    stop_on_restart_reason: str | None = None
    stop_on_stop_reason: str | None = None
    stop_when_lines_contain: tuple[str, ...] = ()
    stop_after_coverage: int | None = None
    stop_after_waits: int | None = None
    peer_gone_after_checks: int | None = None
    fail_write_type: str | None = None
    stop_signum: int | None = None
    nonce_state: str = "live"
    alive_by_kind_at_stop: dict[str, bool] = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)
    _coverage_seen: int = 0
    _waits: int = 0
    _peer_checks: int = 0
    _stdout_detector: supervise._PeerLossDetector = field(
        default_factory=supervise._PeerLossDetector
    )

    def running(self) -> bool:
        return not self.stop

    def live_nonce(self) -> str | None:
        if self.nonce_state == "unreadable":
            return None
        return self.nonce

    def nonce_probe(self) -> str:
        if self.nonce_state == "unreadable":
            return "unreadable"
        if not self.nonce or str(self.nonce) != str(self.lease_nonce):
            return "dead"
        return "live"

    def write_stdout(self, line: str) -> bool:
        text = line if line.endswith("\n") else line + "\n"
        self.lines.append(text)
        try:
            record_type = str(json.loads(text).get("type") or "line")
        except (AttributeError, json.JSONDecodeError):
            record_type = "line"
        self.actions.append(f"write:{record_type}")
        if record_type == self.fail_write_type:
            return False
        if '"type":"coverage"' in text:
            self._coverage_seen += 1
            if (
                self.stop_after_coverage is not None
                and self._coverage_seen >= self.stop_after_coverage
            ):
                self.stop = True
        if self.stop_on_restart_reason and '"type":"restart"' in text:
            record = json.loads(text)
            if record.get("reason") == self.stop_on_restart_reason:
                self.stop = True
        if '"type":"stop"' in text:
            record = json.loads(text)
            self.alive_by_kind_at_stop = {
                child.kind: child.alive for child in self.children
            }
            if (
                self.stop_on_stop_reason
                and record.get("reason") == self.stop_on_stop_reason
            ):
                self.stop = True
        if self.stop_when_lines_contain:
            joined = "".join(self.lines)
            if all(part in joined for part in self.stop_when_lines_contain):
                self.stop = True
        return True

    def stdio_peer_gone(self) -> bool:
        self._peer_checks += 1
        return (
            self.peer_gone_after_checks is not None
            and self._peer_checks >= self.peer_gone_after_checks
        )

    def report_stdout_detector(
        self, source: str, outcome: str, detail: str = "", error: str = ""
    ) -> None:
        self._stdout_detector.report(source, outcome, detail, error)

    def stdout_detector_status(self) -> supervise._DetectorStatus:
        return self._stdout_detector.status()

    def spawn(self, kind: str, command: str) -> FakeChild:
        self.spawns.append((kind, command))
        used = sum(1 for spawned_kind, _command in self.spawns if spawned_kind == kind)
        plans = self.scripts.get(kind, [])
        plan = plans[used - 1] if used - 1 < len(plans) else None
        pid = self.next_pid
        self.next_pid += 1
        if plan is None:
            child = FakeChild(
                name=f"{kind}:{pid}",
                kind=kind,
                command=command,
                pid=pid,
                started_at=self.now,
                exit_at=None,
                returncode=None,
                output="",
                armed=False,
                will_arm=True,
                stdout_lines=[],
            )
        else:
            child = FakeChild(
                name=f"{kind}:{pid}",
                kind=kind,
                command=command,
                pid=pid,
                started_at=self.now,
                exit_at=self.now + plan.lifetime_s,
                returncode=plan.returncode,
                output=plan.output,
                armed=False,
                will_arm=plan.armed,
                stdout_lines=list(plan.stdout_lines),
            )
        self.children.append(child)
        if (
            self.stop_after_spawns is not None
            and len(self.spawns) >= self.stop_after_spawns
        ):
            self.stop = True
        return child

    def wait(
        self,
        children: list[FakeChild],
        timeout_s: float,
    ) -> supervise.WaitResult:
        self._waits += 1
        deadline = self.now + max(0.0, timeout_s)
        for child in children:
            if child.alive and child.will_arm:
                child.armed = True
        times: list[float] = []
        for child in children:
            if not child.alive:
                continue
            for offset, _line in child.stdout_lines:
                at = child.started_at + offset
                if at > child.emitted_through and at <= deadline:
                    times.append(at)
            if child.exit_at is not None and child.exit_at <= deadline:
                times.append(child.exit_at)
        if not times:
            self.now = deadline
            if (
                self.stop_after_waits is not None
                and self._waits >= self.stop_after_waits
            ):
                self.stop = True
            return supervise.WaitResult(lines=[], exits=[])
        self.now = min(times)
        lines: list[tuple[FakeChild, str]] = []
        for child in children:
            if not child.alive:
                continue
            for offset, line in child.stdout_lines:
                at = child.started_at + offset
                if child.emitted_through < at <= self.now:
                    lines.append((child, line))
                    child.emitted_through = at
        exits: list[supervise.ChildExit] = []
        for child in children:
            if (
                child.alive
                and child.exit_at is not None
                and child.exit_at <= self.now
            ):
                child.alive = False
                exits.append(
                    supervise.ChildExit(
                        child=child,
                        returncode=int(child.returncode or 0),
                        output=child.output,
                        armed=child.armed,
                        ran_s=max(0.0, self.now - child.started_at),
                    )
                )
        if (
            self.stop_after_waits is not None
            and self._waits >= self.stop_after_waits
        ):
            self.stop = True
        return supervise.WaitResult(lines=lines, exits=exits)

    def kill_all(self) -> None:
        self.actions.append("kill")
        for child in self.children:
            child.alive = False


def _items(*kinds: str) -> list[tuple[str, str]]:
    return [(kind, f"cmd-{kind}-{index}") for index, kind in enumerate(kinds)]


def _records(host: FakeHost) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for line in host.lines:
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            found.append(payload)
    return found


def _run(
    host: FakeHost,
    items: list[tuple[str, str]],
    *,
    heartbeat_s: float = 30.0,
    coverage_s: float = 30.0,
    nonce: str = "nonce-1",
    emit_depth: bool = False,
    debug: bool = False,
    chatty: bool = False,
    forwarding_frontier: Callable[[], dict[str, object]] | None = None,
) -> int:
    host.lease_nonce = nonce
    return supervise.run_supervisor(
        project_root="/tmp/supervise-test",
        controller_label="bugs",
        lease_nonce=nonce,
        host=host,
        heartbeat_s=heartbeat_s,
        coverage_s=coverage_s,
        items=items,
        emit_depth=emit_depth,
        debug=debug,
        chatty=chatty,
        forwarding_frontier=forwarding_frontier,
    )


def test_supervise_commands_call_the_rearm_generator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GOALFLIGHT_PERSISTENT_BACKUP_SLOTS", raising=False)
    seen: dict[str, object] = {}

    def fake_rearm(
        status: dict[str, object],
        project_root: Path | str,
        *,
        controller_label: str,
        lease_nonce: str | None = None,
    ) -> list[str]:
        seen["status"] = status
        seen["project_root"] = project_root
        seen["controller_label"] = controller_label
        seen["lease_nonce"] = lease_nonce
        return ["STREAM", "BACKUP", "WATCHDOG"]

    monkeypatch.setattr(wake, "coverage_rearm_commands", fake_rearm)
    commands = wake.coverage_supervise_commands(
        tmp_path,
        controller_label="bugs",
        lease_nonce="nonce-from-session",
    )
    assert commands == ["STREAM", "BACKUP", "WATCHDOG"]
    status = seen["status"]
    assert isinstance(status, dict)
    assert status["wake_mode"] == "persistent"
    assert status["live_waiters"] == 0
    assert status["target_waiters"] == wake.persistent_wake_target() == 8
    assert status["backup"]["target"] == wake.persistent_backup_slot_count() == 6
    assert status["missing_components"] == ["stream", "backup", "watchdog"]
    assert seen["lease_nonce"] == "nonce-from-session"
    assert seen["controller_label"] == "bugs"


def test_rearm_commands_expand_configured_backup_shortfall(tmp_path: Path) -> None:
    status = {
        "wake_mode": "persistent",
        "live_waiters": 0,
        "target_waiters": 8,
        "missing_components": ["backup"],
        "backup": {"observed": 0, "target": 6},
        "portable_live_waiters": 0,
        "portable_target_waiters": 6,
    }
    commands = wake.coverage_rearm_commands(
        status,
        tmp_path,
        controller_label="bugs",
        lease_nonce="nonce-1",
    )
    assert len(commands) == 6
    assert all("listen" in command for command in commands)
    assert all("--watch-follow" not in command for command in commands)


@pytest.mark.parametrize("backup_count", [1, 6, 32])
def test_supervisor_runs_the_configured_pool_size(backup_count: int) -> None:
    items = [("stream", "cmd-stream")]
    items.extend(("backup", f"cmd-backup-{index}") for index in range(backup_count))
    items.append(("watchdog", "cmd-watchdog"))
    host = FakeHost(stop_after_spawns=len(items))
    code = _run(
        host,
        items,
        heartbeat_s=100.0,
        coverage_s=100.0,
        emit_depth=True,
    )
    assert code == 0
    assert len(host.spawns) == len(items)
    assert [kind for kind, _command in host.spawns].count("backup") == backup_count
    coverage = next(
        record
        for record in _records(host)
        if record.get("type") == "coverage"
    )
    assert coverage["target"] == len(items)
    assert coverage["live"] == 0


def test_stop_records_compute_a_faithful_rearm_from_invocation_inputs() -> None:
    cases = [
        (FakeHost(), [], "nonce-1"),
        (FakeHost(nonce=""), _items("stream"), "nonce-1"),
        (
            FakeHost(
                scripts={
                    "watchdog": [
                        PlannedExit(
                            lifetime_s=0.1,
                            returncode=3,
                            output=(
                                "listen: this controller generation already has "
                                "a live follow watchdog"
                            ),
                        )
                    ]
                },
                stop_on_stop_reason="did-not-arm",
            ),
            _items("stream", "watchdog"),
            "nonce-1",
        ),
    ]
    stops: list[dict[str, object]] = []
    for host, items, nonce in cases:
        _run(
            host,
            items,
            nonce=nonce,
            heartbeat_s=73.0,
            coverage_s=91.0,
        )
        stops.extend(
            record for record in _records(host) if record.get("type") == "stop"
        )

    assert len(stops) == len(cases)
    for stop in stops:
        argv = shlex.split(str(stop.get("rearm") or ""))
        assert argv[0] == "python3"
        assert Path(argv[1]).is_absolute()
        assert Path(argv[1]).name == "goalflight_messages.py"
        assert argv[2] == "supervise"
        assert Path(argv[argv.index("--project-root") + 1]) == Path(
            "/tmp/supervise-test"
        ).resolve()
        assert argv[argv.index("--controller-label") + 1] == "bugs"
        assert argv[argv.index("--lease-nonce") + 1] == "nonce-1"
        assert argv[argv.index("--heartbeat-secs") + 1] == "73"
        assert argv[argv.index("--coverage-secs") + 1] == "91"


def test_stop_rearm_record_trims_short_detail_without_losing_valid_json() -> None:
    rearm = "python3 " + ("x" * 350)
    line = supervise._supervise_line(
        {
            "kind": "supervise",
            "type": "stop",
            "reason": "dead-lease-nonce",
            "scope": "supervisor",
            "live": 0,
            "target": 8,
            "rearm": rearm,
            "detail": "diagnostic-" * 18,
        }
    )
    assert len(line.encode("utf-8")) <= supervise.STREAM_LINE_MAX_BYTES
    payload = json.loads(line)
    assert payload["rearm"] == rearm
    assert payload["type"] == "stop"


def test_production_oversized_exact_rearm_record_remains_valid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_PERSISTENT_BACKUP_SLOTS", "11")
    project_root = Path("/tmp") / ("long-project-root-" * 16)
    controller_label = "c" * 64
    lease_nonce = "n" * 32
    rearm = supervise._supervisor_rearm_command(
        project_root=project_root,
        controller_label=controller_label,
        lease_nonce=lease_nonce,
        heartbeat_s=1800.0,
        coverage_s=1800.0,
        debug=True,
    )
    line = supervise._supervise_line(
        {
            "kind": "supervise",
            "type": "stop",
            "reason": "dead-lease-nonce",
            "scope": "supervisor",
            "live": 0,
            "target": 8,
            "rearm": rearm,
        }
    )
    assert len(line.encode("utf-8")) > supervise.STREAM_LINE_MAX_BYTES
    payload = json.loads(line)
    assert payload["rearm"] == rearm
    assert payload["type"] == "stop"
    argv = shlex.split(rearm)
    assert argv[:2] == ["env", "GOALFLIGHT_PERSISTENT_BACKUP_SLOTS=11"]
    assert Path(argv[argv.index("--project-root") + 1]) == project_root.resolve()
    assert argv[argv.index("--controller-label") + 1] == controller_label
    assert argv[argv.index("--lease-nonce") + 1] == lease_nonce
    assert argv[-1] == "--debug"


def test_rearm_preserves_backup_depth_override_and_output_knobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_PERSISTENT_BACKUP_SLOTS", "11")
    host = FakeHost()
    _run(
        host,
        [],
        heartbeat_s=61.0,
        coverage_s=89.0,
        chatty=True,
        debug=True,
    )
    stop = next(record for record in _records(host) if record.get("type") == "stop")
    argv = shlex.split(str(stop["rearm"]))
    assert argv[:2] == ["env", "GOALFLIGHT_PERSISTENT_BACKUP_SLOTS=11"]
    assert argv[2] == "python3"
    assert Path(argv[3]).is_absolute()
    assert Path(argv[3]).name == "goalflight_messages.py"
    assert argv[4] == "supervise"
    assert argv[argv.index("--heartbeat-secs") + 1] == "61"
    assert argv[argv.index("--coverage-secs") + 1] == "89"
    assert argv[-2:] == ["--chatty", "--debug"]


def test_signal_exit_is_written_before_teardown_with_rearm() -> None:
    host = FakeHost(stop_after_spawns=1, stop_signum=signal.SIGTERM)
    code = _run(
        host,
        _items("stream"),
        heartbeat_s=67.0,
        coverage_s=83.0,
        emit_depth=True,
    )
    assert code == 128 + signal.SIGTERM
    exit_record = next(
        record for record in _records(host) if record.get("type") == "exit"
    )
    assert exit_record["reason"] == "signal-SIGTERM"
    assert exit_record["live"] == 0
    assert exit_record["target"] == 1
    assert "goalflight_messages.py supervise" in str(exit_record["rearm"])
    assert host.actions.index("write:exit") < host.actions.index("kill")


def test_unchanged_counts_are_silent_and_one_state_change_emits_once() -> None:
    host = FakeHost(stop_after_waits=4)
    _run(
        host,
        _items("stream"),
        heartbeat_s=1.0,
        coverage_s=0.05,
        emit_depth=True,
    )
    coverages = [
        record
        for record in _records(host)
        if record.get("type") == "coverage"
    ]
    assert [(record["live"], record["target"]) for record in coverages] == [
        (0, 1),
        (1, 1),
    ]
    assert not any(
        record.get("type") == "heartbeat" for record in _records(host)
    )


def test_state_change_coverage_does_not_wait_for_slow_heartbeat() -> None:
    host = FakeHost(
        scripts={
            "stream": [
                PlannedExit(
                    lifetime_s=20.0,
                    returncode=0,
                    armed=True,
                    stdout_lines=[(0.05, "STREAM-EVENT")],
                )
            ]
        },
        stop_when_lines_contain=("STREAM-EVENT",),
    )
    _run(
        host,
        _items("stream"),
        heartbeat_s=10.0,
        coverage_s=10.0,
        emit_depth=True,
    )
    coverages = [
        record for record in _records(host) if record.get("type") == "coverage"
    ]
    assert [(record["live"], record["target"]) for record in coverages] == [
        (0, 1),
        (1, 1),
    ]
    assert not any(
        record.get("type") == "heartbeat" for record in _records(host)
    )


def test_debug_restores_unconditional_per_tick_counts() -> None:
    host = FakeHost(stop_after_waits=3)
    _run(
        host,
        _items("stream"),
        heartbeat_s=1.0,
        coverage_s=0.05,
        debug=True,
    )
    counts = [
        record
        for record in _records(host)
        if record.get("type") in {"heartbeat", "coverage"}
    ]
    assert len(counts) == 5
    assert [record["type"] for record in counts[:2]] == ["coverage", "heartbeat"]
    assert all("live" not in record and "target" not in record for record in counts)


def test_slow_heartbeat_is_the_real_write_with_unchanged_state() -> None:
    host = FakeHost(stop_after_waits=4)
    _run(
        host,
        _items("stream"),
        heartbeat_s=0.12,
        coverage_s=0.05,
        emit_depth=True,
    )
    heartbeats = [
        record for record in _records(host) if record.get("type") == "heartbeat"
    ]
    assert heartbeats == [
        {
            "kind": "supervise",
            "live": 1,
            "seq": 1,
            "target": 1,
            "type": "heartbeat",
        }
    ]


def test_failed_slow_heartbeat_write_tears_down_immediately() -> None:
    host = FakeHost(stop_after_waits=6, fail_write_type="heartbeat")
    code = _run(
        host,
        _items("stream"),
        heartbeat_s=0.12,
        coverage_s=0.05,
    )
    assert code == 0
    assert host._waits < 6
    heartbeat_index = host.actions.index("write:heartbeat")
    assert host.actions[heartbeat_index + 1 :] == ["kill"]
    assert sum(action == "write:heartbeat" for action in host.actions) == 1


def test_positive_fast_peer_probe_stops_without_waiting_for_long_write() -> None:
    host = FakeHost(peer_gone_after_checks=2)
    _run(
        host,
        _items("stream"),
        heartbeat_s=1.0,
        coverage_s=0.05,
    )
    assert host._waits == 1
    assert not any(
        record.get("type") == "heartbeat" for record in _records(host)
    )
    assert host.actions[-1] == "kill"


def test_slow_supervisor_heartbeat_does_not_change_stream_watchdog_cadence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOALFLIGHT_PERSISTENT_BACKUP_SLOTS", raising=False)
    assert supervise.DEFAULT_SUPERVISOR_HEARTBEAT_S == 1500.0
    assert supervise.MAX_SUPERVISOR_HEARTBEAT_S == 1800.0
    assert messages.FOLLOW_HEARTBEAT_SECS == 120.0
    assert messages.FOLLOW_DEAD_AFTER_INTERVALS == 3
    assert messages.FOLLOW_DEAD_AFTER_SECS == 360.0

    host = FakeHost(stop_after_spawns=wake.persistent_wake_target())
    code = supervise.run_supervisor(
        project_root=tmp_path,
        controller_label="bugs",
        lease_nonce="nonce-1",
        host=host,
        heartbeat_s=1800.0,
        coverage_s=1800.0,
        items=None,
    )
    assert code == 0
    assert len(host.spawns) == wake.persistent_wake_target() == 8
    stream_command = next(command for kind, command in host.spawns if kind == "stream")
    stream_argv = shlex.split(stream_command)
    assert "follow" in stream_argv
    assert "--heartbeat-secs" not in stream_argv
    watchdog_command = next(
        command for kind, command in host.spawns if kind == "watchdog"
    )
    watchdog_argv = shlex.split(watchdog_command)
    assert "--watch-follow" in watchdog_argv
    assert "--heartbeat-secs" not in watchdog_argv
    assert messages.FOLLOW_HEARTBEAT_SECS == 120.0
    assert messages.FOLLOW_DEAD_AFTER_INTERVALS == 3
    assert messages.FOLLOW_DEAD_AFTER_SECS == 360.0


def test_supervise_cli_accepts_new_heartbeat_ceiling_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("GOALFLIGHT_DISPATCH_ID", raising=False)
    monkeypatch.delenv("GOALFLIGHT_TEST_MODE", raising=False)
    monkeypatch.setattr(supervise, "_stdout_is_regular_file", lambda _stream: None)
    monkeypatch.setattr(
        goalflight_task, "resolve_project_root", lambda _value: tmp_path
    )
    monkeypatch.setattr(
        sessions,
        "resolve_controller_label",
        lambda *_args, **_kwargs: "bugs",
    )
    monkeypatch.setattr(
        supervise,
        "resolve_startup_lease_nonce",
        lambda **_kwargs: ("nonce-1", None, None),
    )
    monkeypatch.setattr(supervise, "RealHost", lambda **_kwargs: object())
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        supervise,
        "run_supervisor",
        lambda **kwargs: calls.append(kwargs) or 0,
    )
    args = SimpleNamespace(
        project_root=str(tmp_path),
        controller_label="bugs",
        lease_nonce="nonce-1",
        heartbeat_secs=1800.0,
        coverage_secs=0.0,
        debug=False,
    )

    assert supervise.cmd_supervise(args) == 0
    assert calls[0]["heartbeat_s"] == 1800.0
    assert calls[0]["coverage_s"] == 1800.0

    args.heartbeat_secs = 1800.1
    assert supervise.cmd_supervise(args) == supervise.SUPERVISE_START_EXIT
    assert len(calls) == 1
    assert "between 60 and 1800" in capsys.readouterr().err


def test_dead_child_is_restarted() -> None:
    host = FakeHost(
        scripts={
            "backup": [
                PlannedExit(lifetime_s=0.5, returncode=2, output="journal-busy", armed=True),
            ]
        },
        stop_after_spawns=2,
    )
    code = _run(host, _items("backup"), heartbeat_s=100.0, coverage_s=100.0)
    assert code == 0
    assert [kind for kind, _command in host.spawns] == ["backup", "backup"]
    restart = next(
        record for record in _records(host) if record.get("type") == "restart"
    )
    assert restart["child"] == "backup"
    assert restart["exit"] == 2
    assert restart["reason"] == "exit-2"
    assert "live" not in restart
    assert "target" not in restart
    restart_index = host.actions.index("write:restart")
    assert host.actions[restart_index + 1] == "write:coverage"


def test_exit_3_unclassified_backoffs_instead_of_implying_contention() -> None:
    host = FakeHost(
        scripts={
            "backup": [
                PlannedExit(
                    lifetime_s=0.2,
                    returncode=3,
                    output="listen: unexpected diagnostic-free exit 3",
                    armed=True,
                ),
            ]
        },
        stop_after_spawns=2,
    )
    _run(host, _items("backup"), heartbeat_s=100.0, coverage_s=100.0)
    restart = next(
        record for record in _records(host) if record.get("type") == "restart"
    )
    assert restart["reason"] == "exit-3-unclassified"
    assert float(restart["backoff_s"]) == 1.0
    assert len(host.spawns) == 2


def test_exit_0_without_arming_stops_with_distinct_reason() -> None:
    host = FakeHost(
        scripts={
            "stream": [
                PlannedExit(
                    lifetime_s=0.1,
                    returncode=0,
                    output="follow: controller-capability-mismatch",
                    armed=False,
                ),
            ]
        }
    )
    code = _run(host, _items("stream"), heartbeat_s=100.0, coverage_s=100.0)
    assert code == supervise.SUPERVISE_STOP_EXIT
    stop = next(record for record in _records(host) if record.get("type") == "stop")
    assert stop["reason"] == "dead-lease-nonce"
    assert stop["child"] == "stream"
    assert "live" not in stop
    assert "target" not in stop
    assert "goalflight_messages.py supervise" in str(stop["rearm"])
    assert len(host.spawns) == 1


def test_exit_0_watchdog_slot_held_without_arming_stops() -> None:
    host = FakeHost(
        scripts={
            "stream": [],
            "watchdog": [
                PlannedExit(
                    lifetime_s=0.1,
                    returncode=3,
                    output="listen: this controller generation already has a live follow watchdog",
                    armed=False,
                ),
            ]
        },
        stop_on_stop_reason="did-not-arm",
    )
    code = _run(
        host,
        _items("stream", "watchdog"),
        heartbeat_s=100.0,
        coverage_s=100.0,
    )
    assert code != supervise.SUPERVISE_STOP_EXIT
    stop = next(record for record in _records(host) if record.get("type") == "stop")
    assert stop["reason"] == "did-not-arm"
    assert stop.get("scope") == "slot"
    assert stop["child"] == "watchdog"
    assert "already has a live follow watchdog" in str(stop.get("detail") or "")
    assert host.alive_by_kind_at_stop.get("stream") is True
    assert [kind for kind, _command in host.spawns].count("stream") == 1
    stop_index = host.actions.index("write:stop")
    assert host.actions[stop_index + 1] == "write:coverage"


def test_terse_supervisor_replaces_only_own_stream_heartbeat() -> None:
    host = FakeHost(
        scripts={
            "stream": [
                PlannedExit(
                    lifetime_s=5.0,
                    returncode=0,
                    armed=True,
                    stdout_lines=[
                        (0.0, '{"kind":"heartbeat","payload":{"seq":1,"from":"stream"}}'),
                        (
                            0.01,
                            '{"kind":"frontier","payload":{"advisory":"information-only","id":"t-022","state":"projected","title":"Useful next task"}}',
                        ),
                        (
                            0.02,
                            '{"kind":"event","payload":{"data":{"subject":"heartbeat quoted in event"},"type":"controller-notice"}}',
                        ),
                    ],
                )
            ],
            "backup": [
                PlannedExit(
                    lifetime_s=5.0,
                    returncode=0,
                    armed=True,
                    stdout_lines=[
                        (0.0, "FROM worker subject=heartbeat quoted in mail"),
                        (
                            0.01,
                            '{"kind":"heartbeat","payload":{"from":"backup","seq":9}}',
                        ),
                    ],
                )
            ],
            "watchdog": [
                PlannedExit(
                    lifetime_s=5.0,
                    returncode=0,
                    armed=True,
                    stdout_lines=[
                        (0.0, '{"kind":"event","payload":{"type":"listener-dead"}}'),
                    ],
                )
            ],
        },
        stop_when_lines_contain=(
            '"kind":"next"',
            '"id":"t-022"',
            "FROM worker subject=heartbeat quoted in mail",
            '{"kind":"heartbeat","payload":{"from":"backup","seq":9}}',
            '"subject":"heartbeat quoted in event"',
            '{"kind":"event","payload":{"type":"listener-dead"}}',
        ),
    )
    host.scripts["stream"][0].lifetime_s = 80.0
    host.scripts["backup"][0].lifetime_s = 80.0
    host.scripts["watchdog"][0].lifetime_s = 80.0
    _run(
        host,
        _items("stream", "backup", "watchdog"),
        heartbeat_s=50.0,
        coverage_s=50.0,
    )
    joined = "".join(host.lines)
    assert '{"kind":"heartbeat","payload":{"seq":1,"from":"stream"}}' not in joined
    assert "FROM worker subject=heartbeat quoted in mail" in joined
    assert '{"kind":"heartbeat","payload":{"from":"backup","seq":9}}' in joined
    assert '"subject":"heartbeat quoted in event"' in joined
    assert '{"kind":"event","payload":{"type":"listener-dead"}}' in joined
    next_record = next(record for record in _records(host) if record.get("kind") == "next")
    assert next_record["payload"] == {
        "directive": "goal-flight next",
        "id": "t-022",
        "state": "projected",
        "title": "Useful next task",
    }
    assert not any(
        record.get("kind") == "supervise" and record.get("type") == "heartbeat"
        for record in _records(host)
    )


def test_failed_actionable_wake_uses_detector_stop_and_rearm() -> None:
    class FailedNextHost(FakeHost):
        failed_next = False

        def write_stdout(self, line: str) -> bool:
            try:
                kind = str(json.loads(line).get("kind") or "")
            except (AttributeError, json.JSONDecodeError):
                kind = ""
            if kind == "next" and not self.failed_next:
                self.failed_next = True
                raise OSError(errno.EIO, "actionable wake write failed")
            return super().write_stdout(line)

    host = FailedNextHost(
        scripts={
            "stream": [
                PlannedExit(
                    lifetime_s=5.0,
                    returncode=0,
                    armed=True,
                    stdout_lines=[
                        (0.0, '{"kind":"heartbeat","payload":{"seq":1}}'),
                        (
                            0.01,
                            '{"kind":"frontier","payload":{"id":"t-022","state":"projected","title":"Useful next task"}}',
                        ),
                    ],
                )
            ]
        }
    )

    code = _run(host, _items("stream"), heartbeat_s=100.0, coverage_s=100.0)

    assert code == supervise.SUPERVISE_STOP_EXIT
    assert host.failed_next
    assert not any(child.alive for child in host.children)
    stop = next(record for record in _records(host) if record.get("type") == "stop")
    assert stop["reason"] == "stdout-peer-detector-unavailable"
    assert stop["detector"] == "write-record"
    assert stop["error"] == "EIO"
    assert "goalflight_messages.py supervise" in str(stop["rearm"])


def test_late_frontier_does_not_create_a_second_wake() -> None:
    host = FakeHost(
        scripts={
            "stream": [
                PlannedExit(
                    lifetime_s=2.0,
                    returncode=0,
                    armed=True,
                    stdout_lines=[
                        (
                            0.0,
                            '{"kind":"heartbeat","payload":{"seq":1}}',
                        ),
                        (
                            0.01,
                            '{"kind":"frontier","payload":{"state":"empty"}}',
                        ),
                        (
                            0.1,
                            '{"kind":"heartbeat","payload":{"seq":2}}',
                        ),
                        (
                            1.6,
                            '{"kind":"frontier","payload":{"id":"t-delayed","state":"projected","title":"Computed slowly"}}',
                        ),
                    ],
                )
            ]
        },
        stop_after_spawns=2,
    )

    _run(host, _items("stream"), heartbeat_s=100.0, coverage_s=100.0)

    actionable = [record for record in _records(host) if record.get("kind") == "next"]
    assert len(actionable) == 2
    assert actionable[0]["payload"]["directive"] == "Nothing pending"
    assert actionable[1]["payload"] == {
        "directive": "goal-flight next",
        "state": "unknown",
    }


def test_forwarding_projection_failure_cannot_turn_legacy_empty_into_idle() -> None:
    host = FakeHost(
        scripts={
            "stream": [
                PlannedExit(
                    lifetime_s=5.0,
                    returncode=0,
                    armed=True,
                    stdout_lines=[
                        (0.0, '{"kind":"heartbeat","payload":{"seq":1}}'),
                        (0.01, '{"kind":"frontier","payload":{"state":"empty"}}'),
                    ],
                )
            ]
        },
        stop_when_lines_contain=('"kind":"next"', '"state":"unavailable"'),
    )

    _run(
        host,
        _items("stream"),
        heartbeat_s=100.0,
        coverage_s=100.0,
        forwarding_frontier=lambda: {
            "kind": "frontier",
            "payload": {"state": "unavailable", "detail": "projection unreadable"},
        },
    )

    actionable = next(
        record for record in _records(host) if record.get("kind") == "next"
    )
    assert actionable["payload"] == {
        "detail": "projection unreadable",
        "directive": "goal-flight next",
        "state": "unavailable",
    }


def test_batched_heartbeat_frontier_pairs_keep_their_own_state() -> None:
    host = FakeHost(
        scripts={"stream": []},
        stop_when_lines_contain=('"id":"t-one"', '"id":"t-two"'),
    )
    delivered = False

    def wait_once(
        children: list[FakeChild], _timeout_s: float
    ) -> supervise.WaitResult:
        nonlocal delivered
        if delivered:
            host.stop = True
            return supervise.WaitResult(lines=[], exits=[])
        delivered = True
        child = children[0]
        return supervise.WaitResult(
            lines=[
                (
                    child,
                    '{"kind":"heartbeat","payload":{"seq":1}}',
                ),
                (
                    child,
                    '{"kind":"frontier","payload":{"id":"t-one","state":"projected","title":"One"}}',
                ),
                (
                    child,
                    '{"kind":"heartbeat","payload":{"seq":2}}',
                ),
                (
                    child,
                    '{"kind":"frontier","payload":{"id":"t-two","state":"projected","title":"Two"}}',
                ),
            ],
            exits=[],
        )

    host.wait = wait_once  # type: ignore[method-assign]
    _run(host, _items("stream"), heartbeat_s=100.0, coverage_s=100.0)

    actionable = [record for record in _records(host) if record.get("kind") == "next"]
    assert [record["payload"]["id"] for record in actionable] == ["t-one", "t-two"]


def test_backoff_resets_after_long_lived_and_escalates_after_fast_failure() -> None:
    assert supervise.next_backoff(0.0, ran_s=0.1, action=supervise.ACTION_BACKOFF) == 1.0
    assert supervise.next_backoff(1.0, ran_s=0.1, action=supervise.ACTION_BACKOFF) == 2.0
    assert supervise.next_backoff(64.0, ran_s=0.1, action=supervise.ACTION_BACKOFF) == 120.0
    assert supervise.next_backoff(64.0, ran_s=30.0, action=supervise.ACTION_BACKOFF) == 1.0
    assert supervise.next_backoff(8.0, ran_s=0.2, action=supervise.ACTION_REARM) == 0.0

    host = FakeHost(
        scripts={
            "stream": [
                PlannedExit(lifetime_s=0.2, returncode=2, output="fault", armed=True),
                PlannedExit(lifetime_s=0.2, returncode=2, output="fault", armed=True),
                PlannedExit(
                    lifetime_s=supervise.LONG_LIVED_S + 1.0,
                    returncode=2,
                    output="fault",
                    armed=True,
                ),
                PlannedExit(lifetime_s=0.2, returncode=2, output="fault", armed=True),
            ]
        }
    )
    host.stop_after_spawns = 5
    _run(host, _items("stream"), heartbeat_s=1000.0, coverage_s=1000.0)
    restarts = [record for record in _records(host) if record.get("type") == "restart"]
    delays = [float(record["backoff_s"]) for record in restarts]
    assert delays[0] == 1.0
    assert delays[1] == 2.0
    assert delays[2] == 1.0


def test_dead_nonce_from_session_status_stops_before_respawn() -> None:
    host = FakeHost(
        scripts={
            "backup": [
                PlannedExit(lifetime_s=0.2, returncode=2, output="fault", armed=True),
            ]
        }
    )

    original_spawn = host.spawn

    def spawn_and_kill_nonce(kind: str, command: str) -> FakeChild:
        child = original_spawn(kind, command)
        if len(host.spawns) == 1:
            host.nonce = "nonce-2"
        return child

    host.spawn = spawn_and_kill_nonce  # type: ignore[method-assign]
    code = _run(host, _items("backup"), heartbeat_s=100.0, coverage_s=100.0)
    assert code == supervise.SUPERVISE_STOP_EXIT
    stop = next(record for record in _records(host) if record.get("type") == "stop")
    assert stop["reason"] == "dead-lease-nonce"
    assert len(host.spawns) == 1


def test_classify_exit_taxonomy() -> None:
    assert supervise.classify_child_exit(
        kind="stream",
        returncode=0,
        output="follow: controller-capability-mismatch",
        armed=False,
    ) == (supervise.ACTION_STOP, "dead-lease-nonce")
    assert supervise.classify_child_exit(
        kind="watchdog",
        returncode=0,
        output="listen: this controller generation already has a live follow watchdog",
        armed=False,
    ) == (supervise.ACTION_STOP, "did-not-arm")
    assert supervise.classify_child_exit(
        kind="backup",
        returncode=0,
        output='{"kind":"exit","reason":"event"}',
        armed=True,
    ) == (supervise.ACTION_REARM, "rang")
    assert supervise.classify_child_exit(
        kind="backup",
        returncode=3,
        output="listen: unexpected diagnostic-free exit 3",
        armed=False,
    ) == (supervise.ACTION_BACKOFF, "exit-3-unclassified")
    assert supervise.classify_child_exit(
        kind="watchdog",
        returncode=3,
        output="listen: orphaned: watchdog parent changed",
        armed=True,
    ) == (supervise.ACTION_BACKOFF, "orphaned-parent")
    assert supervise.classify_child_exit(
        kind="backup",
        returncode=3,
        output="listen: orphaned: listener parent changed",
        armed=True,
    ) == (supervise.ACTION_BACKOFF, "orphaned-parent")
    assert supervise.classify_child_exit(
        kind="watchdog",
        returncode=3,
        output="listen: orphaned: controlling stdout closed; tracked task is gone",
        armed=True,
    ) == (supervise.ACTION_BACKOFF, "orphaned-stdout")
    assert supervise.classify_child_exit(
        kind="stream",
        returncode=2,
        output="journal-busy",
        armed=True,
    ) == (supervise.ACTION_BACKOFF, "exit-2")
    assert supervise.classify_child_exit(
        kind="watchdog",
        returncode=3,
        output="listen: stale-lease: watchdog generation is no longer active",
        armed=True,
    ) == (supervise.ACTION_STOP, "dead-lease-nonce")
    assert supervise.classify_child_exit(
        kind="backup",
        returncode=2,
        output="listen: journal-unavailable: cannot open journal",
        armed=False,
    ) == (supervise.ACTION_BACKOFF, "journal-unreadable")
    assert supervise.classify_child_exit(
        kind="stream",
        returncode=2,
        output="follow: journal-io-failure: sqlite busy",
        armed=False,
    ) == (supervise.ACTION_BACKOFF, "journal-unreadable")
    assert supervise.classify_child_exit(
        kind="stream",
        returncode=0,
        output="",
        armed=False,
    ) == (supervise.ACTION_REARM, "rang")
    assert supervise.classify_child_exit(
        kind="stream",
        returncode=3,
        output="follow: this controller lease already has a persistent stream",
        armed=False,
    ) == (supervise.ACTION_STOP, "did-not-arm")
    assert supervise.classify_child_exit(
        kind="backup",
        returncode=5,
        output=(
            "listen: did-not-arm: lease-nonce-not-live: --lease-nonce is not "
            "a live controller lease; this process is not waiting and will "
            "not cover the pool"
        ),
        armed=False,
    ) == (supervise.ACTION_STOP, "dead-lease-nonce")
    assert supervise.classify_child_exit(
        kind="backup",
        returncode=5,
        output="listen: did-not-arm: no live controller lease is present",
        armed=False,
    ) == (supervise.ACTION_STOP, "dead-lease-nonce")
    assert supervise.classify_child_exit(
        kind="backup",
        returncode=5,
        output="",
        armed=False,
    ) == (supervise.ACTION_STOP, "dead-lease-nonce")


def test_default_supervisor_output_suppresses_depth_and_opt_in_restores_it() -> None:
    default_host = FakeHost(stop_after_spawns=1)
    _run(
        default_host,
        _items("stream"),
        heartbeat_s=100.0,
        coverage_s=100.0,
    )
    default_records = _records(default_host)
    assert [record["type"] for record in default_records] == ["coverage"]
    assert all(
        "live" not in record and "target" not in record
        for record in default_records
    )

    depth_host = FakeHost(stop_after_spawns=1)
    _run(
        depth_host,
        _items("stream"),
        heartbeat_s=100.0,
        coverage_s=100.0,
        emit_depth=True,
    )
    records = _records(depth_host)
    assert [record["type"] for record in records] == ["coverage"]
    assert all(
        isinstance(record.get("live"), int)
        and isinstance(record.get("target"), int)
        for record in records
    )


def test_opt_in_live_counts_armed_components_not_pids() -> None:
    host = FakeHost(stop_after_coverage=2)
    _run(
        host,
        _items("stream", "backup", "watchdog"),
        heartbeat_s=100.0,
        coverage_s=0.05,
        emit_depth=True,
    )
    coverages = [record for record in _records(host) if record.get("type") == "coverage"]
    assert coverages[0]["live"] == 0
    assert coverages[0]["target"] == 3
    assert coverages[-1]["live"] == coverages[-1]["target"] == 3


def test_journal_unreadable_is_retryable_not_dead_nonce() -> None:
    host = FakeHost(
        scripts={
            "backup": [
                PlannedExit(
                    lifetime_s=0.1,
                    returncode=2,
                    output="listen: journal-unavailable: journal is busy",
                    armed=False,
                ),
            ]
        },
        stop_after_spawns=2,
    )
    code = _run(host, _items("backup"), heartbeat_s=100.0, coverage_s=100.0)
    assert code != supervise.SUPERVISE_STOP_EXIT
    reasons = [record.get("reason") for record in _records(host)]
    assert "dead-lease-nonce" not in reasons
    restart = next(record for record in _records(host) if record.get("type") == "restart")
    assert restart["reason"] == "journal-unreadable"


def test_unreadable_journal_probe_does_not_stop_the_supervisor() -> None:
    host = FakeHost(stop_after_coverage=3)
    original = host.spawn

    def spawn_then_unread(kind: str, command: str) -> FakeChild:
        child = original(kind, command)
        host.nonce_state = "unreadable"
        return child

    host.spawn = spawn_then_unread  # type: ignore[method-assign]
    code = _run(
        host,
        _items("stream"),
        heartbeat_s=100.0,
        coverage_s=0.05,
        emit_depth=True,
        debug=True,
    )
    assert code != supervise.SUPERVISE_STOP_EXIT
    assert all(
        record.get("reason") != "dead-lease-nonce" for record in _records(host)
    )


def test_permanent_unarmed_exit_2_is_visible_terminal_not_healthy() -> None:
    host = FakeHost(
        scripts={
            "backup": [
                PlannedExit(
                    lifetime_s=0.05,
                    returncode=2,
                    output="listen: pending-report claim is poisoned",
                    armed=False,
                )
                for _ in range(supervise.PERMANENT_UNARMED_FAULTS)
            ],
            "stream": [],
        },
        stop_on_stop_reason="permanent-exit-2",
    )
    code = _run(
        host,
        _items("stream", "backup"),
        heartbeat_s=100.0,
        coverage_s=0.05,
    )
    assert code != supervise.SUPERVISE_STOP_EXIT
    stop = next(record for record in _records(host) if record.get("type") == "stop")
    assert stop["reason"] == "permanent-exit-2"
    assert stop.get("scope") == "slot"
    assert stop["child"] == "backup"
    assert host.alive_by_kind_at_stop.get("stream") is True
    assert [kind for kind, _command in host.spawns].count("backup") == (
        supervise.PERMANENT_UNARMED_FAULTS
    )
    assert "live" not in stop
    assert "target" not in stop
    assert "goalflight_messages.py supervise" in str(stop["rearm"])


def test_supervise_items_are_the_configured_persistent_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GOALFLIGHT_PERSISTENT_BACKUP_SLOTS", raising=False)
    items = wake.coverage_supervise_items(
        tmp_path,
        controller_label="bugs",
        lease_nonce="nonce-1",
    )
    kinds = [kind for kind, _command in items]
    assert kinds.count("stream") == 1
    assert kinds.count("backup") == wake.persistent_backup_slot_count() == 6
    assert kinds.count("watchdog") == 1
    assert len(items) == wake.persistent_wake_target() == 8
    backup = next(command for kind, command in items if kind == "backup")
    assert f"--listener-slots {wake.persistent_backup_slot_count()}" in backup
    assert "--report-pending" in backup
    assert "--watch-follow" not in backup


def test_controller_mail_documents_supervise_front_door() -> None:
    doctrine = (ROOT / "protocols" / "controller-mail.md").read_text(encoding="utf-8")
    assert "goalflight_messages.py supervise" in doctrine
    assert "--lease-nonce" in doctrine
    assert "live/" in doctrine
    assert "no timeout" in doctrine.lower()
    assert "`persistent: true`" in doctrine
    assert "`timeout_ms` inert" in doctrine


def test_supervisor_signal_exit_contract_matches_installed_handlers(
    tmp_path: Path,
) -> None:
    host = supervise.RealHost(
        project_root=tmp_path,
        controller_label="bugs",
        lease_nonce="nonce-1",
        nonce_reader=lambda: "nonce-1",
    )
    try:
        handled = {signal.Signals(signum).name for signum in host._prev_handlers}
    finally:
        host.kill_all()

    assert handled == {"SIGTERM", "SIGINT", "SIGHUP"}
    for relative in (
        "protocols/controller-mail.md",
        "docs/EVENT-ARCHITECTURE.md",
        "CHANGELOG.md",
    ):
        doctrine = (ROOT / relative).read_text(encoding="utf-8")
        for signame in handled:
            assert f"`{signame}`" in doctrine
        assert "catchable signal" not in doctrine.lower()
        assert "catchable-signal" not in doctrine.lower()


@pytest.mark.parametrize(
    "relative",
    [
        "SKILL.md",
        "commands/execute.md",
        "docs/EVENT-ARCHITECTURE.md",
        "docs/controller-behaviours.md",
    ],
)
def test_every_supervisor_arming_site_requires_session_lifetime_no_timeout(
    relative: str,
) -> None:
    doctrine = (ROOT / relative).read_text(encoding="utf-8")
    assert "no timeout" in doctrine.lower()
    assert "`persistent: true`" in doctrine
    assert "`timeout_ms` inert" in doctrine


def test_supervise_cli_is_the_one_command_front_door(tmp_path: Path) -> None:
    command = wake.coverage_supervise_command(
        tmp_path,
        controller_label="bugs",
        lease_nonce="nonce-1",
    )
    assert "goalflight_messages.py" in command
    assert " supervise " in f" {command} "
    assert "--controller-label bugs" in command
    assert "--lease-nonce nonce-1" in command
    help_text = subprocess.run(
        [sys.executable, str(SCRIPTS / "goalflight_messages.py"), "supervise", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert help_text.returncode == 0
    assert "persistent wake pool" in help_text.stdout
    assert "default 1500" in help_text.stdout
    assert "production 60-1800" in help_text.stdout
    assert "--debug" in help_text.stdout
    assert "--chatty" in help_text.stdout
    assert "restore raw stream keepalives" in help_text.stdout
    top = subprocess.run(
        [sys.executable, str(SCRIPTS / "goalflight_messages.py"), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert top.returncode == 0
    assert "supervise" in top.stdout


@pytest.fixture()
def isolated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, dict[str, str], journal.LeaseIdentity]:
    label = "supervise-test"
    env = isolated_machine_env(tmp_path)
    env.update(
        {
            "GOALFLIGHT_TEST_MODE": "1",
            "GOALFLIGHT_CONTROLLER_LABEL": label,
            "GOALFLIGHT_PROCESS_ROLE": "controller",
            "GOALFLIGHT_WAKE_ENTRY_POLL_S": "0",
        }
    )
    for key in AMBIENT_IDENTITY_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("GOALFLIGHT_WAKE_LEDGER", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    project = tmp_path / "project"
    project.mkdir()
    authority = journal.open_or_create_journal(project)
    claimed = authority.claim_or_renew_lease(
        label,
        principal={"principal_id": "supervise-test-principal"},
    )
    assert claimed.committed and claimed.value is not None
    return project, {**os.environ, **env}, claimed.value


def test_listener_role_can_pin_explicit_lease_nonce(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, env, lease = isolated
    with wake.register_lease_holder(
        project, controller_label=lease.label, lease_nonce=lease.nonce
    ):
        monkeypatch.setenv("GOALFLIGHT_PROCESS_ROLE", "listener")
        monkeypatch.delenv("GOALFLIGHT_DISPATCH_ID", raising=False)
        resolved = messages._resolve_listen_auto_lease(
            project,
            controller_label=lease.label,
            explicit_nonce=lease.nonce,
        )
    assert resolved.get("claimed") is True
    assert resolved.get("reason") == "explicit-lease-nonce"
    assert resolved.get("nonce") == lease.nonce


def test_listener_role_without_nonce_still_refuses_auto_claim(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _env, lease = isolated
    monkeypatch.setenv("GOALFLIGHT_PROCESS_ROLE", "listener")
    monkeypatch.delenv("GOALFLIGHT_DISPATCH_ID", raising=False)
    resolved = messages._resolve_listen_auto_lease(
        project,
        controller_label=lease.label,
        explicit_nonce=None,
    )
    assert resolved.get("claimed") is False
    assert resolved.get("reason") == "non-controller-role"


def test_leftover_watchdog_is_production_did_not_arm_and_keeps_siblings(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    project, env, lease = isolated
    listener_env = {
        **env,
        "GOALFLIGHT_PROCESS_ROLE": "listener",
    }
    listener_env.pop("GOALFLIGHT_DISPATCH_ID", None)
    with wake.register_lease_holder(
        project, controller_label=lease.label, lease_nonce=lease.nonce
    ):
        with wake.register_watchdog_waiter(
            project,
            controller_label=lease.label,
            generation_key=lease.nonce,
        ):
            refused = subprocess.run(
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
                    "--watch-follow",
                    "--poll-secs",
                    "0.01",
                    "--timeout-s",
                    "1",
                ],
                cwd=project,
                env=listener_env,
                capture_output=True,
                text=True,
                timeout=5,
            )
    assert refused.returncode == 3, refused.stderr
    assert "already has a live follow watchdog" in refused.stderr
    action, reason = supervise.classify_child_exit(
        kind="watchdog",
        returncode=refused.returncode,
        output=refused.stderr,
        armed=False,
    )
    assert (action, reason) == (supervise.ACTION_STOP, "did-not-arm")


def test_supervise_refuses_regular_file_stdout_before_spawn(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    tmp_path: Path,
) -> None:
    project, env, lease = isolated
    output = tmp_path / "not-a-monitor.jsonl"
    supervise_env = dict(env)
    supervise_env.pop("GOALFLIGHT_DISPATCH_ID", None)
    with output.open("w", encoding="utf-8") as stream:
        refused = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "goalflight_messages.py"),
                "supervise",
                "--project-root",
                str(project),
                "--controller-label",
                lease.label,
                "--lease-nonce",
                lease.nonce,
                "--heartbeat-secs",
                "60",
            ],
            cwd=project,
            env=supervise_env,
            stdout=stream,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
    assert refused.returncode == supervise.SUPERVISE_START_EXIT
    assert "stdout is a regular file" in refused.stderr
    assert not wake.live_waiters(
        project,
        controller_label=lease.label,
        kinds={"listener", wake.MONITOR_KIND, wake.WATCHDOG_KIND},
    )


def test_coverage_status_keeps_t322_sizing_after_supervise(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    project, _env, lease = isolated
    with wake.register_lease_holder(
        project, controller_label=lease.label, lease_nonce=lease.nonce
    ):
        wake.activate_monitor_state(
            project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
            heartbeat_s=120,
            dead_after_s=360,
        )
        with wake.register_waiter(
            project,
            controller_label=lease.label,
            kind=wake.MONITOR_KIND,
            generation_key=lease.nonce,
        ):
            with wake.register_watchdog_waiter(
                project,
                controller_label=lease.label,
                generation_key=lease.nonce,
            ):
                status = wake.coverage_status(
                    project,
                    controller_label=lease.label,
                    lease_nonce=lease.nonce,
                )
    assert status["target_waiters"] == 8
    assert status["backup"]["target"] == 6
    assert "target" in status["backup"]
    assert status["portable_target_waiters"] == 6


class _RecordingHost(supervise.RealHost):
    """RealHost that captures multiplexed stdout and stops on restart/stop."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.lines: list[str] = []

    def write_stdout(self, line: str) -> bool:
        text = line if line.endswith("\n") else line + "\n"
        self.lines.append(text)
        if '"type":"restart"' in text or '"type":"stop"' in text:
            self._stop = True
        return True


def test_real_host_signal_wakes_blocking_wait(
    tmp_path: Path,
) -> None:
    host = supervise.RealHost(
        project_root=tmp_path,
        controller_label="bugs",
        lease_nonce="nonce-1",
        nonce_reader=lambda: "nonce-1",
    )
    timer = threading.Timer(
        0.05, host._on_signal, args=(signal.SIGTERM, None)
    )
    started = time.monotonic()
    timer.start()
    try:
        result = host.wait([], timeout_s=5.0)
        elapsed = time.monotonic() - started
    finally:
        timer.cancel()
        host.kill_all()

    assert elapsed < 1.0
    assert not host.running()
    assert host.stop_signum == signal.SIGTERM
    assert result.lines == []
    assert result.exits == []


def test_real_host_closed_stdout_wakes_wait_with_quiet_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = supervise.RealHost(
        project_root=tmp_path,
        controller_label="bugs",
        lease_nonce="nonce-1",
        nonce_reader=lambda: "nonce-1",
    )
    child = host.spawn(
        "backup",
        _python_child("import time; time.sleep(5)"),
    )
    original_stdout = sys.stdout
    reader_fd, writer_fd = os.pipe()
    peer_stdout = os.fdopen(writer_fd, "w", buffering=1)
    monkeypatch.setattr(sys, "stdout", peer_stdout)
    timer = threading.Timer(0.05, os.close, args=(reader_fd,))
    started = time.monotonic()
    timer.start()
    try:
        result = host.wait([child], timeout_s=2.0)
        elapsed = time.monotonic() - started
    finally:
        timer.cancel()
        monkeypatch.setattr(sys, "stdout", original_stdout)
        peer_stdout.close()
        try:
            os.close(reader_fd)
        except OSError:
            pass
        host.kill_all()

    assert elapsed < 0.5
    monkeypatch.setattr(messages, "_stdio_peer_gone", lambda _stream: False)
    assert host.stdio_peer_gone() is True
    assert result.lines == []
    assert result.exits == []


def test_real_host_stdout_registration_failure_fails_closed_with_quiet_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class InvalidatedStdout:
        def __init__(self, stream: object) -> None:
            self.stream = stream
            self.valid_fd = True

        def fileno(self) -> int:
            if not self.valid_fd:
                return -1
            return self.stream.fileno()  # type: ignore[attr-defined,no-any-return]

        def write(self, text: str) -> int:
            return self.stream.write(text)  # type: ignore[attr-defined,no-any-return]

        def flush(self) -> None:
            self.stream.flush()  # type: ignore[attr-defined]

    host = supervise.RealHost(
        project_root=tmp_path,
        controller_label="bugs",
        lease_nonce="nonce-1",
        nonce_reader=lambda: "nonce-1",
    )
    original_stdout = sys.stdout
    reader_fd, writer_fd = os.pipe()
    peer_stdout = os.fdopen(writer_fd, "w", buffering=1)
    wrapped_stdout = InvalidatedStdout(peer_stdout)
    monkeypatch.setattr(sys, "stdout", wrapped_stdout)
    assert supervise._stdout_is_regular_file(wrapped_stdout) is None
    wrapped_stdout.valid_fd = False

    started = time.monotonic()
    stdout_text = ""
    try:
        code = supervise.run_supervisor(
            project_root=tmp_path,
            controller_label="bugs",
            lease_nonce="nonce-1",
            host=host,
            heartbeat_s=supervise.DEFAULT_SUPERVISOR_HEARTBEAT_S,
            coverage_s=supervise.DEFAULT_SUPERVISOR_HEARTBEAT_S,
            items=[
                (
                    "backup",
                    _python_child("import time; time.sleep(5)"),
                )
            ],
        )
        elapsed = time.monotonic() - started
    finally:
        monkeypatch.setattr(sys, "stdout", original_stdout)
        peer_stdout.close()
        stdout_text = os.read(reader_fd, 65536).decode("utf-8")
        os.close(reader_fd)
        host.kill_all()

    assert elapsed < 1.0
    assert code == supervise.SUPERVISE_STOP_EXIT
    assert host._stdout_detector_failure == (
        "stdout file descriptor registration failed"
    )
    assert not any(child.alive for child in host._children)
    records = [json.loads(line) for line in stdout_text.splitlines()]
    stop = next(record for record in records if record.get("type") == "stop")
    assert stop["reason"] == "stdout-peer-detector-unavailable"
    assert stop["scope"] == "supervisor"
    assert stop["detector"] == "registration"
    assert stop["error"] == "file-descriptor-registration-failed"
    assert "goalflight_messages.py supervise" in stop["rearm"]
    assert (
        "stdout peer-gone detector unavailable; stopping: "
        "registration: stdout file descriptor registration failed"
    ) in capsys.readouterr().err


@pytest.mark.parametrize("text", ["¢", "€", "😀"])
def test_utf8_completion_finishes_each_multibyte_split(text: str) -> None:
    data = text.encode("utf-8")
    for offset in range(1, len(data)):
        completed = data[:offset] + supervise._utf8_completion(data, offset)
        assert completed.decode("utf-8") == text


def test_full_nonblocking_pipe_eagain_retries_then_keeps_children_alive(
    capsys: pytest.CaptureFixture[str],
) -> None:
    reader_fd, writer_fd = os.pipe()
    os.set_blocking(writer_fd, False)
    while True:
        try:
            os.write(writer_fd, b"x" * 4096)
        except BlockingIOError as exc:
            assert exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}
            break

    class BackpressuredHost(FakeHost):
        actual_errnos: list[int] = []
        children_alive_after_retry = False

        def write_stdout(self, line: str) -> bool:
            try:
                os.write(writer_fd, line.encode("utf-8"))
            except BlockingIOError as exc:
                self.actual_errnos.append(int(exc.errno))
                os.read(reader_fd, 65536)
                raise
            self.children_alive_after_retry = bool(self.children) and all(
                child.alive for child in self.children
            )
            return super().write_stdout(line)

    host = BackpressuredHost(stop_after_coverage=1)
    try:
        code = _run(host, _items("stream"))
    finally:
        os.close(writer_fd)
        os.close(reader_fd)

    assert code == 0
    assert host.actual_errnos == [errno.EAGAIN]
    assert host.children_alive_after_retry
    assert not any(
        record.get("type") == "stop" for record in _records(host)
    )
    status = host.stdout_detector_status()
    assert status.availability == "available"
    assert status.failure is None
    assert capsys.readouterr().err == ""


def test_partial_nonblocking_write_resumes_without_duplicate_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader_fd, writer_fd = os.pipe()
    os.set_blocking(writer_fd, False)
    while True:
        try:
            os.write(writer_fd, b"x" * 4096)
        except BlockingIOError as exc:
            assert exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}
            break
    os.read(reader_fd, 4096)
    peer_stdout = os.fdopen(writer_fd, "w", buffering=1)
    original_stdout = sys.stdout
    monkeypatch.setattr(sys, "stdout", peer_stdout)
    relieved = bytearray()
    relief_calls: list[float] = []

    def relieve_pipe(delay_s: float) -> None:
        relief_calls.append(delay_s)
        relieved.extend(os.read(reader_fd, 4096))

    monkeypatch.setattr(supervise.time, "sleep", relieve_pipe)
    host = supervise.RealHost(
        project_root=tmp_path,
        controller_label="bugs",
        lease_nonce="nonce-1",
        nonce_reader=lambda: "nonce-1",
    )
    line = "a" * 25000
    try:
        assert supervise._write_stdout(host, line, source="write-child-output")
    finally:
        monkeypatch.setattr(sys, "stdout", original_stdout)
        peer_stdout.close()
        host.kill_all()
    remaining = bytearray()
    while True:
        chunk = os.read(reader_fd, 65536)
        if not chunk:
            break
        remaining.extend(chunk)
    os.close(reader_fd)

    forwarded = bytes(relieved + remaining)
    assert len(relief_calls) > supervise.TRANSIENT_DETECTOR_FAILURE_LIMIT
    assert set(relief_calls) == {supervise.TRANSIENT_DETECTOR_RETRY_S}
    assert forwarded.count(b"a") == len(line)
    assert forwarded.endswith(b"\n")
    assert host._stdout_pending is None
    assert host.stdout_detector_status().failure is None


def test_failed_recovery_stop_write_escalates_rearm_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    reader_fd, writer_fd = os.pipe()
    os.set_blocking(writer_fd, False)

    class FailedStopHost(FakeHost):
        pipe_filled = False
        actual_errnos: list[int] = []

        def write_stdout(self, line: str) -> bool:
            try:
                os.write(writer_fd, line.encode("utf-8"))
            except BlockingIOError as exc:
                self.actual_errnos.append(int(exc.errno))
                raise
            return super().write_stdout(line)

        def stdio_peer_gone(self) -> bool:
            if not self.pipe_filled:
                self.report_stdout_detector(
                    "poll",
                    "unavailable",
                    "stdout poll failed: ENOMEM: out of memory",
                    "ENOMEM",
                )
                while True:
                    try:
                        os.write(writer_fd, b"x" * 4096)
                    except BlockingIOError as exc:
                        assert exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}
                        break
                self.pipe_filled = True
            return False

    host = FailedStopHost()
    try:
        code = _run(host, _items("stream"))
    finally:
        os.close(writer_fd)
        os.close(reader_fd)

    assert code == supervise.SUPERVISE_STOP_EXIT
    assert host.actual_errnos == [errno.EAGAIN] * (
        supervise.TRANSIENT_DETECTOR_FAILURE_LIMIT
    )
    assert not any(child.alive for child in host.children)
    assert not any(
        record.get("type") == "stop" for record in _records(host)
    )
    error = capsys.readouterr().err
    assert "recovery record could not be written to stdout" in error
    assert "stop: stdout-peer-detector-unavailable" in error
    assert "re-arm with:" in error
    assert "goalflight_messages.py supervise" in error


def test_real_host_failed_write_clears_pending_before_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reader_fd, writer_fd = os.pipe()
    os.set_blocking(writer_fd, False)
    while True:
        try:
            os.write(writer_fd, b"x" * 4096)
        except BlockingIOError as exc:
            assert exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}
            break
    peer_stdout = os.fdopen(writer_fd, "w", buffering=1)
    original_stdout = sys.stdout
    monkeypatch.setattr(sys, "stdout", peer_stdout)
    host = supervise.RealHost(
        project_root=tmp_path,
        controller_label="bugs",
        lease_nonce="nonce-1",
        nonce_reader=lambda: "nonce-1",
    )
    try:
        code = supervise.run_supervisor(
            project_root=tmp_path,
            controller_label="bugs",
            lease_nonce="nonce-1",
            host=host,
            items=[
                (
                    "backup",
                    _python_child("import time; time.sleep(5)"),
                )
            ],
        )
    finally:
        monkeypatch.setattr(sys, "stdout", original_stdout)
        peer_stdout.close()
        os.close(reader_fd)
        host.kill_all()

    assert code == supervise.SUPERVISE_STOP_EXIT
    assert host._stdout_pending is None
    assert not any(child.alive for child in host._children)
    error = capsys.readouterr().err
    assert "recovery record could not be written to stdout" in error
    assert "re-arm with:" in error
    assert "Traceback" not in error


def test_abandoned_partial_write_delimits_next_recovery_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader_fd, writer_fd = os.pipe()
    os.set_blocking(writer_fd, False)
    while True:
        try:
            os.write(writer_fd, b"x" * 4096)
        except BlockingIOError:
            break
    os.read(reader_fd, 4096)
    peer_stdout = os.fdopen(writer_fd, "w", buffering=1)
    original_stdout = sys.stdout
    monkeypatch.setattr(sys, "stdout", peer_stdout)
    monkeypatch.setattr(supervise.time, "sleep", lambda _delay_s: None)
    host = supervise.RealHost(
        project_root=tmp_path,
        controller_label="bugs",
        lease_nonce="nonce-1",
        nonce_reader=lambda: "nonce-1",
    )
    wire = bytearray()
    try:
        assert not supervise._write_stdout(
            host,
            "a" * 4095 + "€" + "b" * 20000,
            source="write-child-output",
        )
        assert host._stdout_pending is None
        assert host._stdout_needs_delimiter
        assert host._stdout_recovery_completion == "€".encode("utf-8")[1:]
        os.set_blocking(reader_fd, False)
        try:
            while True:
                wire.extend(os.read(reader_fd, 65536))
        except BlockingIOError:
            pass
        finally:
            os.set_blocking(reader_fd, True)
        assert wire.endswith("€".encode("utf-8")[:1])
        assert supervise._emit(
            host,
            {
                "kind": "supervise",
                "type": "stop",
                "reason": "stdout-peer-detector-unavailable",
                "rearm": "python3 goalflight_messages.py supervise",
            },
        )
    finally:
        monkeypatch.setattr(sys, "stdout", original_stdout)
        peer_stdout.close()
        host.kill_all()
    while True:
        chunk = os.read(reader_fd, 65536)
        if not chunk:
            break
        wire.extend(chunk)
    os.close(reader_fd)

    decoded = wire.decode("utf-8")
    assert "€\n{" in decoded
    records = [
        json.loads(line)
        for line in decoded.splitlines()
        if line.startswith("{")
    ]
    assert len(records) == 1
    assert records[0]["type"] == "stop"
    assert "goalflight_messages.py supervise" in records[0]["rearm"]
    assert not host._stdout_needs_delimiter


def test_real_host_retries_transient_poll_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_poll = supervise.select.poll
    calls: list[int | None] = []
    observed_errnos: list[int] = []
    libc = ctypes.CDLL(None, use_errno=True)

    class InterruptThreePoll:
        def __init__(self) -> None:
            self.inner = real_poll()

        def register(self, fd: int, eventmask: int) -> None:
            self.inner.register(fd, eventmask)

        def poll(self, timeout: int | None = None) -> list[tuple[int, int]]:
            calls.append(timeout)
            if len(calls) <= 3:
                ctypes.set_errno(0)
                signal.setitimer(signal.ITIMER_REAL, 0.01)
                try:
                    result = libc.poll(None, 0, 1000)
                    actual_errno = ctypes.get_errno()
                finally:
                    signal.setitimer(signal.ITIMER_REAL, 0)
                assert result == -1
                assert actual_errno == errno.EINTR
                observed_errnos.append(actual_errno)
                raise OSError(actual_errno, os.strerror(actual_errno))
            return self.inner.poll(timeout)

    class StopAfterSuccessfulPollHost(supervise.RealHost):
        children_alive_after_poll = False

        def wait(
            self,
            children: list[object],
            timeout_s: float,
        ) -> supervise.WaitResult:
            result = super().wait(children, timeout_s)
            self.children_alive_after_poll = bool(children) and all(
                getattr(child, "alive", False) for child in children
            )
            self._stop = True
            return result

    monkeypatch.setattr(supervise.select, "poll", InterruptThreePoll)
    monkeypatch.setattr(messages, "_stdio_peer_gone", lambda _stream: False)
    reader_fd, writer_fd = os.pipe()
    peer_stdout = os.fdopen(writer_fd, "w", buffering=1)
    original_stdout = sys.stdout
    monkeypatch.setattr(sys, "stdout", peer_stdout)
    old_alarm_handler = signal.signal(signal.SIGALRM, lambda *_args: None)
    old_alarm_timer = signal.getitimer(signal.ITIMER_REAL)
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.siginterrupt(signal.SIGALRM, True)
    host = StopAfterSuccessfulPollHost(
        project_root=tmp_path,
        controller_label="bugs",
        lease_nonce="nonce-1",
        nonce_reader=lambda: "nonce-1",
    )
    try:
        code = supervise.run_supervisor(
            project_root=tmp_path,
            controller_label="bugs",
            lease_nonce="nonce-1",
            host=host,
            heartbeat_s=0.01,
            coverage_s=0.01,
            items=[
                (
                    "backup",
                    _python_child("import time; time.sleep(5)"),
                )
            ],
        )
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_alarm_handler)
        signal.setitimer(signal.ITIMER_REAL, *old_alarm_timer)
        monkeypatch.setattr(sys, "stdout", original_stdout)
        peer_stdout.close()
        os.close(reader_fd)
        host.kill_all()

    assert code == 0
    assert observed_errnos == [errno.EINTR] * 3
    assert len(calls) == 4
    assert host.children_alive_after_poll
    status = host.stdout_detector_status()
    assert status.availability == "available"
    assert status.failure is None
    assert not status.peer_gone


@pytest.mark.parametrize(
    ("poll_errno", "expected_calls"),
    [
        (errno.ENOMEM, 1),
        (errno.EINVAL, 1),
        (errno.EAGAIN, supervise.TRANSIENT_DETECTOR_FAILURE_LIMIT),
    ],
)
def test_real_host_persistent_poll_failure_emits_recovery_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    poll_errno: int,
    expected_calls: int,
) -> None:
    real_poll = supervise.select.poll
    calls: list[int | None] = []

    class PersistentlyFailingPoll:
        def __init__(self) -> None:
            self.inner = real_poll()

        def register(self, fd: int, eventmask: int) -> None:
            self.inner.register(fd, eventmask)

        def poll(self, timeout: int | None = None) -> list[tuple[int, int]]:
            calls.append(timeout)
            raise OSError(poll_errno, os.strerror(poll_errno))

    monkeypatch.setattr(supervise.select, "poll", PersistentlyFailingPoll)
    reader_fd, writer_fd = os.pipe()
    peer_stdout = os.fdopen(writer_fd, "w", buffering=1)
    original_stdout = sys.stdout
    monkeypatch.setattr(sys, "stdout", peer_stdout)
    host = _RecordingHost(
        project_root=tmp_path,
        controller_label="bugs",
        lease_nonce="nonce-1",
        nonce_reader=lambda: "nonce-1",
    )
    started = time.monotonic()
    try:
        code = supervise.run_supervisor(
            project_root=tmp_path,
            controller_label="bugs",
            lease_nonce="nonce-1",
            host=host,
            heartbeat_s=supervise.DEFAULT_SUPERVISOR_HEARTBEAT_S,
            coverage_s=supervise.DEFAULT_SUPERVISOR_HEARTBEAT_S,
            items=[
                (
                    "backup",
                    _python_child("import time; time.sleep(5)"),
                )
            ],
        )
        elapsed = time.monotonic() - started
    finally:
        monkeypatch.setattr(sys, "stdout", original_stdout)
        peer_stdout.close()
        os.close(reader_fd)
        host.kill_all()

    assert elapsed < 1.0
    supervisor_calls = [timeout for timeout in calls if timeout and timeout > 1000]
    assert len(supervisor_calls) == expected_calls
    assert code == supervise.SUPERVISE_STOP_EXIT
    status = host.stdout_detector_status()
    assert status.availability == "unavailable"
    assert status.failure is not None
    assert status.failure.source == "poll"
    assert errno.errorcode[poll_errno] in status.failure.detail
    stop = next(
        record for record in _records(host) if record.get("type") == "stop"
    )
    assert stop["reason"] == "stdout-peer-detector-unavailable"
    assert stop["detector"] == "poll"
    assert stop["error"] == errno.errorcode[poll_errno]
    assert "goalflight_messages.py supervise" in stop["rearm"]
    if poll_errno == errno.EAGAIN:
        assert (
            f"{supervise.TRANSIENT_DETECTOR_FAILURE_LIMIT} consecutive EAGAIN"
            in str(status.failure.detail)
        )


def test_pollnval_preempts_ready_child_output_before_forwarding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader_fd, writer_fd = os.pipe()
    peer_stdout = os.fdopen(writer_fd, "w", buffering=1)
    original_stdout = sys.stdout
    monkeypatch.setattr(sys, "stdout", peer_stdout)

    class InvalidatingHost(supervise.RealHost):
        wait_lines: list[str] = []

        def wait(
            self,
            children: list[object],
            timeout_s: float,
        ) -> supervise.WaitResult:
            child = children[0]
            child_stdout = child.popen.stdout  # type: ignore[attr-defined]
            assert child_stdout is not None
            readable, _writable, _exceptional = select.select(
                [child_stdout.fileno()], [], [], 1.0
            )
            assert readable
            os.close(writer_fd)
            result = super().wait(children, timeout_s)
            self.wait_lines = [line for _child, line in result.lines]
            return result

    host = InvalidatingHost(
        project_root=tmp_path,
        controller_label="bugs",
        lease_nonce="nonce-1",
        nonce_reader=lambda: "nonce-1",
    )
    child_alive_after_run: list[bool] = []
    signal_fds_after_run: tuple[int | None, int | None] = (-1, -1)
    try:
        code = supervise.run_supervisor(
            project_root=tmp_path,
            controller_label="bugs",
            lease_nonce="nonce-1",
            host=host,
            heartbeat_s=supervise.DEFAULT_SUPERVISOR_HEARTBEAT_S,
            coverage_s=supervise.DEFAULT_SUPERVISOR_HEARTBEAT_S,
            items=[
                (
                    "backup",
                    _python_child(
                        "import os, time; "
                        "os.write(1, b'ready\\n'); time.sleep(5)"
                    ),
                )
            ],
        )
        child_alive_after_run = [child.alive for child in host._children]
        signal_fds_after_run = (host._signal_rfd, host._signal_wfd)
    finally:
        monkeypatch.setattr(sys, "stdout", original_stdout)
        try:
            peer_stdout.close()
        except OSError:
            pass
        os.close(reader_fd)
        host.kill_all()

    assert code == 0
    assert host.wait_lines == ["ready"]
    assert child_alive_after_run and not any(child_alive_after_run)
    assert signal_fds_after_run == (None, None)


def _python_child(script: str) -> str:
    return shlex.join([sys.executable, "-c", script])


def _follow_child_command(
    project: Path,
    lease: journal.LeaseIdentity,
) -> str:
    return shlex.join(
        [
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
            "0.12",
            "--frontier-floor-secs",
            "30",
        ]
    )


def _stored_task(item_id: str, title: str, **extra: object) -> dict[str, object]:
    item: dict[str, object] = {
        "schema_version": 1,
        "id": item_id,
        "kind": "task",
        "title": title,
        "blocked_by": [],
        "links": [],
        "done": False,
        "created_at": "2026-08-26T00:00:00+00:00",
        "created_by": "test",
    }
    item.update(extra)
    return item


def test_messages_supervise_wires_the_forwarding_only_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = task.TaskStore(tmp_path)
    store.save_items_atomic(
        [
            _stored_task(
                "t-wired-working",
                "Wired working item",
                dispatches=[
                    {
                        "dispatch_id": "wired-working-child",
                        "state": "working",
                        "ts": "2026-08-26T00:01:00+00:00",
                    }
                ],
            )
        ]
    )
    captured: dict[str, object] = {}

    def fake_cmd_supervise(args: object, **kwargs: object) -> int:
        captured.update(kwargs)
        return 17

    monkeypatch.setattr(supervise, "cmd_supervise", fake_cmd_supervise)
    assert messages.cmd_supervise(SimpleNamespace(project_root=str(tmp_path))) == 17
    forwarding_frontier = captured["forwarding_frontier"]
    assert callable(forwarding_frontier)
    record = forwarding_frontier(tmp_path)
    assert record["payload"]["state"] == "working"
    assert record["payload"]["id"] == "t-wired-working"


class _ActionWakeHost(_RecordingHost):
    def __init__(self, *args: object, next_target: int, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.next_target = next_target

    def write_stdout(self, line: str) -> bool:
        alive = super().write_stdout(line)
        joined = "".join(self.lines)
        next_count = sum(
            1
            for record in _records(self)  # type: ignore[arg-type]
            if record.get("kind") == "next"
        )
        if (
            next_count >= self.next_target
            and "heartbeat quoted in a stream event" in joined
            and "heartbeat quoted in a mail headline" in joined
        ):
            self._stop = True
        return alive


class _NextCountHost(_RecordingHost):
    def __init__(self, *args: object, next_target: int, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.next_target = next_target

    def write_stdout(self, line: str) -> bool:
        alive = super().write_stdout(line)
        records = _records(self)  # type: ignore[arg-type]
        if sum(record.get("kind") == "next" for record in records) >= self.next_target:
            self._stop = True
        return alive


class _ChattyRecordHost(_RecordingHost):
    def write_stdout(self, line: str) -> bool:
        alive = super().write_stdout(line)
        records = _records(self)  # type: ignore[arg-type]
        if any(record.get("kind") == "heartbeat" for record in records) and any(
            record.get("kind") == "frontier" for record in records
        ):
            self._stop = True
        return alive


def test_actual_follow_child_emits_one_actionable_wake_per_idle_beat(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive follow's real computation and stdout contract through RealHost."""
    project, env, lease = isolated
    monkeypatch.setattr(supervise.wake, "live_waiters", lambda *args, **kwargs: [])
    task.TaskStore(project).save_items_atomic([])
    command = _follow_child_command(project, lease)
    host = _NextCountHost(
        project_root=project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        env=env,
        nonce_reader=lambda: lease.nonce,
        next_target=3,
    )
    with wake.register_lease_holder(
        project, controller_label=lease.label, lease_nonce=lease.nonce
    ):
        try:
            code = supervise.run_supervisor(
                project_root=project,
                controller_label=lease.label,
                lease_nonce=lease.nonce,
                host=host,
                heartbeat_s=100.0,
                coverage_s=100.0,
                items=[("stream", command)],
            )
        finally:
            host.kill_all()

    records = _records(host)  # type: ignore[arg-type]
    actionable = [record for record in records if record.get("kind") == "next"]
    assert code == 0
    assert len(actionable) == 3
    assert [record["payload"]["state"] for record in actionable] == [
        "empty",
        "unknown",
        "unknown",
    ]
    assert actionable[0]["payload"]["directive"] == "Nothing pending"
    assert all(
        record["payload"]["directive"] == "goal-flight next"
        for record in actionable[1:]
    )
    assert not [record for record in records if record.get("kind") == "heartbeat"]
    assert not [record for record in records if record.get("kind") == "frontier"]


@pytest.mark.parametrize(
    ("projection", "expected_state", "expected_id"),
    [
        ("projected", "projected", "t-real-projected"),
        ("malformed", "unavailable", None),
    ],
)
def test_actual_follow_computes_projected_and_unavailable_directives(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    projection: str,
    expected_state: str,
    expected_id: str | None,
) -> None:
    """Classification comes from cmd_follow, not a scripted frontier record."""
    project, env, lease = isolated
    monkeypatch.setattr(supervise.wake, "live_waiters", lambda *args, **kwargs: [])
    store = task.TaskStore(project)
    if projection == "projected":
        store.save_items_atomic(
            [_stored_task("t-real-projected", "Real projected frontier")]
        )
    else:
        store.save_items_atomic([])
        store.data_js_path.write_text("window.GF_ITEMS = {malformed\n", encoding="utf-8")

    host = _NextCountHost(
        project_root=project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        env=env,
        nonce_reader=lambda: lease.nonce,
        next_target=1,
    )
    with wake.register_lease_holder(
        project, controller_label=lease.label, lease_nonce=lease.nonce
    ):
        try:
            code = supervise.run_supervisor(
                project_root=project,
                controller_label=lease.label,
                lease_nonce=lease.nonce,
                host=host,
                heartbeat_s=100.0,
                coverage_s=100.0,
                items=[("stream", _follow_child_command(project, lease))],
            )
        finally:
            host.kill_all()

    records = _records(host)  # type: ignore[arg-type]
    actionable = [record for record in records if record.get("kind") == "next"]
    assert code == 0
    assert len(actionable) == 1
    assert actionable[0]["payload"]["state"] == expected_state
    assert actionable[0]["payload"]["directive"] == "goal-flight next"
    assert actionable[0]["payload"].get("id") == expected_id
    assert not [record for record in records if record.get("kind") == "heartbeat"]
    assert not [record for record in records if record.get("kind") == "frontier"]


def test_actual_follow_keeps_working_item_empty_but_supervisor_forwards_it(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, env, lease = isolated
    monkeypatch.setattr(supervise.wake, "live_waiters", lambda *args, **kwargs: [])
    store = task.TaskStore(project)
    store.save_items_atomic(
        [
            _stored_task(
                "t-real-working",
                "Real working item",
                dispatches=[
                    {
                        "dispatch_id": "real-working-child",
                        "state": "working",
                        "ts": "2026-08-26T00:01:00+00:00",
                    }
                ],
            )
        ]
    )
    command = _follow_child_command(project, lease)

    chatty_host = _ChattyRecordHost(
        project_root=project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        env=env,
        nonce_reader=lambda: lease.nonce,
    )
    with wake.register_lease_holder(
        project, controller_label=lease.label, lease_nonce=lease.nonce
    ):
        try:
            chatty_code = supervise.run_supervisor(
                project_root=project,
                controller_label=lease.label,
                lease_nonce=lease.nonce,
                host=chatty_host,
                heartbeat_s=100.0,
                coverage_s=100.0,
                items=[("stream", command)],
                chatty=True,
            )
        finally:
            chatty_host.kill_all()

    child_records = _records(chatty_host)  # type: ignore[arg-type]
    child_frontier = next(
        record for record in child_records if record.get("kind") == "frontier"
    )
    assert chatty_code == 0
    assert child_frontier["payload"]["state"] == "empty"
    assert "id" not in child_frontier["payload"]

    default_host = _NextCountHost(
        project_root=project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        env=env,
        nonce_reader=lambda: lease.nonce,
        next_target=1,
    )
    with wake.register_lease_holder(
        project, controller_label=lease.label, lease_nonce=lease.nonce
    ):
        try:
            default_code = supervise.run_supervisor(
                project_root=project,
                controller_label=lease.label,
                lease_nonce=lease.nonce,
                host=default_host,
                heartbeat_s=100.0,
                coverage_s=100.0,
                items=[("stream", command)],
                forwarding_frontier=lambda: messages._supervisor_frontier_snapshot(
                    store
                ),
            )
        finally:
            default_host.kill_all()

    forwarded = next(
        record
        for record in _records(default_host)  # type: ignore[arg-type]
        if record.get("kind") == "next"
    )
    assert default_code == 0
    assert forwarded["payload"] == {
        "directive": "goal-flight next",
        "id": "t-real-working",
        "state": "working",
        "title": "Real working item",
    }


def test_forwarding_projection_refreshes_after_active_only_transition(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, env, lease = isolated
    monkeypatch.setattr(supervise.wake, "live_waiters", lambda *args, **kwargs: [])
    store = task.TaskStore(project)
    store.save_items_atomic(
        [
            _stored_task(
                "t-transitioning",
                "Transitioning item",
                dispatches=[
                    {
                        "dispatch_id": "transitioning-child",
                        "state": "working",
                        "ts": "2026-08-26T00:01:00+00:00",
                    }
                ],
            )
        ]
    )
    working_record = messages._supervisor_frontier_snapshot(store)
    store.save_items_atomic(
        [
            _stored_task(
                "t-transitioning",
                "Transitioning item",
                done=True,
                done_reviewed=True,
            )
        ]
    )
    empty_record = messages._supervisor_frontier_snapshot(store)
    snapshots = iter((working_record, empty_record))
    calls = 0

    def forwarding_frontier() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return next(snapshots)

    stream_script = (
        "import time\n"
        "print('{\"kind\":\"armed\"}', flush=True)\n"
        "print('{\"kind\":\"heartbeat\",\"payload\":{\"seq\":1}}', flush=True)\n"
        "time.sleep(0.05)\n"
        "print('{\"kind\":\"frontier\",\"payload\":{\"state\":\"empty\"}}', flush=True)\n"
        "time.sleep(0.20)\n"
        "print('{\"kind\":\"heartbeat\",\"payload\":{\"seq\":2}}', flush=True)\n"
        "time.sleep(3)\n"
    )
    host = _NextCountHost(
        project_root=project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        env=env,
        nonce_reader=lambda: lease.nonce,
        next_target=2,
    )
    with wake.register_lease_holder(
        project, controller_label=lease.label, lease_nonce=lease.nonce
    ):
        try:
            code = supervise.run_supervisor(
                project_root=project,
                controller_label=lease.label,
                lease_nonce=lease.nonce,
                host=host,
                heartbeat_s=100.0,
                coverage_s=100.0,
                items=[("stream", _python_child(stream_script))],
                forwarding_frontier=forwarding_frontier,
            )
        finally:
            host.kill_all()

    actionable = [
        record
        for record in _records(host)  # type: ignore[arg-type]
        if record.get("kind") == "next"
    ]
    assert code == 0
    assert calls == 2
    assert [record["payload"]["state"] for record in actionable] == [
        "working",
        "empty",
    ]
    assert actionable[0]["payload"]["id"] == "t-transitioning"
    assert actionable[1]["payload"] == {
        "directive": "Nothing pending",
        "state": "empty",
    }


def test_forwarding_projection_read_cannot_exceed_the_grace_deadline(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, env, lease = isolated
    monkeypatch.setattr(supervise.wake, "live_waiters", lambda *args, **kwargs: [])
    release = threading.Event()

    def blocked_frontier() -> dict[str, object]:
        release.wait(4.0)
        return {"kind": "frontier", "payload": {"state": "empty"}}

    stream_script = (
        "import time\n"
        "print('{\"kind\":\"armed\"}', flush=True)\n"
        "print('{\"kind\":\"heartbeat\",\"payload\":{\"seq\":1}}', flush=True)\n"
        "time.sleep(0.05)\n"
        "print('{\"kind\":\"frontier\",\"payload\":{\"state\":\"empty\"}}', flush=True)\n"
        "time.sleep(4)\n"
    )
    host = _NextCountHost(
        project_root=project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        env=env,
        nonce_reader=lambda: lease.nonce,
        next_target=1,
    )
    started = time.monotonic()
    with wake.register_lease_holder(
        project, controller_label=lease.label, lease_nonce=lease.nonce
    ):
        try:
            code = supervise.run_supervisor(
                project_root=project,
                controller_label=lease.label,
                lease_nonce=lease.nonce,
                host=host,
                heartbeat_s=100.0,
                coverage_s=100.0,
                items=[("stream", _python_child(stream_script))],
                forwarding_frontier=blocked_frontier,
            )
        finally:
            host.kill_all()
            release.set()
    elapsed = time.monotonic() - started

    actionable = next(
        record
        for record in _records(host)  # type: ignore[arg-type]
        if record.get("kind") == "next"
    )
    assert code == 0
    assert elapsed < supervise.STREAM_FRONTIER_GRACE_S + 1.5
    assert actionable["payload"] == {
        "directive": "goal-flight next",
        "state": "unknown",
    }


def test_subprocess_stream_pairs_keep_timing_and_quoted_heartbeats_structural(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scripted classifications isolate batching and structural routing."""
    project, env, lease = isolated
    monkeypatch.setattr(supervise.wake, "live_waiters", lambda *args, **kwargs: [])
    stream_records = [
        {"kind": "heartbeat", "payload": {"interval_s": 120.0, "seq": 1}},
        {
            "kind": "frontier",
            "payload": {
                "advisory": "information-only",
                "id": "t-001",
                "state": "projected",
                "title": "First frontier",
            },
        },
        {
            "kind": "event",
            "payload": {
                "data": {"subject": "heartbeat quoted in a stream event"},
                "type": "controller-notice",
            },
        },
        {"kind": "heartbeat", "payload": {"interval_s": 120.0, "seq": 2}},
        {
            "kind": "frontier",
            "payload": {
                "advisory": "information-only",
                "id": "t-002",
                "state": "projected",
                "title": "Changed frontier",
            },
        },
        {"kind": "heartbeat", "payload": {"interval_s": 120.0, "seq": 3}},
        {"kind": "heartbeat", "payload": {"interval_s": 120.0, "seq": 4}},
        {
            "kind": "frontier",
            "payload": {"advisory": "information-only", "state": "empty"},
        },
    ]
    stream_lines = [
        json.dumps(record, sort_keys=True, separators=(",", ":"))
        for record in stream_records
    ]
    stream_script = (
        "import sys,time\n"
        "print('{\"kind\":\"armed\"}', flush=True)\n"
        f"lines={stream_lines!r}\n"
        "for index,line in enumerate(lines):\n"
        " print(line, flush=True)\n"
        " time.sleep(0.03 if index not in {0, 3, 6} else 0.01)\n"
        "time.sleep(2)\n"
    )
    backup_script = (
        "import time\n"
        "print('{\"kind\":\"armed\"}', flush=True)\n"
        "print('FROM worker subject=heartbeat quoted in a mail headline', flush=True)\n"
        "time.sleep(2)\n"
    )
    host = _ActionWakeHost(
        project_root=project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        env=env,
        nonce_reader=lambda: lease.nonce,
        next_target=4,
    )
    try:
        code = supervise.run_supervisor(
            project_root=project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
            host=host,
            heartbeat_s=100.0,
            coverage_s=100.0,
            items=[
                ("stream", _python_child(stream_script)),
                ("backup", _python_child(backup_script)),
            ],
        )
    finally:
        host.kill_all()

    records = _records(host)  # type: ignore[arg-type]
    actionable = [record for record in records if record.get("kind") == "next"]
    assert code == 0
    assert len(actionable) == 4
    assert [record["payload"].get("id") for record in actionable] == [
        "t-001",
        "t-002",
        "t-002",
        None,
    ]
    assert actionable[-1]["payload"] == {
        "directive": "Nothing pending",
        "state": "empty",
    }
    assert not [record for record in records if record.get("kind") == "heartbeat"]
    assert any(
        record.get("kind") == "event"
        and "heartbeat quoted in a stream event" in json.dumps(record)
        for record in records
    )
    joined = "".join(host.lines)
    assert "heartbeat quoted in a mail headline" in joined
    assert not any(
        record.get("kind") == "supervise" and record.get("type") == "heartbeat"
        for record in records
    )


def test_chatty_restores_real_stream_keepalive_and_frontier(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, env, lease = isolated
    monkeypatch.setattr(supervise.wake, "live_waiters", lambda *args, **kwargs: [])
    command = _follow_child_command(project, lease)
    host = _ChattyRecordHost(
        project_root=project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        env=env,
        nonce_reader=lambda: lease.nonce,
    )
    with wake.register_lease_holder(
        project, controller_label=lease.label, lease_nonce=lease.nonce
    ):
        try:
            code = supervise.run_supervisor(
                project_root=project,
                controller_label=lease.label,
                lease_nonce=lease.nonce,
                host=host,
                heartbeat_s=100.0,
                coverage_s=100.0,
                items=[("stream", command)],
                chatty=True,
            )
        finally:
            host.kill_all()

    records = _records(host)  # type: ignore[arg-type]
    assert code != supervise.SUPERVISE_STOP_EXIT
    assert sum(record.get("kind") == "heartbeat" for record in records) == 1
    assert sum(record.get("kind") == "frontier" for record in records) == 1
    assert not any(record.get("kind") == "next" for record in records)


def test_open_reader_success_live_session_none_does_not_kill_the_pool(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reviewer refutation: reader succeeds, live_session is None, pool stays up.

    Exercises RealHost.nonce_probe, not a FakeHost that hands back the state.
    """
    project, env, lease = isolated
    opened: list[object] = []
    real_open_reader = journal.Journal.open_reader

    def succeeding_open_reader(cls, project_root, **kwargs):  # type: ignore[no-untyped-def]
        reader = real_open_reader(project_root, **kwargs)
        opened.append(reader)
        return reader

    monkeypatch.setattr(
        journal.Journal, "open_reader", classmethod(succeeding_open_reader)
    )
    monkeypatch.setattr(sessions, "live_session", lambda *args, **kwargs: None)

    def busy_journal_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise journal.JournalUnavailable(
            "journal connection remained busy after 1 attempts within 1.000s"
        )

    monkeypatch.setattr(journal.Journal, "__init__", busy_journal_init)

    with wake.register_lease_holder(
        project, controller_label=lease.label, lease_nonce=lease.nonce
    ):
        host = _RecordingHost(
            project_root=project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
            env=env,
        )
        try:
            assert sessions.live_session(project, label=lease.label) is None
            assert host.nonce_probe() == "live"
            assert opened, "nonce_probe must read through open_reader"
            script = (
                "import sys\n"
                "print('listen: journal-unavailable: journal is busy', "
                "file=sys.stderr)\n"
                "sys.exit(2)\n"
            )
            code = supervise.run_supervisor(
                project_root=project,
                controller_label=lease.label,
                lease_nonce=lease.nonce,
                host=host,
                heartbeat_s=100.0,
                coverage_s=100.0,
                items=[("backup", _python_child(script))],
            )
        finally:
            host.kill_all()

    records = _records(host)
    reasons = [record.get("reason") for record in records]
    assert "dead-lease-nonce" not in reasons
    assert code != supervise.SUPERVISE_STOP_EXIT
    restart = next(record for record in records if record.get("type") == "restart")
    assert restart["reason"] == "journal-unreadable"
    assert restart["child"] == "backup"


def test_fast_ring_between_lock_samples_is_rearmed_not_stopped(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Child arms, rings, and exits 0 between lock samples must re-arm."""
    project, env, lease = isolated
    monkeypatch.setattr(supervise.wake, "live_waiters", lambda *args, **kwargs: [])
    script = (
        "import sys\n"
        "print('{\"kind\":\"armed\"}', flush=True)\n"
        "print('{\"kind\":\"ring\",\"reason\":\"event\"}', flush=True)\n"
        "sys.exit(0)\n"
    )
    host = _RecordingHost(
        project_root=project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        env=env,
        nonce_reader=lambda: lease.nonce,
    )
    try:
        code = supervise.run_supervisor(
            project_root=project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
            host=host,
            heartbeat_s=100.0,
            coverage_s=100.0,
            items=[("backup", _python_child(script))],
        )
    finally:
        host.kill_all()

    records = _records(host)
    stops = [record for record in records if record.get("type") == "stop"]
    assert all(record.get("reason") != "did-not-arm" for record in stops)
    restart = next(record for record in records if record.get("type") == "restart")
    assert restart["reason"] == "rang"
    assert restart["child"] == "backup"
    assert code != supervise.SUPERVISE_STOP_EXIT
    assert any(
        child.armed
        for child in host._children
        if isinstance(child, supervise.RealChild)
    )


@pytest.mark.parametrize(
    ("token", "forbidden_reason"),
    [
        ("stale-lease", "dead-lease-nonce"),
        ("journal-unavailable", "journal-unreadable"),
        ("already has a live follow watchdog", "did-not-arm"),
    ],
)
def test_relayed_mail_headline_does_not_classify_a_successful_ring(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    token: str,
    forbidden_reason: str,
) -> None:
    """Reviewer refutation: a doorbell that reports a marker in a headline,
    then exits 0 on a normal ring, is still ``rang``. Classification must
    not read accumulated listen stdout.
    """
    project, env, lease = isolated
    monkeypatch.setattr(supervise.wake, "live_waiters", lambda *args, **kwargs: [])
    headline = f"[steer] t323 seq=1 — controller mentioned {token} in mail"
    armed_line = json.dumps({"kind": "armed"}, separators=(",", ":"))
    ring_line = json.dumps(
        {"kind": "ring", "reason": "event"}, separators=(",", ":")
    )
    script = (
        "import sys\n"
        f"print({armed_line!r}, flush=True)\n"
        f"print({headline!r}, flush=True)\n"
        f"print({ring_line!r}, flush=True)\n"
        "sys.exit(0)\n"
    )
    host = _RecordingHost(
        project_root=project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        env=env,
        nonce_reader=lambda: lease.nonce,
    )
    try:
        code = supervise.run_supervisor(
            project_root=project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
            host=host,
            heartbeat_s=100.0,
            coverage_s=100.0,
            items=[("backup", _python_child(script))],
        )
        diagnostics = [
            child.output
            for child in host._children
            if isinstance(child, supervise.RealChild)
        ]
    finally:
        host.kill_all()

    records = _records(host)
    reasons = [record.get("reason") for record in records]
    assert forbidden_reason not in reasons
    assert all(record.get("type") != "stop" for record in records)
    restart = next(record for record in records if record.get("type") == "restart")
    assert restart["reason"] == "rang"
    assert restart["child"] == "backup"
    assert code != supervise.SUPERVISE_STOP_EXIT
    assert any(token in (text or "") for text in "".join(host.lines).splitlines())
    assert all(token not in (blob or "") for blob in diagnostics)


def test_follow_listener_exit_json_on_stdout_is_still_dead_nonce(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    """Follow's own stale-lease diagnostic is stdout JSON, not stderr.

    A stderr-only scan would miss it; structured child-exit reasons must
    still stop the supervisor.
    """
    project, env, lease = isolated
    armed_line = json.dumps({"kind": "armed"}, separators=(",", ":"))
    exit_line = json.dumps(
        {
            "kind": "event",
            "payload": {"type": "listener-exit", "reason": "stale-lease"},
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    script = (
        "import sys\n"
        f"print({armed_line!r}, flush=True)\n"
        f"print({exit_line!r}, flush=True)\n"
        "sys.exit(3)\n"
    )
    host = _RecordingHost(
        project_root=project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        env=env,
        nonce_reader=lambda: lease.nonce,
    )
    try:
        code = supervise.run_supervisor(
            project_root=project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
            host=host,
            heartbeat_s=100.0,
            coverage_s=100.0,
            items=[("stream", _python_child(script))],
        )
    finally:
        host.kill_all()

    records = _records(host)
    stop = next(record for record in records if record.get("type") == "stop")
    assert stop["reason"] == "dead-lease-nonce"
    assert stop.get("scope") == "supervisor"
    assert code == supervise.SUPERVISE_STOP_EXIT


def test_bare_exit_5_stops_the_supervisor_not_one_slot() -> None:
    """Exit 5 with no diagnostic is still supervisor-wide never-armed."""
    host = FakeHost(
        scripts={
            "stream": [],
            "backup": [
                PlannedExit(
                    lifetime_s=0.1,
                    returncode=5,
                    output="",
                    armed=False,
                ),
            ],
        },
        stop_when_lines_contain=('"type":"stop"',),
    )
    code = _run(
        host,
        _items("stream", "backup"),
        heartbeat_s=100.0,
        coverage_s=100.0,
    )
    assert code == supervise.SUPERVISE_STOP_EXIT
    stop = next(record for record in _records(host) if record.get("type") == "stop")
    assert stop["reason"] == "dead-lease-nonce"
    assert stop.get("scope") == "supervisor"
    assert stop["child"] == "backup"


def test_open_reader_busy_nonce_probe_is_unreadable_not_dead(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Busy open_reader through the real probe must stay retryable.

    A stub that returns ``("unreadable", None)`` cannot catch a collapse of
    unreadable into dead inside ``probe_live_session`` or ``nonce_probe``.
    """
    project, env, lease = isolated

    def busy_open_reader(cls, project_root, **kwargs):  # type: ignore[no-untyped-def]
        raise journal.JournalBusy(
            "journal connection remained busy after 1 attempts within 1.000s"
        )

    monkeypatch.setattr(
        journal.Journal, "open_reader", classmethod(busy_open_reader)
    )
    state, session = sessions.probe_live_session(project, label=lease.label)
    assert state == "unreadable" and session is None
    host = supervise.RealHost(
        project_root=project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        env=env,
    )
    try:
        assert host.nonce_probe() == "unreadable"
    finally:
        host.kill_all()


def test_nonce_reader_third_state_is_unreadable(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
) -> None:
    project, env, lease = isolated
    host = supervise.RealHost(
        project_root=project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        env=env,
        nonce_reader=lambda: supervise.UNREADABLE_NONCE,
    )
    try:
        assert host.nonce_probe() == "unreadable"
    finally:
        host.kill_all()


def test_probe_live_session_unreadable_does_not_kill_the_pool(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bind probe_live_session -> unreadable must not become dead-lease-nonce.

    Drives RealHost.nonce_probe, not a FakeHost that hands back the state.
    """
    project, env, lease = isolated
    always_unread = supervise.RealHost(
        project_root=project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        env=env,
    )
    try:
        monkeypatch.setattr(
            sessions,
            "probe_live_session",
            lambda *args, **kwargs: ("unreadable", None),
        )
        assert always_unread.nonce_probe() == "unreadable"
    finally:
        always_unread.kill_all()

    state = {"mode": "live"}

    def probe(*args: object, **kwargs: object) -> tuple[str, dict | None]:
        if state["mode"] == "unreadable":
            return ("unreadable", None)
        return ("live", {"lease_nonce": lease.nonce})

    monkeypatch.setattr(sessions, "probe_live_session", probe)

    class Host(_RecordingHost):
        def spawn(self, kind: str, command: str) -> supervise.RealChild:
            child = super().spawn(kind, command)
            state["mode"] = "unreadable"
            return child

    script = (
        "import sys\n"
        "print('{\"kind\":\"armed\"}', flush=True)\n"
        "print('{\"kind\":\"ring\",\"reason\":\"event\"}', flush=True)\n"
        "sys.exit(0)\n"
    )
    host = Host(
        project_root=project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        env=env,
    )
    try:
        assert host.nonce_probe() == "live"
        code = supervise.run_supervisor(
            project_root=project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
            host=host,
            heartbeat_s=100.0,
            coverage_s=100.0,
            items=[("backup", _python_child(script))],
        )
        assert host.nonce_probe() == "unreadable"
    finally:
        host.kill_all()

    records = _records(host)
    reasons = [record.get("reason") for record in records]
    assert "dead-lease-nonce" not in reasons
    assert code != supervise.SUPERVISE_STOP_EXIT
    restart = next(record for record in records if record.get("type") == "restart")
    assert restart["reason"] == "rang"


def test_unreadable_startup_with_explicit_nonce_starts(
    isolated: tuple[Path, dict[str, str], journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _env, lease = isolated
    monkeypatch.setattr(
        sessions, "probe_live_session", lambda *args, **kwargs: ("unreadable", None)
    )
    nonce, err, code = supervise.resolve_startup_lease_nonce(
        project_root=project,
        controller_label=lease.label,
        explicit=lease.nonce,
    )
    assert nonce == lease.nonce
    assert err is None
    assert code is None
    missing, missing_err, missing_code = supervise.resolve_startup_lease_nonce(
        project_root=project,
        controller_label=lease.label,
        explicit="",
    )
    assert missing is None
    assert missing_err is not None and "journal unreadable" in missing_err
    assert missing_code == supervise.SUPERVISE_START_EXIT
    assert "did-not-arm" not in str(missing_err)
