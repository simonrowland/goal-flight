#!/usr/bin/env python3
"""Own the persistent wake pool as one tracked stdout feed.

The controller arms this once. It spawns the stream, backup doorbells, and
watchdog from ``goalflight_wake.coverage_rearm_commands``, multiplexes every
child's stdout line-by-line, restarts deaths, and stops on a dead lease nonce.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import select
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any, Protocol

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX wake path.
    fcntl = None  # type: ignore[assignment]

import goalflight_wake as wake


ACTION_REARM = "rearm"
ACTION_BACKOFF = "backoff"
ACTION_STOP = "stop"
SUPERVISE_STOP_EXIT = 3
SUPERVISE_START_EXIT = 2
BACKOFF_INITIAL_S = 1.0
BACKOFF_CAP_S = 120.0
LONG_LIVED_S = 30.0
STREAM_LINE_MAX_BYTES = 511
PERMANENT_UNARMED_FAULTS = 3
_DEAD_NONCE_MARKERS = (
    "controller-capability-mismatch",
    "lease-nonce-not-live",
    "stale-lease",
)
_JOURNAL_UNREADABLE_MARKERS = (
    "journal-unavailable",
    "journal-io-failure",
)
_DID_NOT_ARM_MARKERS = (
    "already has a live follow watchdog",
    "already has a persistent stream",
    "stdout is a regular file",
)
_SLOT_STOP_REASONS = frozenset(
    {"did-not-arm", "permanent-exit-2"}
)


class SuperviseHost(Protocol):
    now: float

    def running(self) -> bool: ...
    def live_nonce(self) -> str | None: ...
    def write_stdout(self, line: str) -> bool: ...
    def spawn(self, kind: str, command: str) -> Any: ...
    def wait(self, children: list[Any], timeout_s: float) -> WaitResult: ...
    def kill_all(self) -> None: ...
    def nonce_probe(self) -> str: ...


@dataclass
class ChildExit:
    child: Any
    returncode: int
    output: str
    armed: bool
    ran_s: float


@dataclass
class WaitResult:
    lines: list[tuple[Any, str]]
    exits: list[ChildExit]


@dataclass
class _Slot:
    kind: str
    command: str
    child: Any = None
    backoff_s: float = 0.0
    next_start: float = 0.0
    stopped_reason: str | None = None
    unarmed_faults: int = 0


def classify_child_exit(
    *,
    kind: str,
    returncode: int,
    output: str,
    armed: bool,
) -> tuple[str, str]:
    """Map a child death onto re-arm, backoff, or stop-and-say-why.

    ``armed`` is the production wake-lock predicate: the child PID was
    observed holding a stream, doorbell, or watchdog flock. Exit 0 without
    that lock is did-not-arm (b-230); journal unreadability is retryable
    and is never collapsed into a dead nonce.
    """
    del kind
    text = str(output or "")
    lowered = text.lower()
    if any(marker in lowered for marker in _JOURNAL_UNREADABLE_MARKERS):
        return ACTION_BACKOFF, "journal-unreadable"
    if any(marker in lowered for marker in _DEAD_NONCE_MARKERS):
        return ACTION_STOP, "dead-lease-nonce"
    if any(marker in lowered for marker in _DID_NOT_ARM_MARKERS):
        return ACTION_STOP, "did-not-arm"
    if returncode == 0 and not armed:
        return ACTION_STOP, "did-not-arm"
    if returncode == 0:
        return ACTION_REARM, "rang"
    if returncode == 3:
        return ACTION_REARM, "exit-3"
    return ACTION_BACKOFF, f"exit-{returncode}"


def next_backoff(current: float, *, ran_s: float, action: str) -> float:
    """Reset after a long-lived child; escalate fast faults up to two minutes."""
    if action != ACTION_BACKOFF:
        return 0.0
    base = 0.0 if ran_s >= LONG_LIVED_S else max(0.0, float(current))
    if base <= 0:
        return BACKOFF_INITIAL_S
    return min(BACKOFF_CAP_S, base * 2.0)


def _supervise_line(record: dict[str, object]) -> str:
    payload = json.dumps(
        record,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    encoded = (payload + "\n").encode("utf-8")
    if len(encoded) <= STREAM_LINE_MAX_BYTES:
        return payload + "\n"
    detail = record.get("detail")
    if isinstance(detail, str) and detail:
        trimmed = dict(record)
        value = detail
        while value and len(
            json.dumps(
                trimmed,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ) + 1 > STREAM_LINE_MAX_BYTES:
            value = value[:-8] + "…"
            trimmed["detail"] = value
        payload = json.dumps(
            trimmed,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
        encoded = (payload + "\n").encode("utf-8")
        if len(encoded) <= STREAM_LINE_MAX_BYTES:
            return payload + "\n"
    budget = max(0, STREAM_LINE_MAX_BYTES - 1)
    return encoded[:budget].decode("utf-8", "ignore") + "\n"


def _live_target(slots: list[_Slot]) -> tuple[int, int]:
    """``live`` is armed coverage, not "a child PID exists"."""
    live = sum(
        1
        for slot in slots
        if slot.child is not None
        and getattr(slot.child, "alive", True)
        and getattr(slot.child, "armed", False)
        and slot.stopped_reason is None
    )
    return live, len(slots)


def _emit(host: SuperviseHost, record: dict[str, object]) -> bool:
    return host.write_stdout(_supervise_line(record))


def _nonce_state(host: SuperviseHost, expected: str) -> str:
    """Return live, dead, or unreadable. Unreadable is retryable."""
    probe = getattr(host, "nonce_probe", None)
    if callable(probe):
        state = str(probe() or "").strip()
        if state == "unreadable":
            return "unreadable"
        if state == "dead":
            return "dead"
        if state == "live":
            live = host.live_nonce()
            if bool(live) and str(live) == expected:
                return "live"
            return "dead"
    live = host.live_nonce()
    if live is None:
        return "dead"
    return "live" if str(live) == expected else "dead"


def run_supervisor(
    *,
    project_root: Path | str,
    controller_label: str,
    lease_nonce: str,
    host: SuperviseHost,
    heartbeat_s: float = 120.0,
    coverage_s: float = 120.0,
    items: list[tuple[str, str]] | None = None,
) -> int:
    """Run until the lease dies, stdout breaks, or the host asks to stop."""
    nonce = str(lease_nonce or "").strip()
    if not nonce:
        _emit(
            host,
            {
                "kind": "supervise",
                "type": "stop",
                "reason": "dead-lease-nonce",
                "detail": "lease nonce missing",
            },
        )
        return SUPERVISE_STOP_EXIT
    if items is None:
        items = wake.coverage_supervise_items(
            project_root,
            controller_label=controller_label,
            lease_nonce=nonce,
        )
    if not items:
        _emit(
            host,
            {
                "kind": "supervise",
                "type": "stop",
                "reason": "did-not-arm",
                "detail": "coverage_rearm_commands returned no children",
            },
        )
        return SUPERVISE_START_EXIT
    slots = [
        _Slot(kind=kind, command=command, next_start=host.now)
        for kind, command in items
    ]

    def spawn_due() -> int | None:
        state = _nonce_state(host, nonce)
        if state == "dead":
            live, target = _live_target(slots)
            _emit(
                host,
                {
                    "kind": "supervise",
                    "type": "stop",
                    "reason": "dead-lease-nonce",
                    "scope": "supervisor",
                    "live": live,
                    "target": target,
                    "detail": "goalflight_session_status live nonce changed or vanished",
                },
            )
            return SUPERVISE_STOP_EXIT
        if state == "unreadable":
            return None
        for slot in slots:
            if slot.stopped_reason is not None:
                continue
            if slot.child is not None or host.now < slot.next_start:
                continue
            slot.child = host.spawn(slot.kind, slot.command)
            setattr(slot.child, "kind", slot.kind)
            setattr(slot.child, "alive", True)
            setattr(slot.child, "armed", False)
        return None

    stopped = spawn_due()
    if stopped is not None:
        host.kill_all()
        return stopped
    seq = 0

    def emit_counts(kind: str) -> bool:
        nonlocal seq
        live, target = _live_target(slots)
        record: dict[str, object] = {
            "kind": "supervise",
            "type": kind,
            "live": live,
            "target": target,
        }
        if kind == "heartbeat":
            seq += 1
            record["seq"] = seq
        return _emit(host, record)

    if not emit_counts("coverage") or not emit_counts("heartbeat"):
        host.kill_all()
        return 0
    next_heartbeat = host.now + max(0.01, float(heartbeat_s))
    next_coverage = host.now + max(0.01, float(coverage_s))

    while host.running():
        state = _nonce_state(host, nonce)
        if state == "dead":
            live, target = _live_target(slots)
            _emit(
                host,
                {
                    "kind": "supervise",
                    "type": "stop",
                    "reason": "dead-lease-nonce",
                    "scope": "supervisor",
                    "live": live,
                    "target": target,
                    "detail": "goalflight_session_status live nonce changed or vanished",
                },
            )
            host.kill_all()
            return SUPERVISE_STOP_EXIT
        now = host.now
        wake_at = min(next_heartbeat, next_coverage)
        for slot in slots:
            if slot.child is None and slot.stopped_reason is None:
                wake_at = min(wake_at, slot.next_start)
        timeout_s = max(0.0, wake_at - now)
        live_children = [
            slot.child
            for slot in slots
            if slot.child is not None and getattr(slot.child, "alive", True)
        ]
        result = host.wait(live_children, timeout_s)
        for child, line in result.lines:
            text = line if line.endswith("\n") else line + "\n"
            if not host.write_stdout(text):
                host.kill_all()
                return 0
        for event in result.exits:
            child = event.child
            slot = next((row for row in slots if row.child is child), None)
            if slot is None:
                continue
            armed = bool(event.armed or getattr(child, "armed", False))
            action, reason = classify_child_exit(
                kind=slot.kind,
                returncode=event.returncode,
                output=event.output,
                armed=armed,
            )
            if (
                action == ACTION_BACKOFF
                and reason != "journal-unreadable"
                and not armed
            ):
                slot.unarmed_faults += 1
                if slot.unarmed_faults >= PERMANENT_UNARMED_FAULTS:
                    action, reason = ACTION_STOP, "permanent-exit-2"
            elif action == ACTION_REARM or (
                action == ACTION_BACKOFF and armed
            ):
                slot.unarmed_faults = 0
            slot.backoff_s = next_backoff(
                slot.backoff_s, ran_s=event.ran_s, action=action
            )
            nonce_now = _nonce_state(host, nonce)
            if action != ACTION_STOP and nonce_now == "dead":
                action, reason = ACTION_STOP, "dead-lease-nonce"
            slot.child = None
            live, target = _live_target(slots)
            if action == ACTION_STOP:
                scope = (
                    "slot" if reason in _SLOT_STOP_REASONS else "supervisor"
                )
                slot.stopped_reason = reason
                _emit(
                    host,
                    {
                        "kind": "supervise",
                        "type": "stop",
                        "reason": reason,
                        "scope": scope,
                        "child": slot.kind,
                        "exit": event.returncode,
                        "live": live,
                        "target": target,
                        "detail": str(event.output or "").strip()[:180],
                    },
                )
                if scope == "supervisor":
                    host.kill_all()
                    return SUPERVISE_STOP_EXIT
                continue
            delay = slot.backoff_s
            slot.next_start = host.now + delay
            if not _emit(
                host,
                {
                    "kind": "supervise",
                    "type": "restart",
                    "child": slot.kind,
                    "exit": event.returncode,
                    "reason": reason,
                    "backoff_s": delay,
                    "live": live,
                    "target": target,
                },
            ):
                host.kill_all()
                return 0
        if host.now >= next_heartbeat:
            if not emit_counts("heartbeat"):
                host.kill_all()
                return 0
            next_heartbeat = host.now + max(0.01, float(heartbeat_s))
        if host.now >= next_coverage:
            if not emit_counts("coverage"):
                host.kill_all()
                return 0
            next_coverage = host.now + max(0.01, float(coverage_s))
        stopped = spawn_due()
        if stopped is not None:
            host.kill_all()
            return stopped

    host.kill_all()
    signum = getattr(host, "stop_signum", None)
    if isinstance(signum, int) and signum > 0:
        return 128 + signum
    return 0


def _stdout_is_regular_file(stream: object) -> str | None:
    """Follow dies if stdout is a regular file; the supervisor must too."""
    import stat as statmod

    fileno = getattr(stream, "fileno", None)
    if fileno is None:
        return "stdout has no inspectable file descriptor"
    try:
        mode = os.fstat(fileno()).st_mode
    except (OSError, ValueError, TypeError):
        return "stdout has no inspectable file descriptor"
    if statmod.S_ISREG(mode):
        return (
            "stdout is a regular file; only a host-monitored pipe/socket can "
            "turn live records into controller wakes"
        )
    return None


def _pop_lines(buf: bytes) -> tuple[list[str], bytes]:
    lines: list[str] = []
    while True:
        index = buf.find(b"\n")
        if index < 0:
            return lines, buf
        raw, buf = buf[:index], buf[index + 1 :]
        lines.append(raw.decode("utf-8", "replace").rstrip("\r"))


def _set_nonblocking(stream: object) -> None:
    if fcntl is None:
        return
    fileno = getattr(stream, "fileno", None)
    if fileno is None:
        return
    try:
        fd = fileno()
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    except (OSError, ValueError):
        return


@dataclass
class RealChild:
    kind: str
    command: str
    popen: subprocess.Popen[bytes]
    started_at: float
    pid: int
    alive: bool = True
    armed: bool = False
    stdout_buf: bytes = b""
    stderr_buf: bytes = b""
    output: str = ""


class RealHost:
    """Spawn coverage_rearm commands with piped stdout; never a regular file."""

    def __init__(
        self,
        *,
        project_root: Path | str,
        controller_label: str,
        lease_nonce: str,
        env: dict[str, str] | None = None,
        nonce_reader: Callable[[], str | None] | None = None,
    ) -> None:
        self.now = time.monotonic()
        self.project_root = Path(project_root)
        self.controller_label = controller_label
        self.lease_nonce = lease_nonce
        self._env = env
        self._nonce_reader = nonce_reader
        self._children: list[RealChild] = []
        self._stop = False
        self.stop_signum: int | None = None
        self._prev_handlers: dict[int, object] = {}
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                self._prev_handlers[signum] = signal.signal(
                    signum, self._on_signal
                )
            except (OSError, ValueError, RuntimeError):
                continue

    def _on_signal(self, signum: int, _frame: object) -> None:
        self.stop_signum = signum
        self._stop = True

    def running(self) -> bool:
        return not self._stop

    def live_nonce(self) -> str | None:
        if self._nonce_reader is not None:
            return self._nonce_reader()
        import goalflight_session_status as sessions  # type: ignore

        session = sessions.live_session(
            self.project_root, label=self.controller_label
        )
        if not isinstance(session, dict):
            return None
        nonce = str(session.get("lease_nonce") or "").strip()
        return nonce or None

    def nonce_probe(self) -> str:
        """Distinguish a readable dead lease from a journal we could not open."""
        if self._nonce_reader is not None:
            live = self._nonce_reader()
            if live is None:
                return "dead"
            return "live" if str(live) == self.lease_nonce else "dead"
        import goalflight_journal  # type: ignore

        try:
            goalflight_journal.Journal.open_reader(self.project_root)
        except goalflight_journal.JournalUnavailable:
            return "unreadable"
        live = self.live_nonce()
        if live is None:
            return "dead"
        return "live" if str(live) == self.lease_nonce else "dead"

    def _observe_locks(self, children: list[Any]) -> None:
        """Mark children armed only when their PID holds a wake flock."""
        alive = [
            child
            for child in children
            if isinstance(child, RealChild) and child.alive and not child.armed
        ]
        if not alive:
            return
        try:
            waiters = wake.live_waiters(
                self.project_root,
                controller_label=self.controller_label,
                generation_key=self.lease_nonce,
                kinds={"listener", wake.MONITOR_KIND, wake.WATCHDOG_KIND},
            )
        except (OSError, RuntimeError, ValueError, TypeError):
            return
        if not waiters:
            return
        pids = {int(row.pid) for row in waiters}
        for child in alive:
            if child.pid in pids:
                child.armed = True

    def write_stdout(self, line: str) -> bool:
        text = line if line.endswith("\n") else line + "\n"
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
            return True
        except OSError as exc:
            if exc.errno == errno.EPIPE:
                return False
            raise

    def spawn(self, kind: str, command: str) -> RealChild:
        env = dict(self._env if self._env is not None else os.environ)
        env.pop("GOALFLIGHT_DISPATCH_ID", None)
        env["GOALFLIGHT_PROCESS_ROLE"] = "listener"
        argv = shlex.split(command)
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=env,
            bufsize=0,
        )
        _set_nonblocking(proc.stdout)
        _set_nonblocking(proc.stderr)
        child = RealChild(
            kind=kind,
            command=command,
            popen=proc,
            started_at=time.monotonic(),
            pid=int(proc.pid),
        )
        self._children.append(child)
        return child

    def _read_stream(self, child: RealChild, which: str) -> list[str]:
        stream = child.popen.stdout if which == "out" else child.popen.stderr
        if stream is None:
            return []
        try:
            data = os.read(stream.fileno(), 65536)
        except BlockingIOError:
            return []
        except OSError:
            return []
        if not data:
            return []
        if which == "out":
            child.stdout_buf += data
            lines, child.stdout_buf = _pop_lines(child.stdout_buf)
        else:
            child.stderr_buf += data
            lines, child.stderr_buf = _pop_lines(child.stderr_buf)
        for line in lines:
            child.output += line + "\n"
            if which == "err":
                try:
                    sys.stderr.write(line + "\n")
                    sys.stderr.flush()
                except OSError:
                    pass
        return lines if which == "out" else []

    def _drain_exited(self, child: RealChild) -> list[str]:
        extra: list[str] = []
        for which in ("out", "err"):
            while True:
                got = self._read_stream(child, which)
                if which == "out":
                    extra.extend(got)
                if not got:
                    break
            stream = child.popen.stdout if which == "out" else child.popen.stderr
            buf = child.stdout_buf if which == "out" else child.stderr_buf
            if buf:
                leftover = buf.decode("utf-8", "replace")
                child.output += leftover if leftover.endswith("\n") else leftover + "\n"
                if which == "out" and leftover.strip():
                    extra.append(leftover.rstrip("\r\n"))
                if which == "out":
                    child.stdout_buf = b""
                else:
                    child.stderr_buf = b""
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        return extra

    def wait(self, children: list[Any], timeout_s: float) -> WaitResult:
        self.now = time.monotonic()
        self._observe_locks(children)
        deadline = self.now + max(0.0, float(timeout_s))
        fdmap: dict[int, tuple[str, RealChild]] = {}
        fds: list[int] = []
        for child in children:
            if not isinstance(child, RealChild) or not child.alive:
                continue
            for which, stream in (
                ("out", child.popen.stdout),
                ("err", child.popen.stderr),
            ):
                if stream is None:
                    continue
                try:
                    fd = stream.fileno()
                except (OSError, ValueError):
                    continue
                fds.append(fd)
                fdmap[fd] = (which, child)
        remaining = max(0.0, deadline - time.monotonic())
        readable: list[int] = []
        if fds:
            try:
                readable, _w, _x = select.select(fds, [], [], remaining)
            except (InterruptedError, OSError):
                readable = []
        elif remaining > 0:
            time.sleep(remaining)
        self.now = time.monotonic()
        self._observe_locks(children)
        lines: list[tuple[Any, str]] = []
        for fd in readable:
            which, child = fdmap[fd]
            for line in self._read_stream(child, which):
                lines.append((child, line))
        exits: list[ChildExit] = []
        for child in children:
            if not isinstance(child, RealChild) or not child.alive:
                continue
            rc = child.popen.poll()
            if rc is None:
                continue
            for line in self._drain_exited(child):
                lines.append((child, line))
            child.alive = False
            exits.append(
                ChildExit(
                    child=child,
                    returncode=int(rc),
                    output=child.output,
                    armed=child.armed,
                    ran_s=max(0.0, self.now - child.started_at),
                )
            )
        return WaitResult(lines=lines, exits=exits)

    def kill_all(self) -> None:
        for child in self._children:
            if not child.alive:
                continue
            try:
                child.popen.terminate()
            except OSError:
                continue
        deadline = time.monotonic() + 1.0
        for child in self._children:
            if child.popen.poll() is not None:
                child.alive = False
                continue
            remaining = deadline - time.monotonic()
            try:
                child.popen.wait(timeout=max(0.01, remaining))
            except subprocess.TimeoutExpired:
                try:
                    child.popen.kill()
                except OSError:
                    pass
            child.alive = False
        for signum, handler in self._prev_handlers.items():
            try:
                signal.signal(signum, handler)  # type: ignore[arg-type]
            except (OSError, ValueError, RuntimeError):
                continue
        self._prev_handlers.clear()


def cmd_supervise(args: Any) -> int:
    """CLI entry used by goalflight_messages.py supervise."""
    import goalflight_session_status as sessions  # type: ignore
    import goalflight_task  # type: ignore

    if str(os.environ.get("GOALFLIGHT_DISPATCH_ID") or "").strip():
        print(
            "supervise: refuse: workers cannot arm controller wake coverage",
            file=sys.stderr,
        )
        return SUPERVISE_START_EXIT
    refusal = _stdout_is_regular_file(sys.stdout)
    if refusal:
        print(f"supervise: refused: {refusal}", file=sys.stderr)
        return SUPERVISE_START_EXIT
    project_root = goalflight_task.resolve_project_root(
        getattr(args, "project_root", None) or str(Path.cwd())
    )
    label = sessions.resolve_controller_label(
        getattr(args, "controller_label", None),
        project_root=project_root,
    )
    if not label:
        print("supervise: controller label is unavailable", file=sys.stderr)
        return SUPERVISE_START_EXIT
    session = sessions.live_session(project_root, label=label)
    live_nonce = (
        str(session.get("lease_nonce") or "").strip()
        if isinstance(session, dict)
        else ""
    )
    if not live_nonce:
        print(
            "supervise: did-not-arm: no live controller lease nonce "
            "from goalflight_session_status",
            file=sys.stderr,
        )
        return SUPERVISE_STOP_EXIT
    explicit = str(getattr(args, "lease_nonce", None) or "").strip()
    if explicit and explicit != live_nonce:
        print(
            "supervise: did-not-arm: --lease-nonce does not match live "
            f"session nonce ({live_nonce[:12]}…)",
            file=sys.stderr,
        )
        return SUPERVISE_STOP_EXIT
    test_mode = os.environ.get("GOALFLIGHT_TEST_MODE") == "1"
    heartbeat_s = float(getattr(args, "heartbeat_secs", 120.0) or 120.0)
    coverage_s = float(getattr(args, "coverage_secs", 0.0) or 0.0) or heartbeat_s
    if not test_mode:
        if not 60.0 <= heartbeat_s <= 300.0:
            print(
                "supervise: heartbeat-secs must stay between 60 and 300; "
                "faster risks host volume limiting and slower hides deafness",
                file=sys.stderr,
            )
            return SUPERVISE_START_EXIT
        if coverage_s <= 0:
            print("supervise: coverage-secs must be positive", file=sys.stderr)
            return SUPERVISE_START_EXIT
    host = RealHost(
        project_root=project_root,
        controller_label=label,
        lease_nonce=live_nonce,
    )
    return run_supervisor(
        project_root=project_root,
        controller_label=label,
        lease_nonce=live_nonce,
        host=host,
        heartbeat_s=heartbeat_s,
        coverage_s=coverage_s,
    )
