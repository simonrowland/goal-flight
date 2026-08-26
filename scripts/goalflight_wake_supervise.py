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
import math
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
POLL_FAILURE_LIMIT = 3
DEFAULT_SUPERVISOR_HEARTBEAT_S = 25.0 * 60.0
MIN_SUPERVISOR_HEARTBEAT_S = 60.0
MAX_SUPERVISOR_HEARTBEAT_S = 30.0 * 60.0
PERSISTENT_BACKUP_SLOTS_ENV = "GOALFLIGHT_PERSISTENT_BACKUP_SLOTS"
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
_ORPHANED_PARENT_MARKERS = (
    "orphaned: watchdog parent changed",
    "orphaned: listener parent changed",
)
_ORPHANED_STDOUT_MARKERS = (
    "orphaned: controlling stdout closed",
)
_SLOT_STOP_REASONS = frozenset(
    {"did-not-arm", "permanent-exit-2"}
)
_DIAGNOSTIC_EVENT_TYPES = frozenset({"listener-exit", "listener-fault"})
_ARMED_STDOUT_KINDS = frozenset(
    {
        "armed",
        "ring",
        "heartbeat",
        "event",
        "frontier",
        "pending-at-arm",
    }
)
SUPERVISED_ENV = "GOALFLIGHT_SUPERVISED"


class UnreadableNonce:
    """Sentinel: ``nonce_reader`` could not tell live from dead.

    ``None`` is dead. A nonce string is live if it matches, else dead.
    Without this third state the hook cannot express a busy journal and
    a collapse of ``probe_live_session`` ``unreadable`` into dead is unbound.
    """

    __slots__ = ()


UNREADABLE_NONCE = UnreadableNonce()


@dataclass(frozen=True)
class _DetectorFailure:
    source: str
    error: str
    detail: str


@dataclass(frozen=True)
class _DetectorStatus:
    availability: str
    peer_gone: bool
    failure: _DetectorFailure | None


class _PeerLossDetector:
    """One write-once choke point for every controlling-stdout detector.

    Probes may be inconclusive without disproving the registered poll detector.
    Positive availability observations never clear a terminal failure: once a
    detector layer cannot operate, the supervisor must stop and advertise its
    re-arm path rather than letting another layer mask the loss.
    """

    def __init__(self) -> None:
        self._available = False
        self._peer_gone = False
        self._failure: _DetectorFailure | None = None

    def report(
        self,
        source: str,
        outcome: str,
        detail: str = "",
        error: str = "",
    ) -> None:
        if outcome == "available":
            self._available = True
        elif outcome == "peer-gone":
            self._peer_gone = True
        elif outcome == "unavailable":
            if self._failure is None:
                self._failure = _DetectorFailure(
                    source=source,
                    error=error or "unavailable",
                    detail=detail,
                )
        elif outcome != "unknown":
            raise ValueError(f"unknown peer-loss detector outcome: {outcome}")

    def status(self) -> _DetectorStatus:
        availability = "unavailable" if self._failure else (
            "available" if self._available else "unknown"
        )
        return _DetectorStatus(
            availability=availability,
            peer_gone=self._peer_gone,
            failure=self._failure,
        )


