"""t-323: one supervised wake feed owns the pool and re-arms children."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shlex
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
    assert any(
        record.get("kind") == "supervise" and record.get("type") == "heartbeat"
        for record in _records(host)
    )


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
    assert any(
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
