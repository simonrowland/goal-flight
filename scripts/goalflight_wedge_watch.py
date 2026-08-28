"""Two-tier watchlist for an alive-but-stuck worker.

Cheap path (every poll, every worker): one ``stat`` of the tail (mtime + size).
A worker whose tail is still moving, or has been idle for less than the
probation interval, is ``live`` and pays for nothing else.

Escalation: when the tail has been idle longer than probation, the worker
joins a watchlist. Only then do we count worktree writes since the last
observation, difference process-group CPU-seconds, and look for an
ESTABLISHED provider socket.

Verdicts are three-state: ``live`` / ``wedged`` / ``UNKNOWN``. The
conjunction of tail-idle + zero tree writes + ~0 CPU-seconds is necessary
but not sufficient for ``wedged``. An ESTABLISHED provider socket — or the
inability to determine socket state cheaply — is ``UNKNOWN`` (waiting, or
cannot tell). Unknown retains; this module never kills.

Probation interval (``DEFAULT_PROBATION_S``) is derived from historical
successful dispatches, not a round-number guess. See the constant.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
from typing import Callable

from goalflight_liveness import cputime_delta_seconds, pgroup_cputime_snapshot


# Derived 2026-08-28 from the machine ledger at /tmp/goal-flight-501.
#
# Tail files are append-only streams with no write-time index. Codex ISO
# prefixes in tail *content* are not write times (they produced multi-day
# "gaps" from cached/historical log lines). Grok/moonshot/cursor tails have
# no timestamps at all.
#
# What *is* a write-gap observation: successful (terminal ``complete``)
# workers that tripped the previous 900s stall detector and then recovered
# (tail grew, worker completed). Those quiet periods were, by construction,
# not wedges.
#
# Per-engine complete n: grok 719, codex 770, moonshot 19, cursor 2.
# Codex/moonshot/cursor: 0 recovered stalls among complete → max tail-idle
# < 900s (p99 below the old detector). moonshot/cursor n is too thin for a
# split. Grok is the heavy tail:
#
#   700/719 never reached 900s (right-censored at 899s)
#   19 recovered: max tail_age_s per dispatch
#     900.0, 900.2, 900.4, 900.7, 901.0, 901.2, 901.4, 901.7, 901.9, 902.1,
#     947.7, 969.7, 1093.3, 1099.0, 1998.6, 2029.4, 2052.3, 2066.4, 2398.7
#   mixed max-idle (non-stall = 899s): n=719 median=899s p95=899s
#     p99=965.7s max=2398.7s
#
# Global probation is grok-driven (the only engine with measured long
# successful quiet). 1080s sits above that p99. The five grok workers that
# were quiet 33–40 minutes still join the watchlist; the conjunction (CPU /
# tree / socket) is what keeps them from a false ``wedged``.
DEFAULT_PROBATION_S = 1080.0
CPU_EPSILON_S = 0.05
SOCKET_LSOF_TIMEOUT_S = 1.0
VERDICT_LIVE = "live"
VERDICT_WEDGED = "wedged"
VERDICT_UNKNOWN = "UNKNOWN"
SOCKET_PROVIDER = "provider"
SOCKET_NONE = "none"
SOCKET_UNKNOWN = "unknown"

_TREE_SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".tox",
        ".ruff_cache",
        "dist",
        "build",
        ".eggs",
        ".goal-flight",
    }
)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0", "::"})
_PROVIDER_HOST_HINTS = (
    "openai.com",
    "chatgpt.com",
    "x.ai",
    "anthropic.com",
    "moonshot.ai",
    "moonshot.cn",
    "cursor.sh",
    "cursor.com",
    "googleapis.com",
)


@dataclass(frozen=True)
class TailStat:
    mtime: float
    size: int


@dataclass(frozen=True)
class TreeWriteSample:
    """Count of files under cwd with mtime newer than the previous observation.

    ``available`` is True only when the walk finished. ``count`` is 0 for an
    empty/quiet tree that we *looked at*; callers must not treat
    ``available is False`` as zero writes.
    """

    count: int | None
    available: bool


@dataclass(frozen=True)
class WedgeObservation:
    verdict: str
    dispatch_id: str
    quiet_s: float | None
    cpu_s: float | None
    tree_writes: int | None
    tail_delta_bytes: int
    watchlisted: bool
    watchlisted_s: float | None
    reason: str | None
    socket_state: str | None
    probation_s: float


def stat_tail(path: Path | None) -> TailStat | None:
    """One ``stat`` of the tail. The cheap-path primitive."""
    if path is None:
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    return TailStat(mtime=float(st.st_mtime), size=int(st.st_size))


def tail_idle_s(stat: TailStat | None, *, now: float) -> float | None:
    if stat is None:
        return None
    return max(0.0, now - stat.mtime)


def cheap_watchlist_join(
    quiet_s: float | None,
    *,
    probation_s: float = DEFAULT_PROBATION_S,
) -> bool:
    """True when the cheap tail-stat says this worker pays for escalation."""
    if probation_s <= 0:
        return False
    if quiet_s is None:
        return False
    return quiet_s >= probation_s


def count_tree_writes_since(
    root: Path | None,
    *,
    since_mtime: float,
    skip_names: frozenset[str] = _TREE_SKIP_DIR_NAMES,
) -> TreeWriteSample:
    """Count files under ``root`` modified after ``since_mtime``.

    Does not follow symlinks. A walk or ``stat`` failure is unavailable
    unless at least one newer file was already found — that positive
    observation is enough to prove the tree is alive.
    """
    if root is None:
        return TreeWriteSample(count=None, available=False)
    try:
        if not root.is_dir():
            return TreeWriteSample(count=None, available=False)
    except OSError:
        return TreeWriteSample(count=None, available=False)

    count = 0
    stat_failed = False

    def _raise_walk_error(err: OSError) -> None:
        raise err

    try:
        for dirpath, dirnames, filenames in os.walk(
            root, followlinks=False, onerror=_raise_walk_error
        ):
            dirnames[:] = [name for name in dirnames if name not in skip_names]
            for name in filenames:
                path = Path(dirpath) / name
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    stat_failed = True
                    continue
                if mtime > since_mtime:
                    count += 1
    except OSError:
        if count > 0:
            return TreeWriteSample(count=count, available=True)
        return TreeWriteSample(count=None, available=False)
    if stat_failed and count == 0:
        return TreeWriteSample(count=None, available=False)
    return TreeWriteSample(count=count, available=True)


def _remote_host(name_field: str) -> str | None:
    text = name_field.strip()
    if not text:
        return None
    if "->" in text:
        remote = text.split("->", 1)[1]
    else:
        remote = text
    remote = remote.split()[0] if remote.split() else remote
    if remote.startswith("[") and "]:" in remote:
        host = remote[1:].split("]", 1)[0]
    elif remote.count(":") > 1 and not remote.startswith("["):
        # IPv6 without brackets: take everything before the last colon (port).
        host = remote.rsplit(":", 1)[0]
    else:
        host = remote.rsplit(":", 1)[0]
    return host.strip("[]") or None


def _is_loopback_host(host: str) -> bool:
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def _is_provider_host(host: str) -> bool:
    lowered = host.lower()
    return any(hint in lowered for hint in _PROVIDER_HOST_HINTS)


def parse_lsof_established_names(output: str) -> list[str]:
    names: list[str] = []
    for raw in output.splitlines():
        if raw.startswith("n"):
            names.append(raw[1:])
    return names


def classify_socket_names(names: list[str]) -> str:
    """Map ESTABLISHED name fields to provider / none / unknown.

    A remote ESTABLISHED whose host we cannot name as the provider is
    unknown, not ``none``: CDN IPs are exactly the waiting-on-provider
    case and calling them wedged destroys live work.
    """
    saw_remote = False
    saw_provider = False
    for name in names:
        host = _remote_host(name)
        if host is None:
            continue
        if _is_loopback_host(host):
            continue
        saw_remote = True
        if _is_provider_host(host):
            saw_provider = True
    if saw_provider:
        return SOCKET_PROVIDER
    if saw_remote:
        return SOCKET_UNKNOWN
    return SOCKET_NONE


def provider_socket_state(
    pids: list[int] | tuple[int, ...] | int | None,
    *,
    lsof_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    timeout_s: float = SOCKET_LSOF_TIMEOUT_S,
) -> str:
    """ESTABLISHED TCP sockets for ``pids``.

    Returns ``provider``, ``none``, or ``unknown``. A failed/missing ``lsof``
    is unknown — absence of evidence is not ``none``.
    """
    if pids is None:
        return SOCKET_UNKNOWN
    if isinstance(pids, int):
        pid_list = [pids]
    else:
        pid_list = [int(p) for p in pids if p is not None]
    if not pid_list:
        return SOCKET_UNKNOWN
    runner = lsof_runner or subprocess.run
    try:
        proc = runner(
            [
                "lsof",
                "-nP",
                "-a",
                "-p",
                ",".join(str(p) for p in pid_list),
                "-iTCP",
                "-sTCP:ESTABLISHED",
                "-Fn",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, TypeError, ValueError):
        return SOCKET_UNKNOWN
    # lsof returns 1 when there are no matching sockets; that is a look,
    # not a failure. Other non-zero (command error) is unknown.
    rc = getattr(proc, "returncode", 1)
    if rc not in (0, 1):
        return SOCKET_UNKNOWN
    return classify_socket_names(parse_lsof_established_names(proc.stdout or ""))


def cpu_seconds_delta(
    before: dict[int, float] | None,
    after: dict[int, float] | None,
) -> float | None:
    if before is None or after is None:
        return None
    return cputime_delta_seconds(before, after)


def snapshot_cpu_seconds(pgid_or_pid: int | str | None) -> dict[int, float] | None:
    return pgroup_cputime_snapshot(pgid_or_pid)


def classify_wedge_watch(
    *,
    worker_alive: bool,
    quiet_s: float | None,
    probation_s: float = DEFAULT_PROBATION_S,
    tail_delta_bytes: int = 0,
    tree_writes: int | None = None,
    tree_available: bool = False,
    cpu_s: float | None = None,
    sample_interval_s: float | None = None,
    socket_state: str = SOCKET_UNKNOWN,
    cpu_epsilon_s: float = CPU_EPSILON_S,
    watchlisted_s: float | None = None,
) -> tuple[str, str | None]:
    """Return ``(verdict, reason)``. ``reason`` explains UNKNOWN (or None).

    Cheap-path ``live`` does not consult CPU, tree, or sockets. Escalation
    probes are required only after probation. A missing probe is UNKNOWN,
    never a silent default to wedged.
    """
    if not worker_alive:
        return VERDICT_LIVE, None
    if probation_s <= 0:
        return VERDICT_LIVE, None
    if quiet_s is None:
        return VERDICT_UNKNOWN, "tail mtime unreadable"
    if quiet_s < probation_s:
        return VERDICT_LIVE, None
    if tail_delta_bytes > 0:
        return VERDICT_LIVE, None
    if tree_available and tree_writes is not None and tree_writes > 0:
        return VERDICT_LIVE, None
    cpu_moving = (
        cpu_s is not None
        and sample_interval_s is not None
        and sample_interval_s > 0
        and cpu_s > cpu_epsilon_s
    )
    if cpu_moving:
        return VERDICT_LIVE, None

    missing: list[str] = []
    if not tree_available or tree_writes is None:
        missing.append("tree")
    if cpu_s is None or sample_interval_s is None or sample_interval_s <= 0:
        missing.append("cpu_s")
    if missing:
        return VERDICT_UNKNOWN, "missing " + "/".join(missing)

    if socket_state == SOCKET_PROVIDER:
        return VERDICT_UNKNOWN, "waiting on provider"
    if socket_state != SOCKET_NONE:
        return VERDICT_UNKNOWN, "socket state unknown"
    return VERDICT_WEDGED, None


def format_duration_s(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        mins = seconds / 60.0
        if abs(mins - round(mins)) < 0.05:
            return f"{int(round(mins))}m"
        if mins >= 10:
            return f"{mins:.0f}m"
        return f"{mins:.1f}m"
    hours = seconds / 3600.0
    if abs(hours - round(hours)) < 0.05:
        return f"{int(round(hours))}h"
    return f"{hours:.1f}h"


def format_status_line(
    observation: WedgeObservation,
) -> str:
    """Terse enough for a controller status line.

    Example::

        b318-capture  UNKNOWN  quiet=11m  cpu_s=0.0  tree_writes=0  tail=+0B  (watchlisted 6m)  waiting on provider
    """
    cpu = (
        "cpu_s=?"
        if observation.cpu_s is None
        else f"cpu_s={observation.cpu_s:.1f}"
    )
    tree = (
        "tree_writes=?"
        if observation.tree_writes is None
        else f"tree_writes={observation.tree_writes}"
    )
    tail_delta = observation.tail_delta_bytes
    tail = f"tail={tail_delta:+d}B"
    parts = [
        observation.dispatch_id,
        observation.verdict,
        f"quiet={format_duration_s(observation.quiet_s)}",
        cpu,
        tree,
        tail,
    ]
    line = "  ".join(parts)
    if observation.watchlisted and observation.watchlisted_s is not None:
        line += f"  (watchlisted {format_duration_s(observation.watchlisted_s)})"
    if observation.verdict == VERDICT_UNKNOWN and observation.reason:
        line += f"  {observation.reason}"
    return line


def observe_wedge(
    *,
    dispatch_id: str,
    worker_alive: bool,
    quiet_s: float | None,
    tail_delta_bytes: int,
    probation_s: float = DEFAULT_PROBATION_S,
    tree_writes: int | None = None,
    tree_available: bool = False,
    cpu_s: float | None = None,
    sample_interval_s: float | None = None,
    socket_state: str = SOCKET_UNKNOWN,
    watchlisted_s: float | None = None,
    cpu_epsilon_s: float = CPU_EPSILON_S,
) -> WedgeObservation:
    watchlisted = cheap_watchlist_join(quiet_s, probation_s=probation_s) and worker_alive
    if not watchlisted:
        # Cheap path: do not report expensive probes we did not take.
        verdict, reason = classify_wedge_watch(
            worker_alive=worker_alive,
            quiet_s=quiet_s,
            probation_s=probation_s,
            tail_delta_bytes=tail_delta_bytes,
        )
        return WedgeObservation(
            verdict=verdict,
            dispatch_id=dispatch_id,
            quiet_s=quiet_s,
            cpu_s=None,
            tree_writes=None,
            tail_delta_bytes=tail_delta_bytes,
            watchlisted=False,
            watchlisted_s=None,
            reason=reason,
            socket_state=None,
            probation_s=probation_s,
        )
    verdict, reason = classify_wedge_watch(
        worker_alive=worker_alive,
        quiet_s=quiet_s,
        probation_s=probation_s,
        tail_delta_bytes=tail_delta_bytes,
        tree_writes=tree_writes,
        tree_available=tree_available,
        cpu_s=cpu_s,
        sample_interval_s=sample_interval_s,
        socket_state=socket_state,
        cpu_epsilon_s=cpu_epsilon_s,
        watchlisted_s=watchlisted_s,
    )
    return WedgeObservation(
        verdict=verdict,
        dispatch_id=dispatch_id,
        quiet_s=quiet_s,
        cpu_s=cpu_s,
        tree_writes=tree_writes if tree_available else None,
        tail_delta_bytes=tail_delta_bytes,
        watchlisted=True,
        watchlisted_s=watchlisted_s,
        reason=reason,
        socket_state=socket_state,
        probation_s=probation_s,
    )
