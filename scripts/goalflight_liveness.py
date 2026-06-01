"""Shared liveness + status-IO helpers for goal-flight workers.

Used by the ACP runner (``goalflight_acp_run.py``) and the log watcher
(``goalflight_watch.py``). Keeps the liveness classification, process-group
CPU sampling, the atomic status writer, and the idle-path CPU grace in ONE
place so the runner and watcher can't drift apart.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Awaitable, Callable

import goalflight_compat

SYSTEM_STARVED_CACHE_TTL_S = 30.0
SYSTEM_STARVED_IDLE_PCT = 20.0
LOW_POWER_RELAX_FACTOR = 3.0
# Absolute ceiling on the EXTRA grace the low-power relax may add, in seconds.
# The relaxed timeout is min(idle_timeout * factor, idle_timeout + CAP): the
# factor helps the short-idle one-shot case (300s -> 900s), while the CAP keeps
# persistent starvation from scaling a long goal-mode idle (36000s) into a ~30h
# hang. So a starved worker waits at most idle_timeout + 10min before wedging,
# preserving the fail-fast / no-multi-hour-hang invariant regardless of config.
LOW_POWER_RELAX_CAP_S = 600.0
_SYSTEM_STARVED_CACHE: tuple[float, bool] | None = None


def active_monotonic() -> float:
    """Monotonic seconds that do NOT advance while the system is asleep.

    macOS CLOCK_UPTIME_RAW excludes sleep; Linux CLOCK_MONOTONIC excludes suspend.
    """
    for name in ("CLOCK_UPTIME_RAW", "CLOCK_MONOTONIC"):
        clk = getattr(time, name, None)
        if clk is not None:
            try:
                return time.clock_gettime(clk)
            except OSError:
                pass
    return time.monotonic()


def system_sleep_pause_s(
    *,
    prev_wall: float,
    prev_active: float,
    wall_now: float,
    active_now: float,
    heartbeat_interval_s: float,
) -> float:
    """Return detected sleep/suspend seconds large enough to skip this tick."""
    freeze_s = max(0.0, (wall_now - prev_wall) - (active_now - prev_active))
    return freeze_s if freeze_s > max(5.0, 2 * heartbeat_interval_s) else 0.0


def system_sleep_pause_note(freeze_s: float, total_paused_s: float) -> str:
    return f"paused {freeze_s:.0f}s (system sleep/suspend); total_paused {total_paused_s:.0f}s"


def _parse_last_idle_pct(output: str) -> float | None:
    idle_idx: int | None = None
    latest_idle: float | None = None
    for raw_line in output.splitlines():
        parts = raw_line.split()
        if not parts:
            continue
        lowered = [part.lower() for part in parts]
        if "id" in lowered:
            idle_idx = lowered.index("id")
            continue
        if idle_idx is not None and len(parts) > idle_idx:
            try:
                latest_idle = float(parts[idle_idx])
                continue
            except ValueError:
                pass
    if latest_idle is not None:
        return latest_idle
    for raw_line in reversed(output.splitlines()):
        parts = raw_line.split()
        if not parts:
            continue
        try:
            return float(parts[-1])
        except ValueError:
            continue
    return None


def _darwin_low_power_mode_enabled(
    check_output: Callable[..., str] = subprocess.check_output,
) -> bool:
    output = check_output(
        ["pmset", "-g"],
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=2.0,
    )
    for raw_line in output.splitlines():
        parts = raw_line.strip().lower().split()
        if len(parts) >= 2 and parts[0] == "lowpowermode":
            return parts[-1] == "1"
    return False


def _darwin_idle_pct(
    check_output: Callable[..., str] = subprocess.check_output,
) -> float | None:
    output = check_output(
        ["iostat", "-c", "2"],
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=4.0,
    )
    return _parse_last_idle_pct(output)


def _linux_powersave_governor(sys_root: Path = Path("/sys")) -> bool:
    governor_paths = list(
        (sys_root / "devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_governor")
    )
    if not governor_paths:
        return False
    governors: list[str] = []
    for governor_path in governor_paths:
        try:
            governors.append(governor_path.read_text(encoding="utf-8").strip().lower())
        except OSError:
            return False
    return bool(governors) and all(governor == "powersave" for governor in governors)


def _proc_stat_totals(proc_stat: Path = Path("/proc/stat")) -> tuple[int, int] | None:
    try:
        line = proc_stat.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return None
    parts = line.split()
    if not parts or parts[0] != "cpu":
        return None
    try:
        values = [int(value) for value in parts[1:]]
    except ValueError:
        return None
    if len(values) < 4:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return idle, sum(values)


def _linux_idle_pct(
    *,
    sleep: Callable[[float], None] = time.sleep,
    proc_stat: Path = Path("/proc/stat"),
) -> float | None:
    first = _proc_stat_totals(proc_stat)
    if first is None:
        return None
    sleep(0.1)
    second = _proc_stat_totals(proc_stat)
    if second is None:
        return None
    idle_delta = second[0] - first[0]
    total_delta = second[1] - first[1]
    if idle_delta < 0 or total_delta <= 0:
        return None
    return 100.0 * idle_delta / total_delta


def _system_starved_uncached(
    *,
    platform_name: str | None = None,
    check_output: Callable[..., str] = subprocess.check_output,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    platform = platform_name or sys.platform
    if platform == "darwin":
        low_power = _darwin_low_power_mode_enabled(check_output)
        idle_pct = _darwin_idle_pct(check_output) if low_power else None
    elif platform.startswith("linux"):
        low_power = _linux_powersave_governor()
        idle_pct = _linux_idle_pct(sleep=sleep) if low_power else None
    else:
        return False
    return bool(low_power and idle_pct is not None and idle_pct < SYSTEM_STARVED_IDLE_PCT)


def system_starved(
    *,
    now: Callable[[], float] = time.monotonic,
    force_refresh: bool = False,
) -> bool:
    """Best-effort low-power + low-idle detector. Any failure returns False."""
    global _SYSTEM_STARVED_CACHE
    t = now()
    if (
        not force_refresh
        and _SYSTEM_STARVED_CACHE is not None
        and t - _SYSTEM_STARVED_CACHE[0] < SYSTEM_STARVED_CACHE_TTL_S
    ):
        return _SYSTEM_STARVED_CACHE[1]
    try:
        starved = _system_starved_uncached()
    except Exception:
        starved = False
    _SYSTEM_STARVED_CACHE = (t, starved)
    return starved


@dataclass(frozen=True)
class LivenessThresholds:
    idle_timeout_s: float | None
    cpu_epsilon_pct: float = 0.1


@dataclass(frozen=True)
class HeartbeatWedgeDecision:
    dead_sample: bool
    dead_samples: int
    wedged: bool


LivenessState = str


def process_group_id(pid: int | str | None) -> int | None:
    """Return a live process' process-group id, or None when unavailable."""
    try:
        pid_int = int(pid) if pid is not None else None
    except (TypeError, ValueError):
        return None
    if pid_int is None:
        return None
    if goalflight_compat.is_windows():
        # Native Windows has no POSIX process groups (and no ``os.getpgid``).
        # Dispatch is refused there, but stale-cleanup/status code can still
        # import this helper; return None so callers fall back to tracked-pid
        # handling instead of raising AttributeError.
        return None
    try:
        return os.getpgid(pid_int)
    except (ProcessLookupError, PermissionError, OSError):
        return None