class SuperviseHost(Protocol):
    now: float

    def running(self) -> bool: ...
    def live_nonce(self) -> str | None: ...
    def write_stdout(self, line: str) -> bool: ...
    def stdio_peer_gone(self) -> bool: ...
    def report_stdout_detector(
        self, source: str, outcome: str, detail: str = "", error: str = ""
    ) -> None: ...
    def stdout_detector_status(self) -> _DetectorStatus: ...
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

    ``output`` is the child's diagnostic channel (stderr plus structured
    child-exit JSON reasons), never relayed mail headlines. Marker scans
    of mixed stdout would treat a doorbell report of ``stale-lease`` as
    supervisor death.

    ``armed`` is a positive observation (child stdout or a sampled flock).
    A missed sample is a false negative and must re-arm, never stop: exit 0
    without an explicit did-not-arm marker is "rang". Exit 5 is settled
    never-armed (dead or missing lease) and is supervisor-wide even with
    empty stderr: leftover-lock / regular-file did-not-arm is a slot stop
    identified by the markers above (typically exit 3). Mapping bare exit 5
    onto the slot-stop reason made the same dead-nonce condition two
    outcomes depending on whether a marker was captured. Journal
    unreadability is retryable and is never collapsed into a dead nonce.

    Watch-follow return-3 sites: leftover watchdog lock is did-not-arm;
    stale-lease is a dead nonce. Parent-changed and controlling-stdout-closed
    are the child's view of a vanished host. A supervised child's parent is
    this supervisor and its stdout is the pipe we still hold, so those
    prints are a shutdown race (or a subreaper reparent) rather than a
    live-pool condition — they are named here so they cannot hide in the
    exit-3 catch-all. Residual exit 3 is ``exit-3-unclassified``.
    """
    del kind
    del armed
    text = str(output or "")
    lowered = text.lower()
    if any(marker in lowered for marker in _JOURNAL_UNREADABLE_MARKERS):
        return ACTION_BACKOFF, "journal-unreadable"
    if any(marker in lowered for marker in _DEAD_NONCE_MARKERS):
        return ACTION_STOP, "dead-lease-nonce"
    if any(marker in lowered for marker in _DID_NOT_ARM_MARKERS):
        return ACTION_STOP, "did-not-arm"
    if any(marker in lowered for marker in _ORPHANED_PARENT_MARKERS):
        return ACTION_BACKOFF, "orphaned-parent"
    if any(marker in lowered for marker in _ORPHANED_STDOUT_MARKERS):
        return ACTION_BACKOFF, "orphaned-stdout"
    if returncode == 0:
        return ACTION_REARM, "rang"
    # LISTENER_DID_NOT_ARM_EXIT: the child never waited because the lease
    # is known-dead or missing. That is supervisor-wide (do not re-arm this
    # nonce), not a per-slot leftover-lock. Marker checks above still win
    # so an explicit leftover-lock diagnostic stays a slot stop even if a
    # child somehow also used this code.
    if returncode == 5:
        return ACTION_STOP, "dead-lease-nonce"
    if returncode == 3:
        return ACTION_BACKOFF, "exit-3-unclassified"
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
            if len(value) <= 8:
                value = ""
                trimmed.pop("detail", None)
            else:
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
    # Supervisor recovery records must remain valid JSON and preserve the
    # exact re-arm command. Normal records stay within the stream-line cap;
    # when the command alone exceeds it, an oversized valid record is safer
    # than a capped fragment that cannot be parsed or used for recovery.
    if "rearm" in record:
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


def _report_stdout_detector(
    host: SuperviseHost,
    *,
    source: str,
    outcome: str,
    detail: str = "",
    error: str = "",
) -> None:
    host.report_stdout_detector(source, outcome, detail, error)


def _write_stdout(host: SuperviseHost, line: str, *, source: str) -> bool:
    """Write once and report the write detector through the shared choke point."""
    try:
        written = host.write_stdout(line)
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        if isinstance(exc, OSError) and exc.errno == errno.EPIPE:
            _report_stdout_detector(
                host,
                source=source,
                outcome="peer-gone",
                detail="controlling stdout closed during write",
            )
        else:
            error_name = ""
            if isinstance(exc, OSError) and exc.errno is not None:
                error_name = errno.errorcode.get(exc.errno, str(exc.errno)) + ": "
            _report_stdout_detector(
                host,
                source=source,
                outcome="unavailable",
                detail=f"stdout write failed: {error_name}{exc}",
                error=(
                    error_name.removesuffix(": ")
                    if error_name
                    else type(exc).__name__
                ),
            )
        return False
    _report_stdout_detector(
        host,
        source=source,
        outcome="available" if written else "peer-gone",
        detail="" if written else "controlling stdout closed during write",
    )
    return written


def _emit(host: SuperviseHost, record: dict[str, object]) -> bool:
    record_type = str(record.get("type") or "record")
    return _write_stdout(
        host,
        _supervise_line(record),
        source=f"write-{record_type}",
    )


def _supervisor_rearm_command(
    *,
    project_root: Path | str,
    controller_label: str,
    lease_nonce: str,
    heartbeat_s: float,
    coverage_s: float,
    debug: bool,
) -> str:
    """Build the canonical, semantically faithful supervisor invocation."""
    argv = shlex.split(
        wake.coverage_supervise_command(
            project_root,
            controller_label=controller_label,
            lease_nonce=lease_nonce,
        )
    )
    argv.extend(
        [
            "--heartbeat-secs",
            format(float(heartbeat_s), ".15g"),
            "--coverage-secs",
            format(float(coverage_s), ".15g"),
        ]
    )
    if debug:
        argv.append("--debug")
    if PERSISTENT_BACKUP_SLOTS_ENV in os.environ:
        argv[:0] = [
            "env",
            f"{PERSISTENT_BACKUP_SLOTS_ENV}="
            f"{os.environ[PERSISTENT_BACKUP_SLOTS_ENV]}",
        ]
    return shlex.join(argv)


def _probe_stdout_detector(host: SuperviseHost, *, source: str) -> None:
    """Report a pre/post probe without flattening inconclusive into healthy."""
    probe = getattr(host, "stdio_peer_gone", None)
    if not callable(probe):
        _report_stdout_detector(
            host,
            source=source,
            outcome="unknown",
            detail="stdout peer-gone probe is unavailable",
        )
        return
    try:
        peer_gone = bool(probe())
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        _report_stdout_detector(
            host,
            source=source,
            outcome="unknown",
            detail=f"stdout peer-gone probe was inconclusive: {exc}",
        )
        return
    _report_stdout_detector(
        host,
        source=source,
        outcome="peer-gone" if peer_gone else "unknown",
        detail="controlling stdout closed" if peer_gone else "no closure evidence",
    )


def _signal_reason(signum: int) -> str:
    try:
        name = signal.Signals(signum).name
    except (ValueError, SystemError):
        name = str(signum)
    return f"signal-{name}"


def _line_signals_armed(line: str) -> bool:
    """True when a child line is durable evidence it armed, not a lock sample."""
    text = str(line or "").strip()
    if not text:
        return False
    if text.startswith("advance:"):
        return True
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    return str(payload.get("kind") or "") in _ARMED_STDOUT_KINDS


def _is_armed_control_line(line: str) -> bool:
    """The dedicated armed witness is for the supervisor, not a controller wake."""
    text = str(line or "").strip()
    if not text:
        return False
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    return isinstance(payload, dict) and str(payload.get("kind") or "") == "armed"


def _structured_child_reason(line: str) -> str | None:
    """Extract a child-authored diagnostic reason from a JSON control line.

    Mail headlines and follow event payloads (envelope ``data``) are not
    diagnostics. Only ``kind=exit`` and listener-exit/fault events count.
    Markers in those records can appear on stdout; a stderr-only scan
    would miss follow's ``listener-exit`` stale-lease JSON.
    """
    text = str(line or "").strip()
    if not text.startswith("{"):
        return None
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    kind = str(payload.get("kind") or "")
    if kind == "exit":
        reason = str(payload.get("reason") or "").strip()
        return reason or None
    inner = payload.get("payload")
    if kind == "event" and isinstance(inner, dict):
        if str(inner.get("type") or "") in _DIAGNOSTIC_EVENT_TYPES:
            reason = str(inner.get("reason") or "").strip()
            return reason or None
    return None


def _note_child_diagnostic(child: RealChild, line: str, *, stderr: bool) -> None:
    """Accumulate classification input; never relayed mail headlines."""
    if stderr:
        child.output += line if line.endswith("\n") else line + "\n"
        return
    reason = _structured_child_reason(line)
    if reason:
        child.output += reason + "\n"


def _nonce_state(host: SuperviseHost, expected: str) -> str:
    """Return live, dead, or unreadable. Unreadable is retryable.

    The probe is the single source of truth. Do not re-derive via
    ``live_nonce()`` after a live result: a second busy ``Journal()`` can
    flip live to dead inside the same child-death.
    """
    probe = getattr(host, "nonce_probe", None)
    if callable(probe):
        state = str(probe() or "").strip()
        if state in {"unreadable", "dead", "live"}:
            return state
        # Probe existed but could not tell. Do not fall through to
        # live_nonce(): that API collapses unreadable into None, which
        # this function would then treat as dead.
        return "unreadable"
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
    heartbeat_s: float = DEFAULT_SUPERVISOR_HEARTBEAT_S,
    coverage_s: float = DEFAULT_SUPERVISOR_HEARTBEAT_S,
    items: list[tuple[str, str]] | None = None,
    debug: bool = False,
) -> int:
    """Run until the lease dies, stdout breaks, or the host asks to stop."""
    nonce = str(lease_nonce or "").strip()
    rearm = _supervisor_rearm_command(
        project_root=project_root,
        controller_label=controller_label,
        lease_nonce=nonce,
        heartbeat_s=heartbeat_s,
        coverage_s=coverage_s,
        debug=debug,
    )

    def emit_stop(**fields: object) -> bool:
        record: dict[str, object] = {
            "kind": "supervise",
            "type": "stop",
            "rearm": rearm,
        }
        record.update(fields)
        return _emit(host, record)

    if not nonce:
        emit_stop(
            reason="dead-lease-nonce",
            detail="lease nonce missing",
        )
        return SUPERVISE_STOP_EXIT
    if items is None:
        items = wake.coverage_supervise_items(
            project_root,
            controller_label=controller_label,
            lease_nonce=nonce,
        )
    if not items:
        emit_stop(
            reason="did-not-arm",
            detail="coverage_rearm_commands returned no children",
        )
        return SUPERVISE_START_EXIT
    slots = [
        _Slot(kind=kind, command=command, next_start=host.now)
        for kind, command in items
    ]

    def stop_for_stdout_detector() -> int | None:
        """Define the one terminal policy for all peer-loss detector layers."""
        status = host.stdout_detector_status()
        failure = status.failure
        if failure is not None:
            live, target = _live_target(slots)
            emit_stop(
                reason="stdout-peer-detector-unavailable",
                scope="supervisor",
                detector=failure.source,
                error=failure.error,
                live=live,
                target=target,
                detail=failure.detail,
            )
            try:
                sys.stderr.write(
                    "goalflight supervise: stdout peer-gone detector unavailable; "
                    f"stopping: {failure.source}: {failure.detail}\n"
                )
                sys.stderr.flush()
            except (AttributeError, OSError, ValueError):
                pass
            host.kill_all()
            return SUPERVISE_STOP_EXIT
        if status.peer_gone:
            host.kill_all()
            return 0
        # Unknown probe observations are allowed only while the registered
        # poll detector or periodic write path remains available. Registration,
        # poll use, and writes report terminal unavailability above.
        return None

    def stop_after_failed_write() -> int:
        """Route every failed write through the same terminal policy."""
        stopped = stop_for_stdout_detector()
        if stopped is not None:
            return stopped
        # A host returning False without reporting it violates the protocol.
        # Treat that unknown detector state as unavailable, then use the same
        # stop-record/teardown/exit path rather than inventing a fallback here.
        _report_stdout_detector(
            host,
            source="write",
            outcome="unavailable",
            detail="stdout write failed without a detector outcome",
            error="missing-write-outcome",
        )
        stopped = stop_for_stdout_detector()
        assert stopped is not None
        return stopped

    def spawn_due() -> int | None:
        state = _nonce_state(host, nonce)
        if state == "dead":
            live, target = _live_target(slots)
            emit_stop(
                reason="dead-lease-nonce",
                scope="supervisor",
                live=live,
                target=target,
                detail="goalflight_session_status live nonce changed or vanished",
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
    coverage_revision = 0
    reported_revision = -1
    reported_counts: tuple[int, int] | None = None

    def coverage_changed() -> None:
        nonlocal coverage_revision
        coverage_revision += 1

    def emit_coverage(*, force: bool = False) -> tuple[bool, bool]:
        nonlocal reported_counts, reported_revision
        live, target = _live_target(slots)
        counts = (live, target)
        if (
            not force
            and counts == reported_counts
            and coverage_revision == reported_revision
        ):
            return True, False
        record: dict[str, object] = {
            "kind": "supervise",
            "type": "coverage",
            "live": live,
            "target": target,
        }
        emitted = _emit(host, record)
        if emitted:
            reported_counts = counts
            reported_revision = coverage_revision
        return emitted, emitted

    def emit_heartbeat() -> bool:
        nonlocal seq
        live, target = _live_target(slots)
        seq += 1
        return _emit(
            host,
            {
                "kind": "supervise",
                "type": "heartbeat",
                "live": live,
                "target": target,
                "seq": seq,
            },
        )

    coverage_ok, _coverage_emitted = emit_coverage(force=True)
    if not coverage_ok or (debug and not emit_heartbeat()):
        return stop_after_failed_write()
    next_heartbeat = host.now + max(0.01, float(heartbeat_s))
    next_coverage = host.now + max(0.01, float(coverage_s))

    while host.running():
        # Every detector reports to _PeerLossDetector; stop_for_stdout_detector
        # is the sole terminal policy. The probes around wait are allowed to be
        # inconclusive, while registration failure or persistently unusable
        # poll means the fast detector is unavailable and fails closed. Every
        # write is the authoritative point-in-time peer check. The 1500-second
        # supervisor heartbeat remains distinct from the forwarded stream
        # child's 120-second heartbeat, which proves stream liveness to
        # --watch-follow and drives its three-missed-interval death threshold.
        _probe_stdout_detector(host, source="probe-before-wait")
        stopped = stop_for_stdout_detector()
        if stopped is not None:
            return stopped
        state = _nonce_state(host, nonce)
        if state == "dead":
            live, target = _live_target(slots)
            emit_stop(
                reason="dead-lease-nonce",
                scope="supervisor",
                live=live,
                target=target,
                detail="goalflight_session_status live nonce changed or vanished",
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
        _probe_stdout_detector(host, source="probe-after-wait")
        stopped = stop_for_stdout_detector()
        if stopped is not None:
            return stopped
        wait_signum = getattr(host, "stop_signum", None)
        if (
            not host.running()
            and isinstance(wait_signum, int)
            and wait_signum > 0
        ):
            break
        for child, line in result.lines:
            text = line if line.endswith("\n") else line + "\n"
            if not _write_stdout(host, text, source="write-child-output"):
                return stop_after_failed_write()
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
            # Do not use sampled ``armed`` as negative evidence (the P0-2
            # flock-miss hazard). Count consecutive short non-journal
            # exit-2s regardless of the sample; a long-lived run resets.
            if (
                action == ACTION_BACKOFF
                and reason != "journal-unreadable"
                and event.returncode == 2
                and event.ran_s < LONG_LIVED_S
            ):
                slot.unarmed_faults += 1
                if slot.unarmed_faults >= PERMANENT_UNARMED_FAULTS:
                    action, reason = ACTION_STOP, "permanent-exit-2"
            elif (
                action == ACTION_REARM
                or reason == "journal-unreadable"
                or event.ran_s >= LONG_LIVED_S
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
                coverage_changed()
                emit_stop(
                    reason=reason,
                    scope=scope,
                    child=slot.kind,
                    exit=event.returncode,
                    live=live,
                    target=target,
                    detail=str(event.output or "").strip()[:180],
                )
                coverage_ok, _coverage_emitted = emit_coverage()
                if not coverage_ok:
                    return stop_after_failed_write()
                if scope == "supervisor":
                    host.kill_all()
                    return SUPERVISE_STOP_EXIT
                continue
            delay = slot.backoff_s
            slot.next_start = host.now + delay
            coverage_changed()
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
                return stop_after_failed_write()
        coverage_ok, coverage_emitted = emit_coverage()
        if not coverage_ok:
            return stop_after_failed_write()
        if host.now >= next_heartbeat:
            if not emit_heartbeat():
                return stop_after_failed_write()
            next_heartbeat = host.now + max(0.01, float(heartbeat_s))
        if host.now >= next_coverage:
            coverage_ok, _debug_emitted = emit_coverage(
                force=bool(debug and not coverage_emitted)
            )
            if not coverage_ok:
                return stop_after_failed_write()
            next_coverage = host.now + max(0.01, float(coverage_s))
        stopped = spawn_due()
        if stopped is not None:
            host.kill_all()
            return stopped

    signum = getattr(host, "stop_signum", None)
    if isinstance(signum, int) and signum > 0:
        live, target = _live_target(slots)
        # SIGTERM, SIGINT, and SIGHUP get one hint while stdout is still open.
        # SIGKILL cannot be caught, so that hard-kill gap cannot emit a hint.
        _emit(
            host,
            {
                "kind": "supervise",
                "type": "exit",
                "reason": _signal_reason(signum),
                "live": live,
                "target": target,
                "rearm": rearm,
            },
        )
        host.kill_all()
        return 128 + signum
    host.kill_all()
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
    output: str = ""  # diagnostic: stderr + structured child-exit reasons


class RealHost:
    """Spawn coverage_rearm commands with piped stdout; never a regular file."""

    def __init__(
        self,
        *,
        project_root: Path | str,
        controller_label: str,
        lease_nonce: str,
        env: dict[str, str] | None = None,
        nonce_reader: Callable[[], str | None | UnreadableNonce] | None = None,
    ) -> None:
        self.now = time.monotonic()
        self.project_root = Path(project_root)
        self.controller_label = controller_label
        self.lease_nonce = lease_nonce
        self._env = env
        self._nonce_reader = nonce_reader
        self._children: list[RealChild] = []
        self._stop = False
        self._stdout_detector = _PeerLossDetector()
        self.stop_signum: int | None = None
        self._prev_handlers: dict[int, object] = {}
        signal_rfd, signal_wfd = os.pipe()
        self._signal_rfd: int | None = signal_rfd
        self._signal_wfd: int | None = signal_wfd
        os.set_blocking(self._signal_rfd, False)
        os.set_blocking(self._signal_wfd, False)
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
        # Flagging alone can leave select() asleep for the 25-minute heartbeat
        # because Python may restart interrupted syscalls. The self-pipe makes
        # SIGTERM/SIGINT/SIGHUP recovery output prompt; a full pipe is awake.
        signal_wfd = self._signal_wfd
        if signal_wfd is None:
            return
        try:
            os.write(signal_wfd, b"\0")
        except (BlockingIOError, OSError):
            pass

    def running(self) -> bool:
        return not self._stop

    def live_nonce(self) -> str | None:
        if self._nonce_reader is not None:
            live = self._nonce_reader()
            if live is UNREADABLE_NONCE or live is None:
                return None
            return str(live)
        import goalflight_session_status as sessions  # type: ignore

        state, session = sessions.probe_live_session(
            self.project_root, label=self.controller_label
        )
        if state != "live" or not isinstance(session, dict):
            return None
        nonce = str(session.get("lease_nonce") or "").strip()
        return nonce or None

    def nonce_probe(self) -> str:
        """Distinguish a readable dead lease from a journal we could not open.

        Reads the nonce through ``probe_live_session`` (non-locking reader).
        Never calls the write ``Journal()`` constructor, and never treats
        ``live_session() is None`` as dead after a successful reader open.

        ``nonce_reader`` returns a nonce string, ``None`` (dead), or
        ``UNREADABLE_NONCE``. Collapsing ``unreadable`` into dead here
        would recreate the busy-journal supervisor death.
        """
        if self._nonce_reader is not None:
            live = self._nonce_reader()
            if live is UNREADABLE_NONCE:
                return "unreadable"
            if live is None:
                return "dead"
            return "live" if str(live) == self.lease_nonce else "dead"
        import goalflight_session_status as sessions  # type: ignore

        state, session = sessions.probe_live_session(
            self.project_root, label=self.controller_label
        )
        if state == "unreadable":
            return "unreadable"
        if state != "live" or not isinstance(session, dict):
            return "dead"
        live = str(session.get("lease_nonce") or "").strip()
        if not live:
            return "dead"
        return "live" if live == self.lease_nonce else "dead"

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

    def stdio_peer_gone(self) -> bool:
        if self.stdout_detector_status().peer_gone:
            return True
        # Import lazily: goalflight_messages imports this module for the CLI.
        import goalflight_messages as messages  # type: ignore

        return bool(messages._stdio_peer_gone(sys.stdout))

    def report_stdout_detector(
        self, source: str, outcome: str, detail: str = "", error: str = ""
    ) -> None:
        self._stdout_detector.report(source, outcome, detail, error)

    def stdout_detector_status(self) -> _DetectorStatus:
        return self._stdout_detector.status()

    @property
    def _stdout_detector_failure(self) -> str | None:
        """Compatibility view of the write-once detector failure latch."""
        failure = self.stdout_detector_status().failure
        return failure.detail if failure is not None else None

    def spawn(self, kind: str, command: str) -> RealChild:
        env = dict(self._env if self._env is not None else os.environ)
        env.pop("GOALFLIGHT_DISPATCH_ID", None)
        env["GOALFLIGHT_PROCESS_ROLE"] = "listener"
        env[SUPERVISED_ENV] = "1"
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
        forwarded: list[str] = []
        for line in lines:
            if _line_signals_armed(line):
                child.armed = True
            if which == "err":
                _note_child_diagnostic(child, line, stderr=True)
                try:
                    sys.stderr.write(line + "\n")
                    sys.stderr.flush()
                except OSError:
                    pass
            else:
                _note_child_diagnostic(child, line, stderr=False)
                if not _is_armed_control_line(line):
                    forwarded.append(line)
        return forwarded if which == "out" else []

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
                if _line_signals_armed(leftover):
                    child.armed = True
                _note_child_diagnostic(
                    child, leftover, stderr=(which == "err")
                )
                if (
                    which == "out"
                    and leftover.strip()
                    and not _is_armed_control_line(leftover)
                ):
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
        fdmap: dict[int, tuple[str, RealChild | None]] = {}
        poller = select.poll()
        peer_gone_events = select.POLLERR | select.POLLHUP | select.POLLNVAL
        read_events = select.POLLIN | peer_gone_events
        signal_rfd = self._signal_rfd
        if signal_rfd is not None:
            try:
                poller.register(signal_rfd, read_events)
            except (OSError, ValueError):
                pass
            else:
                fdmap[signal_rfd] = ("signal", None)
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
                try:
                    poller.register(fd, read_events)
                except (OSError, ValueError):
                    continue
                else:
                    fdmap[fd] = (which, child)
        try:
            stdout_fd = sys.stdout.fileno()
        except (AttributeError, OSError, TypeError, ValueError):
            self.report_stdout_detector(
                "registration",
                "unavailable",
                "stdout has no inspectable file descriptor",
                "no-file-descriptor",
            )
        else:
            if stdout_fd in fdmap:
                self.report_stdout_detector(
                    "registration",
                    "unavailable",
                    "stdout file descriptor collides with another wait source",
                    "file-descriptor-collision",
                )
            else:
                try:
                    poller.register(stdout_fd, peer_gone_events)
                except (OSError, TypeError, ValueError):
                    self.report_stdout_detector(
                        "registration",
                        "unavailable",
                        "stdout file descriptor registration failed",
                        "file-descriptor-registration-failed",
                    )
                else:
                    fdmap[stdout_fd] = ("stdout", None)
                    self.report_stdout_detector("registration", "available")
        # _stdio_peer_gone returns False both for no evidence and when it
        # cannot inspect stdout. Registration success adds an independent
        # detector whose no-event result is likewise only no evidence. If
        # registration cannot be established, no detector can be trusted to
        # wake this wait, so fail closed now instead of using the heartbeat as
        # an implicit 25-minute fallback.
        remaining = max(0.0, deadline - time.monotonic())
        if self.stdout_detector_status().failure is not None:
            remaining = 0.0
        ready: list[int] = []
        if fdmap and self.stdout_detector_status().failure is None:
            poll_failures = 0
            while True:
                try:
                    events_ready = poller.poll(
                        math.ceil(
                            max(0.0, deadline - time.monotonic()) * 1000.0
                        )
                    )
                except (OSError, ValueError) as exc:
                    poll_failures += 1
                    error_number = getattr(exc, "errno", None)
                    error_name = (
                        errno.errorcode.get(error_number, str(error_number))
                        if error_number is not None
                        else type(exc).__name__
                    )
                    if poll_failures >= POLL_FAILURE_LIMIT:
                        self.report_stdout_detector(
                            "poll",
                            "unavailable",
                            "stdout poll failed persistently after "
                            f"{poll_failures} attempts: {error_name}: {exc}",
                            error_name,
                        )
                        break
                    self.report_stdout_detector(
                        "poll",
                        "unknown",
                        (
                            "stdout poll interrupted; retrying"
                            if error_number == errno.EINTR
                            else "stdout poll failed transiently; retrying"
                        ),
                    )
                    continue
                self.report_stdout_detector("poll", "available")
                ready = [
                    fd
                    for fd, events in events_ready
                    if events & read_events
                ]
                break
        elif remaining > 0:
            time.sleep(remaining)
        self.now = time.monotonic()
        self._observe_locks(children)
        lines: list[tuple[Any, str]] = []
        for fd in ready:
            which, child = fdmap[fd]
            if which == "stdout":
                self.report_stdout_detector(
                    "poll", "peer-gone", "controlling stdout poll reported closure"
                )
                continue
            if which == "signal":
                while True:
                    try:
                        if not os.read(fd, 4096):
                            break
                    except BlockingIOError:
                        break
                    except OSError:
                        break
                continue
            if child is None:
                continue
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
        for name in ("_signal_rfd", "_signal_wfd"):
            fd = getattr(self, name)
            if fd is None:
                continue
            try:
                os.close(fd)
            except OSError:
                pass
            setattr(self, name, None)


def resolve_startup_lease_nonce(
    *,
    project_root: Path | str,
    controller_label: str,
    explicit: str,
) -> tuple[str | None, str | None, int | None]:
    """Pin the supervise nonce from a readable live session.

    Unreadable is retryable: an explicit ``--lease-nonce`` is used as the pin
    so the process can start and the runtime probe can retry. A missing
    explicit nonce with an unreadable journal is a start fault, not
    did-not-arm. Only a readable absent or changed session is did-not-arm.
    """
    import goalflight_session_status as sessions  # type: ignore

    explicit_nonce = str(explicit or "").strip()
    state, session = sessions.probe_live_session(
        Path(project_root), label=controller_label
    )
    if state == "unreadable":
        if explicit_nonce:
            return explicit_nonce, None, None
        return (
            None,
            "journal unreadable; cannot confirm a live lease nonce "
            "and no --lease-nonce was given",
            SUPERVISE_START_EXIT,
        )
    live = ""
    if isinstance(session, dict):
        live = str(session.get("lease_nonce") or "").strip()
    if state != "live" or not live:
        return (
            None,
            "did-not-arm: no live controller lease nonce "
            "from goalflight_session_status",
            SUPERVISE_STOP_EXIT,
        )
    if explicit_nonce and explicit_nonce != live:
        return (
            None,
            "did-not-arm: --lease-nonce does not match live "
            f"session nonce ({live[:12]}…)",
            SUPERVISE_STOP_EXIT,
        )
    return live, None, None


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
    live_nonce, refusal, refusal_code = resolve_startup_lease_nonce(
        project_root=project_root,
        controller_label=label,
        explicit=str(getattr(args, "lease_nonce", None) or ""),
    )
    if not live_nonce:
        print(f"supervise: {refusal}", file=sys.stderr)
        return int(refusal_code or SUPERVISE_START_EXIT)
    test_mode = os.environ.get("GOALFLIGHT_TEST_MODE") == "1"
    heartbeat_s = float(
        getattr(args, "heartbeat_secs", DEFAULT_SUPERVISOR_HEARTBEAT_S)
        or DEFAULT_SUPERVISOR_HEARTBEAT_S
    )
    coverage_s = float(getattr(args, "coverage_secs", 0.0) or 0.0) or heartbeat_s
    if not test_mode:
        if not (
            MIN_SUPERVISOR_HEARTBEAT_S
            <= heartbeat_s
            <= MAX_SUPERVISOR_HEARTBEAT_S
        ):
            print(
                "supervise: heartbeat-secs must stay between "
                f"{MIN_SUPERVISOR_HEARTBEAT_S:g} and "
                f"{MAX_SUPERVISOR_HEARTBEAT_S:g}; "
                "faster risks host volume limiting and the periodic write "
                "must remain a bounded stdout peer check",
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
        debug=bool(getattr(args, "debug", False)),
    )
