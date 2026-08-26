"""t-323: one supervised wake feed owns the pool and re-arms children."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_session_status as sessions  # noqa: E402
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
    nonce_state: str = "live"
    alive_by_kind_at_stop: dict[str, bool] = field(default_factory=dict)
    _coverage_seen: int = 0

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
    host.lease_nonce = nonce
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
    code = _run(host, items, heartbeat_s=100.0, coverage_s=100.0)
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
    ) == (supervise.ACTION_STOP, "did-not-arm")


def test_live_counts_armed_components_not_pids() -> None:
    host = FakeHost(stop_after_coverage=2)
    _run(host, _items("stream", "backup", "watchdog"), heartbeat_s=100.0, coverage_s=0.05)
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
    code = _run(host, _items("stream"), heartbeat_s=100.0, coverage_s=0.05)
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
    assert stop["live"] < stop["target"]


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


@pytest.fixture()
def isolated(monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, str], journal.LeaseIdentity]:
    td = Path(tempfile.mkdtemp(prefix="gf-supervise-"))
    label = "supervise-test"
    env = {
        "GOALFLIGHT_JOURNAL_DIR": str(td / "journals"),
        "GOALFLIGHT_STATE_DIR": str(td / "state"),
        "GOALFLIGHT_WAKE_LEDGER_DIR": str(td / "wake-ledger"),
        "GOALFLIGHT_MESSAGES_DIR": str(td / "messages"),
        "GOALFLIGHT_TASK_STORE_DIR": str(td / "task-store"),
        "GOAL_FLIGHT_PIDFILE_DIR": str(td / "pids"),
        "GOALFLIGHT_CAPACITY_CONF": os.devnull,
        "GOALFLIGHT_TEST_MODE": "1",
        "GOALFLIGHT_CONTROLLER_LABEL": label,
        "GOALFLIGHT_PROCESS_ROLE": "controller",
        "GOALFLIGHT_WAKE_ENTRY_POLL_S": "0",
    }
    for key in (
        "GOALFLIGHT_DISPATCH_ID",
        "GOALFLIGHT_PROMPT_FILE",
        "GOALFLIGHT_STEER_FILE",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE",
        "GOALFLIGHT_PERSISTENT_BACKUP_SLOTS",
    ):
        monkeypatch.delenv(key, raising=False)
    for value in env.values():
        if value != os.devnull:
            Path(value).mkdir(parents=True, exist_ok=True)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    project = td / "project"
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


def _python_child(script: str) -> str:
    return shlex.join([sys.executable, "-c", script])


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
