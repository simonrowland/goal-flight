#!/usr/bin/env python3
"""Own the persistent wake pool as one tracked stdout feed.

The controller arms this once. It spawns the stream, backup doorbells, and
watchdog from ``goalflight_wake.coverage_rearm_commands``, multiplexes every
child's stdout line-by-line, restarts deaths, and stops on a dead lease nonce.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import errno
import json
import math
import os
from pathlib import Path
import select
import shlex
import signal
import sqlite3
import subprocess
import sys
import threading
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
# Failure-restart backoff. Exit 0 / rang stays at zero delay and is not
# this curve.
#
# Premise: a persistent resource fault (disk full, journal I/O) does not
# recover in one second. Three children restarting at 1 Hz is 180 attempts
# per minute against the failing filesystem, plus 180 identical wake
# records that drown real doorbells.
#
# Arithmetic: the first consecutive fast failure waits BACKOFF_INITIAL_S
# (1s). Each further fast failure doubles: 1, 2, 4, 8, 16, 32, 64, then
# the cap. Time to first cap from a crash loop:
# 1+2+4+8+16+32+64 = 127s.
#
# Cap BACKOFF_CAP_S = 120s. The supervisor's job is to notice recovery
# and re-arm, so the ceiling belongs in minutes, not hours. 120s matches
# the stream child's keepalive, so a failed slot never retries slower
# than a healthy stream proves liveness. The 3600s supervisor heartbeat
# is a last-ditch peer probe, not a recovery SLA — waiting an hour to
# retry would make the wake channel deaf. Sanity: three children at cap
# restart 1.5 times per minute total, versus 180/min at a flat 1s.
#
# Fast vs long-run: ran_s < LONG_LIVED_S (30s) escalates; a child that
# already ran 30s+ did useful work, so a later non-zero exit is one
# incident, not a crash loop — reset to INITIAL (1s), not zero. Zero
# delay is reserved for ACTION_REARM (exit 0 / rang). A long-lived
# non-zero exit still failed; putting it on the success path would
# make backoff_s=0 mean two things and would re-arm a slowly-dying
# child as fast as a doorbell ring. The extra 1s after a 30s+ run is
# not a tax on the wake path — rings already use 0. 30s is far above
# spawn-and-die (tens of ms) and far below a healthy doorbell wait.
# ACTION_REARM (including exit 0 / rang) resets to 0 so a recovered
# child is immediately responsive and one transient failure does not
# leave the slot slow.
#
# Give-up: not on generic failures. A silently-stopped listener is worse
# than a quiet retry. Cap the delay and collapse identical restart
# records instead. Explicit did-not-arm diagnostics remain the slot-stop
# for a child that proves it cannot run in this generation.
BACKOFF_INITIAL_S = 1.0
BACKOFF_CAP_S = 120.0
LONG_LIVED_S = 30.0
STREAM_LINE_MAX_BYTES = 511
TRANSIENT_DETECTOR_RETRY_S = 0.01
# ``poll`` EAGAIN/EWOULDBLOCK means the readiness service has no current
# capacity; it says nothing about the peer. Retry across real elapsed time
# instead of sampling the same instant three times. One second spans ordinary
# scheduler and descriptor-pressure hiccups while keeping a persistently
# unusable detector bounded far below the child/watchdog liveness intervals.
TRANSIENT_DETECTOR_RETRY_BUDGET_S = 1.0
# EAGAIN/EWOULDBLOCK on a nonblocking stdout write is "no current pipe
# capacity", not peer loss. A live controller can pause 100ms–2s while
# scheduling another turn; two 10ms sleeps (~20ms) cannot tell that pause
# from a dead reader. A dead reader keeps the pipe full forever AND
# typically raises POLLHUP/closed stdout (the b-248 detectors). Bound
# consecutive no-progress EAGAIN by wall clock: 5s is above a 2s
# scheduling pause and far below the follow-child 120s death detector
# and the 3600s supervisor heartbeat, so a live reader can drain without
# delaying genuine peer-loss past existing watchdog bounds. False from
# _stdio_peer_gone remains no evidence, never proof of liveness.
STDOUT_BACKPRESSURE_BUDGET_S = 5.0
# Controllers never act on the supervisor's own heartbeat — real worker
# events and kind=next wake them — so after the b-248 rounds its only
# load-bearing role is the periodic AUTHORITATIVE peer-probe write.
# Prompt peer-gone detection is selector/POLLHUP-based with the
# fail-closed detector choke point. The last-ditch write still protects
# the all-poll-detectors-fail-silent fallback (EPIPE on the next write).
# Default terse mode must not print that write: a controller-visible
# heartbeat is a Monitor wake (b-249 / b-202). --debug restores the
# printed record. Worst-case detection delay without a printed beat is
# one heartbeat period (now 3600s) plus the next real event write.
DEFAULT_SUPERVISOR_HEARTBEAT_S = 3600.0
MIN_SUPERVISOR_HEARTBEAT_S = 60.0
MAX_SUPERVISOR_HEARTBEAT_S = 4.0 * 3600
PERSISTENT_BACKUP_SLOTS_ENV = "GOALFLIGHT_PERSISTENT_BACKUP_SLOTS"
# ``follow`` writes a heartbeat before computing a possibly changed frontier.
# Hold the beat briefly so one pipe-read split does not create two wakes. The
# bound preserves anti-stall if projection work hangs; a late frontier is cached
# for the next beat and remains advisory-only.
STREAM_FRONTIER_GRACE_S = 1.0
# Follow already withholds an unchanged frontier until
# FOLLOW_FRONTIER_FLOOR_SECS (15 min). The Monitor-visible reminder is a
# task-store next line (ids + short titles), not a keepalive ping.
# Emit on first observation and on content change only. An identical
# payload is never reprinted (b-271); silence is correct. This 15-minute
# floor is that identity window. The 3600s supervisor interval is a
# silent peer probe, not a second next emit. Empty/unknown idle next
# records are not controller wakes. --chatty restores the raw
# heartbeat/frontier feed. --debug may print the old heartbeat record.
DEFAULT_NEXT_REPEAT_FLOOR_S = 15.0 * 60.0
NEXT_REMINDER_REPEAT_FLOOR_S = math.inf
# Escalations and listener health must survive even a routine record carrying
# the same envelope identity. Keep these types aligned with the message registry;
# messages imports this module for its CLI, so importing it here would cycle.
_ESCALATION_EVENT_TYPES = frozenset({"blocked", "user_confirm", "user_need"})
_PASSTHROUGH_EVENT_TYPES = frozenset(
    {
        "listener-dead",
        "listener-fault",
        "listener-exit",
        "watchdog-dead",
        "listener-degraded",
        "listener-recovered",
    }
)
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
_SLOT_STOP_REASONS = frozenset({"did-not-arm"})
_DIAGNOSTIC_EVENT_TYPES = frozenset({"listener-exit", "listener-fault"})
_IDLE_NEXT_STATES = frozenset({"empty", "unknown"})
_DASHBOARD_HINT_UNAVAILABLE = "next-task hint unavailable"
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


def _detector_error_policy(exc: BaseException) -> tuple[str, str]:
    """Classify detector I/O errors without weakening unknown-error closure."""
    if not isinstance(exc, OSError):
        return "persistent", type(exc).__name__
    error_number = exc.errno
    error_name = (
        errno.errorcode.get(error_number, str(error_number))
        if error_number is not None
        else type(exc).__name__
    )
    if error_number == errno.EPIPE:
        return "peer-gone", error_name
    if error_number == errno.EINTR:
        return "retry", error_name
    if error_number in {errno.EAGAIN, errno.EWOULDBLOCK}:
        return "retry-bounded", error_name
    return "persistent", error_name


def _utf8_completion(data: bytes, offset: int) -> bytes:
    """Return bytes needed to finish a code point split at ``offset``."""
    if offset <= 0 or offset >= len(data):
        return b""
    start = offset - 1
    while start >= 0 and data[start] & 0xC0 == 0x80:
        start -= 1
    if start < 0:
        return b""
    lead = data[start]
    if lead < 0x80:
        expected = 1
    elif lead & 0xE0 == 0xC0:
        expected = 2
    elif lead & 0xF0 == 0xE0:
        expected = 3
    elif lead & 0xF8 == 0xF0:
        expected = 4
    else:
        return b""
    written = offset - start
    if written >= expected:
        return b""
    return data[offset : start + expected]


class SuperviseHost(Protocol):
    now: float

    def running(self) -> bool: ...
    def live_nonce(self) -> str | None: ...
    def write_stdout(self, line: str) -> bool: ...
    def touch_stdout(self) -> bool: ...
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
    label: str
    child: Any = None
    backoff_s: float = 0.0
    next_start: float = 0.0
    stopped_reason: str | None = None


def _unique_slot_labels(kinds: list[str]) -> list[str]:
    """Name each slot uniquely. Unique kinds keep the kind; pools get kind-N.

    Persistent coverage repeats the ``backup`` kind once per missing doorbell
    (default two). Stream and watchdog appear once. Without a per-slot label
    those backups share a collapse gate and one child's failures suppress
    the other's record.
    """
    totals: dict[str, int] = {}
    for kind in kinds:
        totals[kind] = totals.get(kind, 0) + 1
    seen: dict[str, int] = {}
    labels: list[str] = []
    for kind in kinds:
        if totals[kind] == 1:
            labels.append(kind)
            continue
        seen[kind] = seen.get(kind, 0) + 1
        labels.append(f"{kind}-{seen[kind]}")
    return labels


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
    """Exponential failure delay; zero for re-arm; reset after a long-lived run."""
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


def _abandon_stdout_write(host: SuperviseHost) -> None:
    abandon = getattr(host, "abandon_stdout_write", None)
    if not callable(abandon):
        return
    try:
        abandon()
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        _report_stdout_detector(
            host,
            source="write",
            outcome="unavailable",
            detail=f"stdout pending write could not be abandoned: {exc}",
            error=type(exc).__name__,
        )


def _write_stdout(host: SuperviseHost, line: str, *, source: str) -> bool:
    """Write through the shared detector, retrying only known transients."""
    backpressure_started: float | None = None
    while True:
        progress_before = getattr(host, "stdout_write_progress", None)
        try:
            written = host.write_stdout(line)
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
            policy, error_name = _detector_error_policy(exc)
            if policy == "retry":
                _report_stdout_detector(
                    host,
                    source=source,
                    outcome="unknown",
                    detail=f"stdout write interrupted by {error_name}; retrying",
                )
                continue
            if policy == "retry-bounded":
                progress_after = getattr(host, "stdout_write_progress", None)
                if (
                    isinstance(progress_before, int)
                    and isinstance(progress_after, int)
                    and progress_after > progress_before
                ):
                    backpressure_started = None
                now = time.monotonic()
                if backpressure_started is None:
                    backpressure_started = now
                # Positive peer-gone evidence is terminal immediately.
                # False from stdio_peer_gone is no evidence, not liveness.
                peer_gone = False
                probe = getattr(host, "stdio_peer_gone", None)
                if callable(probe):
                    try:
                        peer_gone = bool(probe())
                    except (
                        AttributeError,
                        OSError,
                        TypeError,
                        ValueError,
                    ):
                        peer_gone = False
                if peer_gone:
                    _report_stdout_detector(
                        host,
                        source=source,
                        outcome="peer-gone",
                        detail=(
                            "controlling stdout closed during "
                            "backpressured write"
                        ),
                    )
                    _abandon_stdout_write(host)
                    return False
                elapsed = now - backpressure_started
                if elapsed < STDOUT_BACKPRESSURE_BUDGET_S:
                    _report_stdout_detector(
                        host,
                        source=source,
                        outcome="unknown",
                        detail=(
                            "stdout write has no current capacity "
                            f"({error_name}); retrying "
                            f"({elapsed:.3f}s/"
                            f"{STDOUT_BACKPRESSURE_BUDGET_S:.3f}s)"
                        ),
                    )
                    time.sleep(TRANSIENT_DETECTOR_RETRY_S)
                    continue
                _report_stdout_detector(
                    host,
                    source=source,
                    outcome="unavailable",
                    detail=(
                        "stdout write stalled for "
                        f"{elapsed:.3f}s under consecutive {error_name} "
                        f"with no peer-gone evidence: {exc}"
                    ),
                    error=error_name,
                )
            elif policy == "peer-gone":
                _report_stdout_detector(
                    host,
                    source=source,
                    outcome="peer-gone",
                    detail="controlling stdout closed during write",
                )
            else:
                _report_stdout_detector(
                    host,
                    source=source,
                    outcome="unavailable",
                    detail=f"stdout write failed: {error_name}: {exc}",
                    error=error_name,
                )
            _abandon_stdout_write(host)
            return False
        _report_stdout_detector(
            host,
            source=source,
            outcome="available" if written else "peer-gone",
            detail="" if written else "controlling stdout closed during write",
        )
        if not written:
            _abandon_stdout_write(host)
        return written


def _emit(host: SuperviseHost, record: dict[str, object]) -> bool:
    record_type = str(record.get("type") or "record")
    return _write_stdout(
        host,
        _supervise_line(record),
        source=f"write-{record_type}",
    )


def _note_supervise_exit(reason: str, detail: str = "") -> None:
    """Never exit mute: stderr carries the reason when stdout cannot."""
    message = f"goalflight supervise: {reason}"
    if detail:
        message = f"{message}: {detail}"
    try:
        sys.stderr.write(message + "\n")
        sys.stderr.flush()
    except (AttributeError, OSError, ValueError):
        pass


def _owned_coverage_label(slots: list[_Slot]) -> str:
    seen: list[str] = []
    for slot in slots:
        if slot.kind not in seen:
            seen.append(slot.kind)
    return "/".join(seen)


def _next_is_actionable_wake(record: dict[str, object]) -> bool:
    """Idle next records are not controller wakes.

    A disabled optional dashboard is a known idle condition. Other
    unavailable states, such as a malformed projection, remain actionable so
    the controller sees the fault rather than silently treating it as empty.
    """
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return False
    state = str(payload.get("state") or "")
    if state in _IDLE_NEXT_STATES:
        return False
    if state == "unavailable":
        detail = str(payload.get("detail") or "")
        return not detail.startswith(_DASHBOARD_HINT_UNAVAILABLE)
    return True


def _silent_peer_write(host: SuperviseHost) -> bool:
    """Last-ditch peer probe that must not complete a Monitor-visible line."""
    touch = getattr(host, "touch_stdout", None)
    if callable(touch):
        try:
            written = bool(touch())
        except OSError as exc:
            policy, error_name = _detector_error_policy(exc)
            if policy == "peer-gone":
                _report_stdout_detector(
                    host,
                    source="write-peer-probe",
                    outcome="peer-gone",
                    detail="controlling stdout closed during peer probe",
                    error=error_name,
                )
                return False
            _report_stdout_detector(
                host,
                source="write-peer-probe",
                outcome="unavailable",
                detail=f"stdout peer probe failed: {error_name}: {exc}",
                error=error_name,
            )
            return False
        _report_stdout_detector(
            host,
            source="write-peer-probe",
            outcome="available" if written else "peer-gone",
            detail="" if written else "controlling stdout closed during peer probe",
        )
        return written
    _probe_stdout_detector(host, source="write-peer-probe")
    return not host.stdout_detector_status().peer_gone


def _supervisor_rearm_command(
    *,
    project_root: Path | str,
    controller_label: str,
    lease_nonce: str,
    heartbeat_s: float,
    coverage_s: float,
    chatty: bool = False,
    debug: bool = False,
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
    if chatty:
        argv.append("--chatty")
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


def _own_stream_record(child: Any, line: str, *, kind: str) -> dict[str, object] | None:
    """Return one structural record authored by the stream child itself.

    A relayed mail headline may quote a heartbeat and an event payload may contain
    the same word.  Neither is the stream's own top-level signal, and a backup or
    watchdog child that happens to emit the same JSON shape is not the stream.
    """
    if str(getattr(child, "kind", "") or "") != "stream":
        return None
    text = str(line or "").strip()
    if not text.startswith("{"):
        return None
    try:
        record = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(record, dict) or str(record.get("kind") or "") != kind:
        return None
    payload = record.get("payload")
    return record if isinstance(payload, dict) else None


def _bounded_payload_text(value: object, *, max_bytes: int) -> str:
    encoded = str(value or "").encode("utf-8")
    if len(encoded) <= max_bytes:
        return encoded.decode("utf-8")
    return encoded[: max(0, max_bytes - 3)].decode("utf-8", "ignore") + "…"


def _next_reminder_items(source_payload: dict[str, object]) -> list[dict[str, object]]:
    """Bound id+title rows for one Monitor-visible next reminder line."""
    items: list[dict[str, object]] = []
    raw_items = source_payload.get("items")
    rows = raw_items if isinstance(raw_items, list) else []
    if not rows and (
        source_payload.get("id") not in (None, "")
        or source_payload.get("title") not in (None, "")
    ):
        rows = [source_payload]
    for raw in rows[:4]:
        if not isinstance(raw, dict):
            continue
        item: dict[str, object] = {}
        if raw.get("id") not in (None, ""):
            item["id"] = _bounded_payload_text(raw["id"], max_bytes=32)
        if raw.get("title") not in (None, ""):
            item["title"] = _bounded_payload_text(raw["title"], max_bytes=72)
        if item:
            items.append(item)
    return items


def _actionable_stream_wake(
    frontier: dict[str, object] | None,
) -> dict[str, object]:
    """Replace an idle keepalive with a task-store next reminder."""
    source = frontier.get("payload") if isinstance(frontier, dict) else None
    source_payload = source if isinstance(source, dict) else {}
    state = str(source_payload.get("state") or "unknown")
    payload: dict[str, object] = {
        "directive": "Nothing pending" if state == "empty" else "goal-flight next",
        "state": state,
    }
    if source_payload.get("id") not in (None, ""):
        payload["id"] = _bounded_payload_text(source_payload["id"], max_bytes=72)
    if source_payload.get("title") not in (None, ""):
        payload["title"] = _bounded_payload_text(
            source_payload["title"], max_bytes=240
        )
    if source_payload.get("detail") not in (None, "") and "title" not in payload:
        payload["detail"] = _bounded_payload_text(
            source_payload["detail"], max_bytes=240
        )
    items = _next_reminder_items(source_payload)
    if items:
        payload["items"] = items
    return {"kind": "next", "payload": payload}


def _next_payload_key(record: dict[str, object]) -> str:
    """Stable identity of a terse kind=next payload for repeat suppression.

    Keyed on the SUBJECT (directive + head row id), not the representation.
    The two frontier producers describe one frontier differently -- the follow
    child carries the head row alone, the supervisor snapshot carries up to
    four items -- and the projection state flips projected/stale on its own
    schedule.  Keying on the serialized payload made the repeat gate false on
    almost every beat, so the floor never engaged and every controller was
    woken by the same frontier several times an hour (battery-perf, 2026-09-12).
    """
    payload = record.get("payload")
    source = payload if isinstance(payload, dict) else {}
    head = source.get("id")
    if head in (None, ""):
        items = source.get("items")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            head = items[0].get("id")
    return json.dumps(
        {"directive": source.get("directive"), "id": head},
        sort_keys=True,
        default=str,
    )


def _restart_record_key(record: dict[str, object]) -> str:
    """Stable identity of a restart record, ignoring live/target/count churn."""
    return json.dumps(
        {
            "backoff_s": record.get("backoff_s"),
            "child": record.get("child"),
            "exit": record.get("exit"),
            "kind": record.get("kind"),
            "reason": record.get("reason"),
            "type": record.get("type"),
        },
        sort_keys=True,
        default=str,
    )


@dataclass
class _RepeatGate:
    """Emit the first copy of a key; suppress identical copies until floor_s.

    kind=next uses ``NEXT_REMINDER_REPEAT_FLOOR_S`` (inf) so an unchanged
    frontier is never reprinted (b-271). A content change still wakes
    immediately. Restart records use ``_RestartGate`` so a crash loop at
    a fixed backoff does not flood the same channel.
    """

    last_key: str | None = None
    last_at: float = field(default=-math.inf)

    def should_emit(self, key: str, *, now: float, floor_s: float) -> bool:
        if (
            self.last_key is not None
            and key == self.last_key
            and now - self.last_at < floor_s
        ):
            return False
        self.last_key = key
        self.last_at = now
        return True


@dataclass
class _RestartGroup:
    key: str
    record: dict[str, object]
    count: int
    first_at: float
    last_at: float


def _restart_group_record(group: _RestartGroup) -> dict[str, object]:
    """Stamp scale onto a restart batch without mutating the gate key.

    ``count`` is the number of child restarts this record represents.
    Records do not overlap: the first copy of a key is one record with
    ``count=1``, and a later collapse record counts only the copies held
    after that first. Summing ``count`` across ``type=restart`` records
    recovers the true restart total. ``window_s`` is the span of the
    copies in this record (0 for a single immediate first copy).
    """
    record = dict(group.record)
    record["count"] = group.count
    record["window_s"] = max(0.0, float(group.last_at) - float(group.first_at))
    return record


@dataclass
class _RestartGate:
    """Emit the first restart of a key immediately; collapse later copies.

    The first note of a key is written at once (count=1, window_s=0).
    Later identical keys inside floor_s accumulate as a pending group.
    When the group closes (key change, floor, or supervisor exit), the
    held copies emit as one record whose count is the number of
    suppressed restarts — not including the already-emitted first.
    Summing count across records recovers the true restart total.
    floor_s <= 0 emits every copy. One instance per slot label (unique
    kinds keep the kind name; a backup pool is backup-1, backup-2, …).
    """

    last_key: str | None = None
    window_start: float = field(default=-math.inf)
    pending: _RestartGroup | None = None

    def flush_at(self, floor_s: float) -> float | None:
        if self.pending is None:
            return None
        if floor_s <= 0:
            return self.pending.first_at
        return self.window_start + floor_s

    def note(
        self,
        key: str,
        record: dict[str, object],
        *,
        now: float,
        floor_s: float,
    ) -> list[dict[str, object]]:
        emits: list[dict[str, object]] = []
        if (
            floor_s > 0
            and self.last_key == key
            and now - self.window_start < floor_s
        ):
            pending = self.pending
            if pending is None:
                self.pending = _RestartGroup(
                    key=key,
                    record=record,
                    count=1,
                    first_at=now,
                    last_at=now,
                )
            else:
                pending.count += 1
                pending.last_at = now
                pending.record = record
            return []
        if floor_s > 0 and self.last_key == key and self.pending is not None:
            # Same key at or past the floor: include this copy in the
            # closing group so a straddle retry is counted once.
            pending = self.pending
            pending.count += 1
            pending.last_at = now
            pending.record = record
            emits.append(_restart_group_record(pending))
            self.pending = None
            self.last_key = None
            self.window_start = -math.inf
            return emits
        if self.pending is not None:
            emits.append(_restart_group_record(self.pending))
            self.pending = None
        emits.append(
            _restart_group_record(
                _RestartGroup(
                    key=key,
                    record=record,
                    count=1,
                    first_at=now,
                    last_at=now,
                )
            )
        )
        if floor_s <= 0:
            self.last_key = None
            self.window_start = -math.inf
            return emits
        self.last_key = key
        self.window_start = now
        return emits

    def flush(self) -> list[dict[str, object]]:
        pending = self.pending
        self.pending = None
        self.last_key = None
        self.window_start = -math.inf
        if pending is None:
            return []
        return [_restart_group_record(pending)]


def _parse_child_record(line: str) -> dict[str, object] | None:
    text = str(line or "").strip()
    if not text.startswith("{"):
        return None
    try:
        record = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _child_mail_rows(line: str) -> list[object]:
    """Normalize the mail-bearing parts of headline, event and arm records.

    Rings and diagnostics carry no mail. Pending reports also carry snapshot
    metadata, which must survive even when every item was delivered already.
    """
    text = str(line or "").strip()
    record = _parse_child_record(text)
    if record is None:
        if text.startswith("[") and "] " in text:
            event_type, _, receipt = text[1:].partition("] ")
            stream, separator, tail = receipt.partition(" seq=")
            seq = tail.partition(" — ")[0]
            if separator and stream and seq.isdecimal():
                return [{"stream_id": stream, "stream_seq": int(seq), "type": event_type}]
        return []
    if record.get("kind") == "event":
        payload = record.get("payload")
        return [payload] if isinstance(payload, dict) else []
    if record.get("kind") == "pending-at-arm":
        items = record.get("items")
        return items if isinstance(items, list) else []
    return []


def _backlog_capable_row(row: object) -> bool:
    if not isinstance(row, dict):
        return False
    event_type = str(row.get("event_type") or row.get("type") or "")
    return event_type not in _PASSTHROUGH_EVENT_TYPES


def _is_backlog_capable_line(line: str) -> bool:
    """Apply the same health-record exemption to every mail representation."""
    rows = _child_mail_rows(line)
    return bool(rows) and all(_backlog_capable_row(row) for row in rows)


def _envelope_identity(row: object) -> str | None:
    from goalflight_messages import MessageError, validate_stream_id

    if not isinstance(row, dict):
        return None
    stream = row.get("stream_id") or row.get("dispatch_id")
    seq = row.get("stream_seq")
    if stream == "None":
        return None
    # Reuse ingress validation: malformed or shortened display text cannot
    # establish envelope identity. Unknown identity always forwards.
    try:
        validate_stream_id(stream)
    except MessageError:
        return None
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        return None
    event_type = str(row.get("event_type") or row.get("type") or "")
    if event_type in _ESCALATION_EVENT_TYPES:
        # A routine collision cannot suppress the first escalation, but an
        # exact repeat of that escalation still belongs to this owner's memory.
        return f"envelope:{stream}:{seq}:{event_type}"
    return f"envelope:{stream}:{seq}"


def _backlog_line_identity(line: str) -> str:
    """Resolve envelope identity before any snapshot/kind fallback."""
    rows = _child_mail_rows(line)
    if len(rows) == 1:
        identity = _envelope_identity(rows[0])
        if identity is not None:
            return identity
    text = str(line or "").strip()
    record = _parse_child_record(text)
    if record is not None:
        kind = str(record.get("kind") or "record")
        version = record.get("cursor_version")
        if isinstance(version, int) and not isinstance(version, bool) and version >= 0:
            return f"snapshot:{kind}:{version}"
        return f"{kind}:{text}"
    return f"line:{text}"


@dataclass
class _ForwardingFrontierRead:
    done: threading.Event
    record: dict[str, object] | None = None
    expired: bool = False


def _start_forwarding_frontier_read(
    reader: Callable[[], dict[str, object]],
) -> _ForwardingFrontierRead:
    """Read the supervisor-only projection without blocking its wake deadline."""
    state = _ForwardingFrontierRead(done=threading.Event())

    def run() -> None:
        try:
            record = reader()
        except Exception:
            record = None
        payload = record.get("payload") if isinstance(record, dict) else None
        state.record = record if isinstance(payload, dict) else None
        state.done.set()

    threading.Thread(
        target=run,
        name="goalflight-forwarding-frontier",
        daemon=True,
    ).start()
    return state


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
    emit_depth: bool = False,
    debug: bool = False,
    chatty: bool = False,
    forwarding_frontier: Callable[[], dict[str, object]] | None = None,
    cursor_rewinds: Callable[[], dict[str, int]] | None = None,
    next_repeat_floor_s: float = DEFAULT_NEXT_REPEAT_FLOOR_S,
    on_startup_probe: Callable[[Path, str, str], str | None] | None = None,
) -> int:
    """Run until the lease dies, stdout breaks, or the host asks to stop."""
    nonce = str(lease_nonce or "").strip()
    rearm = _supervisor_rearm_command(
        project_root=project_root,
        controller_label=controller_label,
        lease_nonce=nonce,
        heartbeat_s=heartbeat_s,
        coverage_s=coverage_s,
        chatty=chatty,
        debug=debug,
    )

    def emit_recovery(record: dict[str, object]) -> bool:
        if not emit_depth:
            record.pop("live", None)
            record.pop("target", None)
        if _emit(host, record):
            return True
        try:
            sys.stderr.write(
                "goalflight supervise: recovery record could not be written to "
                f"stdout ({record.get('type', 'record')}: "
                f"{record.get('reason', 'unknown')}); re-arm with: {rearm}\n"
            )
            sys.stderr.flush()
        except (AttributeError, OSError, ValueError):
            pass
        return False

    def emit_stop(**fields: object) -> bool:
        record: dict[str, object] = {
            "kind": "supervise",
            "type": "stop",
            "rearm": rearm,
        }
        record.update(fields)
        return emit_recovery(record)

    if not nonce:
        if not emit_stop(
            reason="dead-lease-nonce",
            detail="lease nonce missing",
        ):
            return SUPERVISE_STOP_EXIT
        return SUPERVISE_STOP_EXIT
    if items is None:
        items = wake.coverage_supervise_items(
            project_root,
            controller_label=controller_label,
            lease_nonce=nonce,
        )
    if not items:
        if not emit_stop(
            reason="did-not-arm",
            detail="coverage_rearm_commands returned no children",
        ):
            return SUPERVISE_START_EXIT
        return SUPERVISE_START_EXIT
    slots = [
        _Slot(kind=kind, command=command, label=label, next_start=host.now)
        for (kind, command), label in zip(
            items,
            _unique_slot_labels([kind for kind, _command in items]),
            strict=True,
        )
    ]
    coverage_revision = 0
    repeat_floor_s = max(0.0, float(next_repeat_floor_s))
    restart_gates: dict[str, _RestartGate] = {}

    def coverage_changed() -> None:
        nonlocal coverage_revision
        coverage_revision += 1

    def emit_restart_records(records: list[dict[str, object]]) -> bool:
        for outgoing in records:
            if not _emit(host, outgoing):
                return False
        return True

    def emit_restart(record: dict[str, object]) -> bool:
        if not chatty and str(record.get("reason") or "") == "rang":
            return True
        child = str(record.get("child") or "")
        gate = restart_gates.setdefault(child, _RestartGate())
        key = _restart_record_key(record)
        return emit_restart_records(
            gate.note(key, record, now=host.now, floor_s=repeat_floor_s)
        )

    def stop_for_stdout_detector() -> int | None:
        """Define the one terminal policy for all peer-loss detector layers."""
        status = host.stdout_detector_status()
        failure = status.failure
        if failure is not None:
            live, target = _live_target(slots)
            emitted = emit_stop(
                reason="stdout-peer-detector-unavailable",
                scope="supervisor",
                detector=failure.source,
                error=failure.error,
                live=live,
                target=target,
                detail=failure.detail,
            )
            if not emitted:
                host.kill_all()
                return SUPERVISE_STOP_EXIT
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
            _note_supervise_exit(
                "reader-gone",
                "controlling stdout closed (EPIPE/monitor-drop)",
            )
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
            if not emit_stop(
                reason="dead-lease-nonce",
                scope="supervisor",
                live=live,
                target=target,
                detail="goalflight_session_status live nonce changed or vanished",
            ):
                return SUPERVISE_STOP_EXIT
            return SUPERVISE_STOP_EXIT
        if state == "unreadable":
            return None
        for slot in slots:
            if slot.stopped_reason is not None:
                continue
            if slot.child is not None or host.now < slot.next_start:
                continue
            try:
                slot.child = host.spawn(slot.kind, slot.command)
            except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
                slot.child = None
                slot.backoff_s = next_backoff(
                    slot.backoff_s,
                    ran_s=0.0,
                    action=ACTION_BACKOFF,
                )
                slot.next_start = host.now + slot.backoff_s
                coverage_changed()
                live, target = _live_target(slots)
                record: dict[str, object] = {
                    "kind": "supervise",
                    "type": "restart",
                    "child": slot.label,
                    "exit": None,
                    "reason": "spawn-failed",
                    "error": type(exc).__name__,
                    "detail": str(exc),
                    "backoff_s": slot.backoff_s,
                }
                if emit_depth:
                    record.update(live=live, target=target)
                if not emit_restart(record):
                    return stop_after_failed_write()
                continue
            setattr(slot.child, "kind", slot.kind)
            setattr(slot.child, "alive", True)
            setattr(slot.child, "armed", False)
        return None

    if on_startup_probe is None:
        stopped = spawn_due()
        if stopped is not None:
            host.kill_all()
            return stopped
    seq = 0
    reported_revision = -1
    reported_counts: tuple[int, int] | None = None
    last_observed_live: int | None = None

    def emit_coverage(*, force: bool = False) -> tuple[bool, bool]:
        nonlocal reported_counts, reported_revision, last_observed_live
        live, target = _live_target(slots)
        counts = (live, target)
        previous_live = last_observed_live
        last_observed_live = live
        if emit_depth:
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
        # Terse: only operator-actionable loss (e.g. 4/4 → 0/4), or --debug ticks.
        # A healthy rang/re-arm gap is live=0 with backoff 0 and no slot stop.
        # That is not operator action — restart records cover real deaths.
        rang_only_gap = all(
            slot.stopped_reason is None and slot.backoff_s <= 0
            for slot in slots
            if slot.child is None
        )
        actionable_loss = (
            previous_live is not None
            and previous_live > 0
            and live == 0
            and target > 0
            and not rang_only_gap
        )
        if not force and not actionable_loss:
            return True, False
        if force and not debug and not actionable_loss:
            return True, False
        record = {
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
        if not debug:
            return _silent_peer_write(host)
        record: dict[str, object] = {
            "kind": "supervise",
            "type": "heartbeat",
            "seq": seq,
        }
        if emit_depth:
            record.update(live=live, target=target)
        return _emit(host, record)

    if on_startup_probe is not None:
        # A migration must prove the controlling stdout before the caller may
        # release known-good incumbent coverage. Process construction alone is
        # not proof, so migration children are not spawned before this write.
        startup_probe_ok = _emit(
            host,
            {
                "kind": "supervise",
                "type": "probe",
                "reason": "stdout-peer-liveness",
            },
        )
    elif emit_depth:
        startup_probe_ok, _coverage_emitted = emit_coverage(force=True)
    else:
        # First successful arm is the one controller-visible startup write.
        # Heartbeat stays an internal peer probe unless --debug.
        startup_probe_ok = _emit(
            host,
            {
                "kind": "supervise",
                "type": "arm",
                "owned": _owned_coverage_label(slots),
            },
        )
    if not startup_probe_ok:
        return stop_after_failed_write()
    if on_startup_probe is not None:
        try:
            migration_failure = on_startup_probe(
                Path(project_root), controller_label, nonce
            )
        except Exception as exc:  # startup boundary: failure must be visible
            migration_failure = f"{type(exc).__name__}: {exc}"
        if migration_failure:
            emitted = emit_stop(
                reason="migration-release-failed",
                scope="supervisor",
                detail=str(migration_failure)[:180],
            )
            host.kill_all()
            return SUPERVISE_START_EXIT if emitted else SUPERVISE_STOP_EXIT
    if on_startup_probe is not None:
        stopped = spawn_due()
        if stopped is not None:
            host.kill_all()
            return stopped
    if on_startup_probe is not None and emit_depth:
        coverage_ok, _coverage_emitted = emit_coverage(force=True)
        if not coverage_ok:
            return stop_after_failed_write()
    if debug and not emit_heartbeat():
        return stop_after_failed_write()
    next_heartbeat = host.now + max(0.01, float(heartbeat_s))
    next_coverage = host.now + max(0.01, float(coverage_s))
    latest_frontier: dict[str, object] | None = None
    pending_stream_heartbeat: dict[str, object] | None = None
    pending_stream_heartbeat_due = float("inf")
    pending_stream_frontier: dict[str, object] | None = None
    active_forwarding_read: _ForwardingFrontierRead | None = None
    pending_forwarding_read: _ForwardingFrontierRead | None = None
    next_gate = _RepeatGate()
    # Scoped to this stdout owner, shared across every child and replacement
    # child. This is delivery memory, never journal acknowledgement. A new
    # supervisor starts empty and can recover any partially delivered batch.
    delivered_envelopes: dict[str, str] = {}
    seen_rewinds: dict[str, int] = {}

    def emit_pending_stream_wake(*, paired_frontier: bool = False) -> bool:
        nonlocal latest_frontier
        nonlocal pending_forwarding_read, pending_stream_frontier
        nonlocal pending_stream_heartbeat, pending_stream_heartbeat_due
        if pending_stream_heartbeat is None:
            return True
        frontier = latest_frontier
        child_payload = (
            pending_stream_frontier.get("payload")
            if isinstance(pending_stream_frontier, dict)
            else None
        )
        if forwarding_frontier is not None:
            if (
                isinstance(child_payload, dict)
                and child_payload.get("state") != "empty"
            ):
                frontier = pending_stream_frontier
                if (
                    pending_forwarding_read is not None
                    and not pending_forwarding_read.done.is_set()
                ):
                    pending_forwarding_read.expired = True
            elif (
                pending_forwarding_read is not None
                and pending_forwarding_read.done.is_set()
            ):
                frontier = pending_forwarding_read.record
            else:
                # The richer selection did not complete within this beat's
                # grace. Keep the cadence and preserve uncertainty.
                frontier = None
                if pending_forwarding_read is not None:
                    pending_forwarding_read.expired = True
        elif pending_stream_frontier is not None:
            frontier = pending_stream_frontier
        pending_stream_heartbeat = None
        pending_stream_heartbeat_due = float("inf")
        pending_stream_frontier = None
        pending_forwarding_read = None
        frontier_payload = (
            frontier.get("payload") if isinstance(frontier, dict) else None
        )
        if (
            forwarding_frontier is None
            and not paired_frontier
            and isinstance(frontier_payload, dict)
            and frontier_payload.get("state") == "empty"
        ):
            # A cached empty projection cannot prove that nothing appeared
            # during a slow current refresh. Preserve the wake, but never turn
            # that ambiguity into a false idle directive.
            frontier = None
        if isinstance(frontier, dict):
            latest_frontier = frontier
        record = _actionable_stream_wake(frontier)
        if not _next_is_actionable_wake(record):
            return True
        key = _next_payload_key(record)
        if not next_gate.should_emit(
            key, now=host.now, floor_s=NEXT_REMINDER_REPEAT_FLOOR_S
        ):
            return True
        return _emit(host, record)

    def emit_pending_restarts() -> bool:
        outgoing: list[dict[str, object]] = []
        for gate in restart_gates.values():
            outgoing.extend(gate.flush())
        return emit_restart_records(outgoing)

    def forward_child_line(child: Any, line: str) -> bool:
        nonlocal seen_rewinds
        record = _parse_child_record(line)
        rows = _child_mail_rows(line)
        if rows and cursor_rewinds is not None:
            try:
                rewinds = cursor_rewinds()
            except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError):
                # No current evidence cannot prove a duplicate. Preserve both
                # the output and old memory until a later successful read.
                text = line if line.endswith("\n") else line + "\n"
                return _write_stdout(host, text, source="write-child-output")
            changed_streams = {
                stream for stream, version in rewinds.items()
                if version != seen_rewinds.get(stream, 0)
            }
            if changed_streams:
                for identity, stream in tuple(delivered_envelopes.items()):
                    if stream in changed_streams:
                        del delivered_envelopes[identity]
            seen_rewinds = rewinds
        identities: dict[str, str] = {}
        if record is not None and record.get("kind") == "pending-at-arm":
            remaining = []
            for row in rows:
                identity = _envelope_identity(row)
                if (
                    _backlog_capable_row(row) and identity is not None
                    and (identity in delivered_envelopes or identity in identities)
                ):
                    continue
                remaining.append(row)
                if identity is not None:
                    identities[identity] = str(row.get("stream_id") or row.get("dispatch_id"))
            if len(remaining) != len(rows):
                # Keep cursor/advance and all other metadata intact; only
                # repeated mail items disappear from the report.
                record["items"] = remaining
                line = json.dumps(record, ensure_ascii=False)
        elif _is_backlog_capable_line(line):
            identity = _backlog_line_identity(line)
            if identity.startswith("envelope:"):
                if identity in delivered_envelopes:
                    return True
                identities[identity] = str(rows[0].get("stream_id") or rows[0].get("dispatch_id"))
        text = line if line.endswith("\n") else line + "\n"
        if not _write_stdout(host, text, source="write-child-output"):
            return False
        delivered_envelopes.update(identities)
        return True

    while host.running():
        # Every detector reports to _PeerLossDetector; stop_for_stdout_detector
        # is the sole terminal policy. The probes around wait are allowed to be
        # inconclusive, while registration failure or persistently unusable
        # poll means the fast detector is unavailable and fails closed. Every
        # write is the authoritative point-in-time peer check. The 3600-second
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
            emitted = emit_stop(
                reason="dead-lease-nonce",
                scope="supervisor",
                live=live,
                target=target,
                detail="goalflight_session_status live nonce changed or vanished",
            )
            host.kill_all()
            if not emitted:
                return SUPERVISE_STOP_EXIT
            return SUPERVISE_STOP_EXIT
        now = host.now
        wake_at = min(next_heartbeat, next_coverage)
        if pending_stream_heartbeat is not None:
            wake_at = min(wake_at, pending_stream_heartbeat_due)
        for slot in slots:
            if slot.child is None and slot.stopped_reason is None:
                wake_at = min(wake_at, slot.next_start)
        for gate in restart_gates.values():
            flush_at = gate.flush_at(repeat_floor_s)
            if flush_at is not None:
                wake_at = min(wake_at, flush_at)
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
            if not chatty:
                heartbeat = _own_stream_record(child, line, kind="heartbeat")
                if heartbeat is not None:
                    if pending_stream_heartbeat is not None:
                        if not emit_pending_stream_wake():
                            return stop_after_failed_write()
                    pending_stream_heartbeat = heartbeat
                    pending_stream_heartbeat_due = (
                        host.now + STREAM_FRONTIER_GRACE_S
                    )
                    pending_stream_frontier = None
                    pending_forwarding_read = None
                    if forwarding_frontier is not None:
                        if (
                            active_forwarding_read is None
                            or active_forwarding_read.done.is_set()
                        ):
                            active_forwarding_read = (
                                _start_forwarding_frontier_read(
                                    forwarding_frontier
                                )
                            )
                        if not active_forwarding_read.expired:
                            pending_forwarding_read = active_forwarding_read
                    continue
                frontier = _own_stream_record(child, line, kind="frontier")
                if frontier is not None:
                    if pending_stream_heartbeat is None:
                        latest_frontier = frontier
                        continue
                    pending_stream_frontier = frontier
                    frontier_payload = frontier.get("payload")
                    forwarding_ready = (
                        pending_forwarding_read is not None
                        and pending_forwarding_read.done.is_set()
                    )
                    if (
                        forwarding_frontier is None
                        or not isinstance(frontier_payload, dict)
                        or frontier_payload.get("state") != "empty"
                        or forwarding_ready
                    ):
                        if not emit_pending_stream_wake(paired_frontier=True):
                            return stop_after_failed_write()
                    continue
            if not forward_child_line(child, line):
                return stop_after_failed_write()
        stream_exited = any(
            str(getattr(event.child, "kind", "") or "") == "stream"
            for event in result.exits
        )
        if pending_stream_heartbeat is not None and (
            stream_exited or host.now >= pending_stream_heartbeat_due
        ):
            if not emit_pending_stream_wake():
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
                emitted = emit_stop(
                    reason=reason,
                    scope=scope,
                    child=slot.label,
                    exit=event.returncode,
                    live=live,
                    target=target,
                    detail=str(event.output or "").strip()[:180],
                )
                if not emitted:
                    host.kill_all()
                    return SUPERVISE_STOP_EXIT
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
            record: dict[str, object] = {
                "kind": "supervise",
                "type": "restart",
                "child": slot.label,
                "exit": event.returncode,
                "reason": reason,
                "backoff_s": delay,
            }
            if emit_depth:
                record.update(live=live, target=target)
            if not emit_restart(record):
                return stop_after_failed_write()
        for gate in list(restart_gates.values()):
            flush_at = gate.flush_at(repeat_floor_s)
            if flush_at is None or host.now < flush_at:
                continue
            if not emit_restart_records(gate.flush()):
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
            if not emit_pending_restarts():
                return stop_after_failed_write()
            host.kill_all()
            return stopped

    if not emit_pending_restarts():
        return stop_after_failed_write()
    signum = getattr(host, "stop_signum", None)
    if isinstance(signum, int) and signum > 0:
        live, target = _live_target(slots)
        # SIGTERM, SIGINT, and SIGHUP get one hint while stdout is still open.
        # SIGKILL cannot be caught, so that hard-kill gap cannot emit a hint.
        if not emit_recovery(
            {
                "kind": "supervise",
                "type": "exit",
                "reason": _signal_reason(signum),
                "live": live,
                "target": target,
                "rearm": rearm,
            }
        ):
            host.kill_all()
            return SUPERVISE_STOP_EXIT
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


class _JournalWALHolder:
    """Keep WAL sidecars alive without pinning a read transaction."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: sqlite3.Connection | None = None
        self._identity: tuple[int, int] | None = None
        self.refresh()

    def close(self) -> None:
        connection, self.connection = self.connection, None
        self._identity = None
        if connection is not None:
            connection.close()

    def refresh(self) -> None:
        connection = None
        try:
            stat = self.path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if self.connection is not None and self._identity == identity:
                return
            self.close()
            connection = sqlite3.connect(
                f"{self.path.as_uri()}?mode=rw", uri=True,
                timeout=0, isolation_level=None,
            )
            connection.execute("PRAGMA query_only = ON")
            # connect() alone is lazy in SQLite too. Finish a schema read to
            # attach to the WAL, then leave no statement or transaction open.
            connection.execute("SELECT rootpage FROM sqlite_schema LIMIT 1").fetchall()
            stat = self.path.stat()
            if (stat.st_dev, stat.st_ino) != identity:
                return
            self.connection, connection = connection, None
            self._identity = identity
        except (OSError, sqlite3.Error):
            # No contention wait or bootstrap here: retry on the next tick.
            self.close()
        finally:
            if connection is not None:
                connection.close()


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
        self._journal_holder: _JournalWALHolder | None = None
        self._stdout_detector = _PeerLossDetector()
        # Consecutive poll EAGAIN is one host-level outage, even when shorter
        # semantic waits return to the supervisor loop. Only a successful
        # poll clears this timestamp; loop turnover is not detector recovery.
        self._stdout_poll_transient_started: float | None = None
        self._stdout_pending: tuple[object, str, bytes, int, int] | None = None
        self._stdout_needs_delimiter = False
        self._stdout_recovery_completion = b""
        self.stdout_write_progress = 0
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
        # Flagging alone can leave select() asleep for the 3600-second heartbeat
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
        if not self._stop and self._journal_holder is not None:
            self._journal_holder.refresh()
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
        if waiters is None:
            # UNKNOWN is not "no live waiters": do not mark children armed
            # without a determinate pid match, and do not treat the set as
            # empty.
            return
        if not waiters:
            return
        pids = {int(row.pid) for row in waiters}
        for child in alive:
            if child.pid in pids:
                child.armed = True

    def touch_stdout(self) -> bool:
        """Detect a dead controlling reader without completing a stdout line."""
        return not self.stdio_peer_gone()

    def write_stdout(self, line: str) -> bool:
        text = line if line.endswith("\n") else line + "\n"
        stream = sys.stdout
        if getattr(stream, "buffer", None) is None:
            stream.write(text)
            stream.flush()
            return True
        pending = self._stdout_pending
        try:
            stdout_fd = stream.fileno()
        except (AttributeError, OSError, ValueError):
            if pending is not None:
                raise
            stream.write(text)
            stream.flush()
            return True
        if pending is None:
            prefix = self._stdout_recovery_completion
            leading_completion = len(prefix)
            if self._stdout_needs_delimiter:
                prefix += b"\n"
            self._stdout_needs_delimiter = False
            self._stdout_recovery_completion = b""
            data = prefix + text.encode("utf-8")
            offset = 0
        else:
            (
                pending_stream,
                pending_text,
                data,
                offset,
                leading_completion,
            ) = pending
            if pending_stream is not stream or pending_text != text:
                raise RuntimeError("stdout retry does not match pending write")
        while offset < len(data):
            try:
                written = os.write(stdout_fd, data[offset:])
            except OSError:
                self._stdout_pending = (
                    stream,
                    text,
                    data,
                    offset,
                    leading_completion,
                )
                raise
            if written <= 0:
                self._stdout_pending = None
                return False
            offset += written
            self.stdout_write_progress += written
            self._stdout_pending = (
                stream,
                text,
                data,
                offset,
                leading_completion,
            )
        self._stdout_pending = None
        return True

    def abandon_stdout_write(self) -> None:
        pending = self._stdout_pending
        if pending is not None and pending[3] > 0:
            data, offset, leading_completion = pending[2], pending[3], pending[4]
            if offset < leading_completion:
                self._stdout_recovery_completion = data[offset:leading_completion]
            else:
                self._stdout_recovery_completion = _utf8_completion(data, offset)
            self._stdout_needs_delimiter = True
        self._stdout_pending = None

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
        # an implicit 3600-second fallback.
        remaining = max(0.0, deadline - time.monotonic())
        if self.stdout_detector_status().failure is not None:
            remaining = 0.0
        ready: list[int] = []
        if fdmap and self.stdout_detector_status().failure is None:
            while True:
                try:
                    poll_timeout_ms = math.ceil(
                        max(0.0, deadline - time.monotonic()) * 1000.0
                    )
                    events_ready = poller.poll(poll_timeout_ms)
                except (OSError, ValueError) as exc:
                    policy, error_name = _detector_error_policy(exc)
                    if policy == "retry":
                        self.report_stdout_detector(
                            "poll",
                            "unknown",
                            f"stdout poll interrupted by {error_name}; retrying",
                        )
                        continue
                    if policy == "retry-bounded":
                        now = time.monotonic()
                        try:
                            peer_gone = self.stdio_peer_gone()
                        except (AttributeError, OSError, TypeError, ValueError):
                            peer_gone = False
                        if peer_gone:
                            self.report_stdout_detector(
                                "poll",
                                "peer-gone",
                                "controlling stdout peer-gone probe confirmed closure",
                            )
                            break
                        if self._stdout_poll_transient_started is None:
                            self._stdout_poll_transient_started = now
                        transient_started = self._stdout_poll_transient_started
                        transient_deadline = (
                            transient_started
                            + TRANSIENT_DETECTOR_RETRY_BUDGET_S
                        )
                        # The host-level failure window outranks this call's
                        # semantic deadline. Thus continuous EAGAIN becomes
                        # terminal after the budget plus retry/scheduler delay,
                        # however often coverage wakes turn the loop over.
                        if now >= transient_deadline:
                            elapsed = now - transient_started
                            self.report_stdout_detector(
                                "poll",
                                "unavailable",
                                "stdout poll failed persistently for "
                                f"{elapsed:.3f}s under consecutive {error_name} "
                                f"with no peer-gone evidence: {exc}",
                                error_name,
                            )
                            break
                        # The semantic wait itself is complete. Preserve the
                        # inconclusive detector observation and retry it on the
                        # next supervisor iteration without resetting the
                        # host-level window; an expired zero-time wait is not
                        # evidence that the detector recovered.
                        if now >= deadline:
                            self.report_stdout_detector(
                                "poll",
                                "unknown",
                                "stdout poll temporarily unavailable at wait deadline",
                            )
                            break
                        elapsed = now - transient_started
                        self.report_stdout_detector(
                            "poll",
                            "unknown",
                            "stdout poll temporarily unavailable; retrying "
                            f"({elapsed:.3f}s/"
                            f"{TRANSIENT_DETECTOR_RETRY_BUDGET_S:.3f}s)",
                        )
                        time.sleep(
                            min(
                                TRANSIENT_DETECTOR_RETRY_S,
                                max(0.0, transient_deadline - now),
                                max(0.0, deadline - now),
                            )
                        )
                        continue
                    self.report_stdout_detector(
                        "poll",
                        "unavailable",
                        f"stdout poll failed: {error_name}: {exc}",
                        error_name,
                    )
                    break
                # Reset policy: only a blocking poll (timeout > 0) that
                # returns without EAGAIN may clear the consecutive-failure
                # window. This poller is multiplexed across stdout, children,
                # and the signal pipe, so a poll(0) that returns events is
                # still only instantaneous readiness — often of a different
                # fd — not proof the blocking poll recovered. poll(0) must
                # not reset a budget opened by positive-timeout EAGAIN and
                # must not be published as definite available. Events from
                # poll(0) are still processed below.
                poll_clears_budget = poll_timeout_ms > 0
                if poll_clears_budget:
                    self._stdout_poll_transient_started = None
                    self.report_stdout_detector("poll", "available")
                else:
                    self.report_stdout_detector(
                        "poll",
                        "unknown",
                        "zero-timeout poll is not evidence the blocking "
                        "readiness service recovered",
                    )
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


