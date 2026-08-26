"""t-323: one supervised wake feed owns the pool and re-arms children."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_wake as wake  # noqa: E402
import goalflight_wake_supervise as supervise  # noqa: E402


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
    stdout_lines: list[tuple[float, str]]
    emitted_through: float = -1.0
    alive: bool = True


@dataclass
class FakeHost:
    nonce: str = "nonce-1"
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

    def running(self) -> bool:
        return not self.stop

    def live_nonce(self) -> str | None:
        return self.nonce

    def write_stdout(self, line: str) -> bool:
        text = line if line.endswith("\n") else line + "\n"
        self.lines.append(text)
        if self.stop_on_restart_reason and '"type":"restart"' in text:
            record = json.loads(text)
            if record.get("reason") == self.stop_on_restart_reason:
                self.stop = True
        if self.stop_on_stop_reason and '"type":"stop"' in text:
            record = json.loads(text)
            if record.get("reason") == self.stop_on_stop_reason:
                self.stop = True
        if self.stop_when_lines_contain:
            joined = "".join(self.lines)
            if all(part in joined for part in self.stop_when_lines_contain):
                self.stop = True
        return True

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
                armed=True,
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
                armed=plan.armed,
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
        deadline = self.now + max(0.0, timeout_s)
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
        return supervise.WaitResult(lines=lines, exits=exits)

    def kill_all(self) -> None:
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
) -> int:
    return supervise.run_supervisor(
        project_root="/tmp/supervise-test",
        controller_label="bugs",
        lease_nonce=nonce,
        host=host,
        heartbeat_s=heartbeat_s,
        coverage_s=coverage_s,
        items=items,
    )


def test_supervise_commands_call_the_rearm_generator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    code = _run(host, items, heartbeat_s=100.0, coverage_s=100.0)
    assert code == 0
    assert len(host.spawns) == len(items)
    assert [kind for kind, _command in host.spawns].count("backup") == backup_count
    coverage = next(
        record
        for record in _records(host)
        if record.get("type") == "coverage"
    )
    assert coverage["live"] == coverage["target"] == len(items)


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


def test_exit_3_rearms_promptly() -> None:
    host = FakeHost(
        scripts={
            "backup": [
                PlannedExit(
                    lifetime_s=0.2,
                    returncode=3,
                    output="listen: all 1 listener slots hold live doorbells",
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
    assert restart["reason"] == "exit-3"
    assert float(restart["backoff_s"]) == 0.0
    assert len(host.spawns) == 2
    assert host.now < 1.0


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
    assert len(host.spawns) == 1


def test_exit_0_watchdog_slot_held_without_arming_stops() -> None:
    host = FakeHost(
        scripts={
            "watchdog": [
                PlannedExit(
                    lifetime_s=0.1,
                    returncode=0,
                    output="listen: this controller generation already has a live follow watchdog",
                    armed=False,
                ),
            ]
        }
    )
    code = _run(host, _items("watchdog"), heartbeat_s=100.0, coverage_s=100.0)
    assert code == supervise.SUPERVISE_STOP_EXIT
    stop = next(record for record in _records(host) if record.get("type") == "stop")
    assert stop["reason"] == "did-not-arm"
    assert "already has a live follow watchdog" in str(stop.get("detail") or "")


def test_child_stdout_reaches_multiplexed_stdout() -> None:
    host = FakeHost(
        scripts={
            "stream": [
                PlannedExit(
                    lifetime_s=5.0,
                    returncode=0,
                    armed=True,
                    stdout_lines=[
                        (0.0, '{"kind":"heartbeat","payload":{"seq":1,"from":"stream"}}'),
                    ],
                )
            ],
            "backup": [
                PlannedExit(
                    lifetime_s=5.0,
                    returncode=0,
                    armed=True,
                    stdout_lines=[(0.0, "FROM worker subject=done")],
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
            '{"kind":"heartbeat","payload":{"seq":1,"from":"stream"}}',
            "FROM worker subject=done",
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
    assert '{"kind":"heartbeat","payload":{"seq":1,"from":"stream"}}' in joined
    assert "FROM worker subject=done" in joined
    assert '{"kind":"event","payload":{"type":"listener-dead"}}' in joined


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
        output="listen: all 4 listener slots hold live doorbells",
        armed=False,
    ) == (supervise.ACTION_REARM, "exit-3")
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


def test_controller_mail_documents_supervise_front_door() -> None:
    doctrine = (ROOT / "protocols" / "controller-mail.md").read_text(encoding="utf-8")
    assert "goalflight_messages.py supervise" in doctrine
    assert "--lease-nonce" in doctrine
    assert "live/" in doctrine


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
    top = subprocess.run(
        [sys.executable, str(SCRIPTS / "goalflight_messages.py"), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert top.returncode == 0
    assert "supervise" in top.stdout
