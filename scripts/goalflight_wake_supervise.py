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
# records instead. PERMANENT_UNARMED_FAULTS remains the slot-stop for
# repeated never-armed exit-2, where that slot cannot work.
BACKOFF_INITIAL_S = 1.0
BACKOFF_CAP_S = 120.0
LONG_LIVED_S = 30.0
STREAM_LINE_MAX_BYTES = 511
PERMANENT_UNARMED_FAULTS = 3
TRANSIENT_DETECTOR_FAILURE_LIMIT = 3
TRANSIENT_DETECTOR_RETRY_S = 0.01
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
# fail-closed detector choke point. This write still protects the
# all-poll-detectors-fail-silent fallback, where EPIPE on the next write
# is the last detector. Worst-case detection delay is one heartbeat
# period (now 3600s). That is acceptable for an already-multiply-degraded
# case: the fast poll path and POLLHUP already failed silent, so waiting
# one hour for the last-ditch write is better than waking the controller
# every 25 minutes for a record it never acts on.
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
# FOLLOW_FRONTIER_FLOOR_SECS (15 min). Terse mode used to re-emit that
# cached frontier as kind=next on every 120s keepalive, so a verbatim-
# identical payload cost a full controller wake (b-271). Keep the
# keepalive cadence for CHANGED content; suppress unchanged repeats
# until this floor so the anti-stall beat still exists. --chatty
# restores the raw heartbeat/frontier feed and therefore the raw cadence.
DEFAULT_NEXT_REPEAT_FLOOR_S = 15.0 * 60.0
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


def _actionable_stream_wake(
    frontier: dict[str, object] | None,
) -> dict[str, object]:
    """Replace an idle keepalive with the latest action-bearing frontier."""
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
    return {"kind": "next", "payload": payload}


def _next_payload_key(record: dict[str, object]) -> str:
    """Stable identity of a terse kind=next payload for repeat suppression."""
    return json.dumps(record.get("payload"), sort_keys=True, default=str)


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

    kind=next uses this so an unchanged idle frontier does not wake the
    controller every keepalive. Restart records use ``_RestartGate`` so a
    crash loop at a fixed backoff does not flood the same channel and the
    emitted copy still carries occurrence count. One instance per stream
    (next); a changed key emits immediately.
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
    """Stamp scale onto a collapsed restart without mutating the gate key."""
    record = dict(group.record)
    record["count"] = group.count
    record["window_s"] = max(0.0, float(group.last_at) - float(group.first_at))
    return record