def _renew_controller_lease_before_arm(
    *,
    project_root: Path | str,
    controller_label: str,
    nonce: str,
) -> str | None:
    """Extend the live lease before supervise arms.

    Only the pinned active generation may renew. A dead holder refuses without
    expiring journal or coverage state. If the holder dies after the liveness
    check, the renewed lease simply lapses at its next deadline; no generation
    is created. Journal unreadability retains the runtime probe's retry path.
    """
    import goalflight_journal  # type: ignore

    unavailable = (
        goalflight_journal.JournalBusy,
        goalflight_journal.JournalDisappeared,
        goalflight_journal.JournalIOError,
    )
    try:
        authority = goalflight_journal.Journal(project_root)
        lease = authority.active_lease(controller_label)
    except unavailable:
        return nonce
    if lease is None or lease.nonce != nonce:
        return None
    if wake.lease_holder_alive(
        Path(project_root), controller_label=controller_label,
        lease_nonce=nonce, prune_dead=False,
    ) is False:
        return None
    try:
        result = authority.renew_active_lease(
            controller_label,
            nonce=lease.nonce,
            generation=lease.generation,
        )
    except unavailable:
        return nonce
    if result.disposition == goalflight_journal.WriteDisposition.CAS_LOST:
        return None
    if not result.committed or result.value is None:
        return nonce
    return str(result.value.nonce)