def parse_ps_pgroup_cpu(ps_output: str, target_pgid: int | str) -> float:
    """Sum %CPU from `ps -A -o pgid=,%cpu=` output for one process group."""
    try:
        target = int(str(target_pgid).strip())
    except (TypeError, ValueError):
        return 0.0

    total = 0.0
    for raw_line in ps_output.splitlines():
        parts = raw_line.split()
        if len(parts) < 2:
            continue
        try:
            pgid = int(parts[0])
            cpu = float(parts[1])
        except ValueError:
            continue
        if pgid == target:
            total += cpu
    return total


def pgroup_cpu_pct(pgid_or_pid: int | str | None) -> float | None:
    """Return summed %CPU for a process group.

    Accepts either a process-group id or a worker pid. If given a live pid, the
    pid is resolved to its current pgid first. Returns None only when the CPU
    sample itself is unavailable; a live-but-idle group returns 0.0.
    """
    test_override = os.environ.get("GOALFLIGHT_TEST_PGROUP_CPU_PCT")
    if test_override is not None:
        try:
            return float(test_override)
        except ValueError:
            return None
    try:
        target = int(pgid_or_pid) if pgid_or_pid is not None else None
    except (TypeError, ValueError):
        return None
    if target is None:
        return None

    # Invariant: the bare-pid fallback (`or target`) is correct ONLY because
    # workers are spawned with start_new_session=True, which makes the direct
    # child the process-group leader (pgid == pid). If a future caller spawns a
    # worker that is NOT its own group leader, this would under-count CPU (it
    # would sum only that pid's group rather than the worker's actual group).
    pgid = process_group_id(target) or target
    try:
        output = subprocess.check_output(
            ["ps", "-A", "-o", "pgid=,%cpu="],
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_ps_pgroup_cpu(output, pgid)


def classify_liveness(
    pid_alive: bool,
    pgroup_cpu: float | None,
    seconds_since_event: float | None,
    thresholds: LivenessThresholds,
    *,
    low_power_relax: bool = False,
    low_power_relax_factor: float = LOW_POWER_RELAX_FACTOR,
) -> LivenessState:
    """Classify worker liveness from identity, CPU, and progress silence."""
    if not pid_alive:
        return "worker_dead"

    idle_timeout = thresholds.idle_timeout_s
    idle_expired = (
        idle_timeout is not None
        and idle_timeout > 0
        and seconds_since_event is not None
        and seconds_since_event >= idle_timeout
    )
    if not idle_expired:
        return "running"

    if pgroup_cpu is not None and pgroup_cpu > thresholds.cpu_epsilon_pct:
        return "running_quiet"
    if low_power_relax and pgroup_cpu is not None:
        # Absolute hard wall: the relax adds at most LOW_POWER_RELAX_CAP_S of
        # extra grace, never a multiple of a long idle_timeout. min() of the
        # factor form and the additive-cap form means short idles get the factor
        # benefit and long (goal-mode) idles are bounded by the cap -> a starved
        # worker still wedges within idle_timeout + cap, not idle_timeout * 3.
        relaxed_timeout = min(
            idle_timeout * max(1.0, low_power_relax_factor),
            idle_timeout + LOW_POWER_RELAX_CAP_S,
        )
        if seconds_since_event < relaxed_timeout:
            return "running"
    return "wedged"


def heartbeat_wedge_decision(
    *,
    pid_alive: bool,
    pgroup_cpu: float | None,
    wedge_progress_seen: int,
    previous_wedge_progress_seen: int,
    outstanding_count: int,
    cpu_epsilon_pct: float,
    previous_dead_samples: int,
    wedge_samples: int,
) -> HeartbeatWedgeDecision:
    """Update heartbeat dead-sample streak and wedge verdict.

    A dead sample is intentionally stricter than the idle-path classifier:
    unavailable CPU (None) is not treated as idle, because this loop repeats on
    a short cadence and should avoid false kills from a transient ps failure.
    """
    dead_sample = (
        pid_alive
        and pgroup_cpu is not None
        and pgroup_cpu <= cpu_epsilon_pct
        and wedge_progress_seen == previous_wedge_progress_seen
        and outstanding_count == 0
    )
    dead_samples = previous_dead_samples + 1 if dead_sample else 0
    return HeartbeatWedgeDecision(
        dead_sample=dead_sample,
        dead_samples=dead_samples,
        wedged=dead_samples >= max(1, wedge_samples) and wedge_progress_seen >= 1,
    )


def progress_stall_decision(
    *,
    pid_alive: bool,
    progress_quiet_s: float | None,
    progress_stall_s: float,
    outstanding_count: int = 0,
) -> bool:
    """Return true when no standard progress has arrived before the wall."""
    return (
        pid_alive
        and progress_stall_s > 0
        and progress_quiet_s is not None
        and progress_quiet_s >= progress_stall_s
        and outstanding_count == 0
    )


def write_status(path: Path, payload: dict) -> None:
    """Atomically write status JSON (write temp sibling, then os.replace).

    Same-directory tmp + replace is atomic on POSIX, so a concurrent reader
    never sees a half-written file. Shared by goalflight_acp_run.py (runner
    heartbeat) and goalflight_watch.py (log watcher) so the two writers stay
    byte-identical (grok 2026-05-20 DRY note).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


async def cpu_liveness_keep_waiting(
    sampler: Callable[[], Awaitable[float | None]],
    cpu_epsilon_pct: float,
    *,
    attempts: int = 3,
    resample_s: float = 0.5,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> tuple[bool, float | None]:
    """Decide whether a silent worker is running_quiet (keep waiting) vs wedged.

    Called when an ACP worker has emitted no events for a full idle window.
    Samples process-group CPU up to ``attempts`` times: the first sample above
    ``cpu_epsilon_pct`` returns ``(True, cpu)`` — the worker is alive and busy,
    so the caller keeps waiting (the Phase-1 false-positive killer).

    CPU-sample-failure grace (codex 2026-05-20 P2): ``sampler`` returns None on
    a transient ``ps`` failure. One failed sample must NOT be read as "0 CPU =
    wedged" — that would reintroduce the false positive. Re-sampling rides out a
    transient blip. Only when EVERY sample is None or at/below epsilon do we
    return ``(False, last)`` → wedged, so the caller cancels. (If ``ps`` is
    permanently unavailable on the platform, every sample is None and we
    correctly fall back to the pre-Phase-1 event-gap cancel.)

    This is the runner-side transient-failure grace. The watchers
    (``goalflight_watch.py``, ``watch-dispatch-tail.sh``) mirror the same intent
    with a consecutive-sample streak (``WEDGE_CONFIRM_SAMPLES``) instead of an
    intra-decision re-sample — different mechanism, same goal; keep them aligned.

    ``sampler``/``sleep`` are injected so the policy is unit-testable without a
    real worker or real delays.
    """
    last_cpu: float | None = None
    for attempt in range(max(1, attempts)):
        last_cpu = await sampler()
        if last_cpu is not None and last_cpu > cpu_epsilon_pct:
            return True, last_cpu
        if attempt < attempts - 1:
            await sleep(resample_s)
    return False, last_cpu


class IdleLivenessGate:
    """Stateful liveness gate for the ACP runner's idle path.

    Wraps ``cpu_liveness_keep_waiting`` (the transient-ps-failure grace) with a
    *hard wall*. A worker that stays CPU-busy but emits NO events resets the idle
    clock every window, so without a ceiling a pathological spinner would keep
    the runner alive forever (the one regression the bare CPU rule introduces
    versus the old idle-timeout). The gate caps cumulative running_quiet time
    *since the last real event* at ``hard_wall_s``; past it, ``keep_waiting``
    returns False so the runner cancels even though CPU > epsilon. A real ACP
    event calls ``note_event()`` and resets the wall — legitimate work that
    emits anything periodically is never capped. The full total-runtime / typed
    timeout-state taxonomy is Phase 2; this is the minimal no-hang backstop.
    """

    def __init__(
        self,
        cpu_epsilon_pct: float,
        hard_wall_s: float,
        *,
        now: Callable[[], float] = active_monotonic,
    ) -> None:
        self.cpu_epsilon_pct = cpu_epsilon_pct
        self.hard_wall_s = hard_wall_s
        self._now = now
        self._quiet_since: float | None = None

    def note_event(self) -> None:
        """Call when a real ACP event arrives — resets the running_quiet wall."""
        self._quiet_since = None

    async def keep_waiting(
        self, sampler: Callable[[], Awaitable[float | None]]
    ) -> tuple[bool, float | None]:
        """Return (keep_waiting, last_cpu). True only if the worker is CPU-busy
        AND has not been continuously event-silent past the hard wall."""
        keep, cpu = await cpu_liveness_keep_waiting(sampler, self.cpu_epsilon_pct)
        if not keep:
            self._quiet_since = None
            return False, cpu
        t = self._now()
        if self._quiet_since is None:
            self._quiet_since = t
        elif t - self._quiet_since > self.hard_wall_s:
            # Hard wall: CPU-busy but event-silent past the lease lifetime →
            # give up so a pathological spinner can't hang the runner forever.
            return False, cpu
        return True, cpu