@dataclass
class _RestartGate:
    """Hold identical restarts until the group closes; emit with count.

    Same key inside floor_s accumulates. A changed key flushes the previous
    group so a new failure reason is not swallowed. floor_s <= 0 emits every
    copy (tests and --chatty-adjacent zero-floor). The pending group also
    flushes at supervisor exit and when the floor deadline wakes the loop,
    so a collapse is never a contentless "this happened, some number of
    times." One instance per child identity.
    """

    pending: _RestartGroup | None = None

    def flush_at(self, floor_s: float) -> float | None:
        if self.pending is None:
            return None
        if floor_s <= 0:
            return self.pending.first_at
        return self.pending.first_at + floor_s

    def note(
        self,
        key: str,
        record: dict[str, object],
        *,
        now: float,
        floor_s: float,
    ) -> list[dict[str, object]]:
        emits: list[dict[str, object]] = []
        pending = self.pending
        if pending is not None and pending.key == key and floor_s > 0:
            pending.count += 1
            pending.last_at = now
            pending.record = record
            if now - pending.first_at < floor_s:
                return []
            emits.append(_restart_group_record(pending))
            self.pending = None
            return emits
        if pending is not None:
            emits.append(_restart_group_record(pending))
            self.pending = None
        group = _RestartGroup(
            key=key,
            record=record,
            count=1,
            first_at=now,
            last_at=now,
        )
        if floor_s <= 0:
            emits.append(_restart_group_record(group))
            return emits
        self.pending = group
        return emits

    def flush(self) -> list[dict[str, object]]:
        pending = self.pending
        self.pending = None
        if pending is None:
            return []
        return [_restart_group_record(pending)]


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
    next_repeat_floor_s: float = DEFAULT_NEXT_REPEAT_FLOOR_S,
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
        _Slot(kind=kind, command=command, next_start=host.now)
        for kind, command in items
    ]

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
        if not emit_depth:
            # Option A: coverage carries only listener depth, so terse mode has
            # no informational record to emit. Its depth/revision suppression
            # key therefore applies only when those values are in the payload.
            # Startup uses an explicit probe below for the required peer write;
            # restart and stop paths already attempt their own meaningful write.
            return True, False
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
        record: dict[str, object] = {
            "kind": "supervise",
            "type": "heartbeat",
            "seq": seq,
        }
        if emit_depth:
            record.update(live=live, target=target)
        return _emit(host, record)

    if emit_depth:
        startup_probe_ok, _coverage_emitted = emit_coverage(force=True)
    else:
        # Terse startup still needs an actual stdout write to detect a dead
        # controller. Name that operational write instead of emitting empty
        # coverage or changing the scheduled heartbeat interval.
        startup_probe_ok = _emit(
            host,
            {
                "kind": "supervise",
                "type": "probe",
                "reason": "stdout-peer-liveness",
            },
        )
    if not startup_probe_ok or (debug and not emit_heartbeat()):
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
    restart_gates: dict[str, _RestartGate] = {}
    repeat_floor_s = max(0.0, float(next_repeat_floor_s))

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
        key = _next_payload_key(record)
        if not next_gate.should_emit(key, now=host.now, floor_s=repeat_floor_s):
            return True
        return _emit(host, record)

    def emit_restart_records(records: list[dict[str, object]]) -> bool:
        for outgoing in records:
            if not _emit(host, outgoing):
                return False
        return True

    def emit_restart(record: dict[str, object]) -> bool:
        child = str(record.get("child") or "")
        gate = restart_gates.setdefault(child, _RestartGate())
        key = _restart_record_key(record)
        return emit_restart_records(
            gate.note(key, record, now=host.now, floor_s=repeat_floor_s)
        )

    def emit_pending_restarts() -> bool:
        outgoing: list[dict[str, object]] = []
        for gate in restart_gates.values():
            outgoing.extend(gate.flush())
        return emit_restart_records(outgoing)

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
            text = line if line.endswith("\n") else line + "\n"
            if not _write_stdout(host, text, source="write-child-output"):
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
                emitted = emit_stop(
                    reason=reason,
                    scope=scope,
                    child=slot.kind,
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
                "child": slot.kind,
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
            bounded_failures = 0
            while True:
                try:
                    events_ready = poller.poll(
                        math.ceil(
                            max(0.0, deadline - time.monotonic()) * 1000.0
                        )
                    )
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
                        bounded_failures += 1
                        if bounded_failures < TRANSIENT_DETECTOR_FAILURE_LIMIT:
                            self.report_stdout_detector(
                                "poll",
                                "unknown",
                                "stdout poll temporarily unavailable; retrying "
                                f"({bounded_failures}/"
                                f"{TRANSIENT_DETECTOR_FAILURE_LIMIT})",
                            )
                            continue
                        self.report_stdout_detector(
                            "poll",
                            "unavailable",
                            "stdout poll failed persistently after "
                            f"{bounded_failures} consecutive {error_name} "
                            f"attempts: {exc}",
                            error_name,
                        )
                        break
                    self.report_stdout_detector(
                        "poll",
                        "unavailable",
                        f"stdout poll failed: {error_name}: {exc}",
                        error_name,
                    )
                    break
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


def cmd_supervise(
    args: Any,
    *,
    forwarding_frontier: Callable[[Path], dict[str, object]] | None = None,
) -> int:
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
        emit_depth=bool(getattr(args, "chatty", False)),
        debug=bool(getattr(args, "debug", False)),
        chatty=bool(getattr(args, "chatty", False)),
        forwarding_frontier=(
            (lambda: forwarding_frontier(project_root))
            if forwarding_frontier is not None
            else None
        ),
    )