def cmd_supervise(
    args: Any,
    *,
    forwarding_frontier: Callable[[Path], dict[str, object]] | None = None,
    before_renewal: Callable[[Path, str, str], str | None] | None = None,
    on_startup_probe: Callable[[Path, str, str], str | None] | None = None,
) -> int:
    """CLI entry used by goalflight_messages.py supervise."""
    import goalflight_session_status as sessions  # type: ignore
    import goalflight_task  # type: ignore
    import goalflight_journal  # type: ignore

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
    if on_startup_probe is not None:
        slot_state, had_slot = wake.supervisor_slot_probe(
            project_root,
            controller_label=label,
            generation_key=live_nonce,
        )
        if had_slot or slot_state == wake.SUPERVISOR_UNKNOWN:
            existing = slot_state
        else:
            listing = wake._process_listing()
            if listing is not None:
                listing = [
                    (pid, command)
                    for pid, command in listing
                    if pid != os.getpid()
                ]
            existing = wake._supervisor_generation_state_from_listing(
                listing,
                project_root=project_root,
                controller_label=label,
                lease_nonce=live_nonce,
            )
        if existing != wake.SUPERVISOR_ABSENT:
            takeover = bool(getattr(args, "takeover", False))
            if existing == wake.SUPERVISOR_RUNNING:
                detail = (
                    "takeover refused: an existing supervisor remains live; "
                    "wake coverage was not verified; run --list-controllers"
                    if takeover
                    else "an existing supervisor remains live; wake coverage "
                    "was not verified; run --list-controllers"
                )
            else:
                detail = (
                    "takeover refused: supervisor owner death is not proven "
                    "by PID + start token; run --list-controllers"
                    if takeover
                    else "existing supervisor state is indeterminate; wake "
                    "coverage was not verified; run --list-controllers or "
                    "retry with --takeover only after PID + start token "
                    "prove the owner is dead"
                )
            print(
                f"supervise: did-not-arm: {detail}",
                file=sys.stderr,
            )
            return SUPERVISE_START_EXIT
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
    if before_renewal is not None:
        refusal = before_renewal(project_root, label, live_nonce)
        if refusal:
            print(f"supervise: did-not-arm: {refusal}", file=sys.stderr)
            return SUPERVISE_START_EXIT
    # Eligibility refusals must not extend the lease. Later startup I/O failures
    # (including broken stdout or child-start failure) can follow renewal; this
    # is not an atomic arm operation. Release still waits for stdout proof.
    renewed_nonce = _renew_controller_lease_before_arm(
        project_root=project_root,
        controller_label=label,
        nonce=live_nonce,
    )
    if not renewed_nonce:
        print(
            "supervise: did-not-arm: controller lease lost before arm",
            file=sys.stderr,
        )
        return SUPERVISE_STOP_EXIT
    live_nonce = renewed_nonce
    host = RealHost(
        project_root=project_root,
        controller_label=label,
        lease_nonce=live_nonce,
    )
    supervisor_registration = None
    try:
        try:
            supervisor_registration = wake.register_supervisor_waiter(
                project_root,
                controller_label=label,
                generation_key=live_nonce,
            )
        except (BlockingIOError, OSError, RuntimeError, ValueError) as exc:
            print(
                "supervise: did-not-arm: supervisor slot could not be claimed; "
                f"wake coverage was not verified: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return SUPERVISE_START_EXIT
        reader = goalflight_journal.Journal.open_reader(
            project_root,
            persistent=True,
            retry_budget_s=0,
            open_retry_budget_s=0,
        )
    except (
        goalflight_journal.JournalBusy,
        goalflight_journal.JournalDisappeared,
        goalflight_journal.JournalIOError,
        goalflight_journal.JournalIntegrityError,
        goalflight_journal.JournalUpgradeRequired,
    ) as exc:
        if supervisor_registration is not None:
            supervisor_registration.close()
        print(f"supervise: journal holder unavailable: {exc}", file=sys.stderr)
        return SUPERVISE_START_EXIT

    def cursor_rewinds() -> dict[str, int]:
        # Read-only and without contention waits: mail/peer handling must stay
        # responsive even when the journal cannot currently establish identity.
        return reader._cursor_rewinds(label)

    try:
        # Independent of the lazy mail reader: attach before the first mail.
        host._journal_holder = _JournalWALHolder(reader.path)
        return run_supervisor(
            project_root=project_root,
            controller_label=label,
            lease_nonce=live_nonce,
            host=host,
            cursor_rewinds=cursor_rewinds,
            heartbeat_s=heartbeat_s,
            coverage_s=coverage_s,
            emit_depth=bool(getattr(args, "chatty", False)),
            debug=bool(getattr(args, "debug", False)),
            chatty=bool(getattr(args, "chatty", False)),
            forwarding_frontier=(
                (lambda: forwarding_frontier(project_root))
                if forwarding_frontier is not None
                else None
            ),
            on_startup_probe=on_startup_probe,
        )
    finally:
        if supervisor_registration is not None:
            supervisor_registration.close()
        if host._journal_holder is not None:
            host._journal_holder.close()
        connection = getattr(reader, "_reader_connection", None)
        if connection is not None:
            reader._reader_connection = None
            reader._reader_pid = None
            if connection.in_transaction:
                connection.rollback()
            connection.close()
