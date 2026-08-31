"""Shared liveness + status-IO helpers for goal-flight workers.

Used by the ACP runner (``goalflight_acp_run.py``) and the log watcher
(``goalflight_watch.py``). Keeps the liveness classification, process-group
CPU sampling, the atomic status writer, and the idle-path CPU grace in ONE
place so the runner and watcher can't drift apart.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Awaitable, Callable
import uuid

import goalflight_compat
import goalflight_output_redact

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
# Give-up bound when every applicable liveness probe remains unknown. Unknown
# never counts as death (the b-238 class: a failed `ps` on a busy box looks
# like "no children"). Waiting forever stalls finalization. 7200s is 2 hours:
# a 55-minute working worker (b-238) survives with ~65 minutes of margin,
# and the bound matches the dispatch lease TTL cap. Known-idle still dies at
# idle_timeout; positive activity remains live, while cannot-tell probes keep
# the worker until this outer event-silence bound and then start bounded cleanup.
# Capacity remains held unless that cleanup proves the full worker group is dead.
INDETERMINATE_LIVENESS_FLOOR_S = 7200.0
LIVENESS_INDETERMINATE_STATE = "liveness_indeterminate"
TREE_PROBE_SKIPPED = "skipped"
TREE_PROBE_MEASURED = "measured"
TREE_PROBE_UNAVAILABLE = "unavailable"
_SYSTEM_STARVED_CACHE: tuple[float, bool] | None = None
_STATUS_EPOCH_SCHEMAS = {"goalflight.acp-run.v1", "goalflight.status.v1"}
_STATUS_EPOCH_CACHE: dict[str, str] = {}
_TEST_ACTIVE_TIME_SCALE: tuple[float, float] | None = None
_TEST_ACTIVE_TIME_SCALE_CHECKED = False


def active_monotonic() -> float:
    """Monotonic seconds that do NOT advance while the system is asleep.

    macOS CLOCK_UPTIME_RAW excludes sleep; Linux CLOCK_MONOTONIC excludes suspend.
    """
    now: float | None = None
    for name in ("CLOCK_UPTIME_RAW", "CLOCK_MONOTONIC"):
        clk = getattr(time, name, None)
        if clk is not None:
            try:
                now = time.clock_gettime(clk)
                break
            except OSError:
                pass
    if now is None:
        now = time.monotonic()

    # Hermetic acceptance tests can compress an incident-scale awake-time gap
    # without sleeping for an hour. The gate is test-mode-only and resolved
    # once, so production clocks and hot-loop logging remain untouched.
    global _TEST_ACTIVE_TIME_SCALE, _TEST_ACTIVE_TIME_SCALE_CHECKED
    if not _TEST_ACTIVE_TIME_SCALE_CHECKED:
        raw_scale = goalflight_compat.allowed_env_override(
            "GOALFLIGHT_TEST_ACTIVE_TIME_SCALE",
            "",
            test_mode=True,
        )
        if raw_scale is not None:
            try:
                scale = float(raw_scale)
            except ValueError:
                scale = 0.0
            if scale > 0:
                _TEST_ACTIVE_TIME_SCALE = (now, scale)
        _TEST_ACTIVE_TIME_SCALE_CHECKED = True
    if _TEST_ACTIVE_TIME_SCALE is not None:
        origin, scale = _TEST_ACTIVE_TIME_SCALE
        return origin + ((now - origin) * scale)
    return now


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
) -> bool | None:
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
    # pmset ran but did not report the key. That is not proof the machine is
    # in high-power mode; treat as unknown so the watcher grants the same
    # bounded starvation grace as a failed read.
    return None


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


def _linux_powersave_governor(sys_root: Path = Path("/sys")) -> bool | None:
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
            return None
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
    sys_root: Path = Path("/sys"),
    proc_stat: Path = Path("/proc/stat"),
) -> bool | None:
    platform = platform_name or sys.platform
    if platform == "darwin":
        low_power = _darwin_low_power_mode_enabled(check_output)
        if low_power is None:
            return None
        idle_pct = _darwin_idle_pct(check_output) if low_power else None
    elif platform.startswith("linux"):
        low_power = _linux_powersave_governor(sys_root)
        if low_power is None:
            return None
        idle_pct = (
            _linux_idle_pct(sleep=sleep, proc_stat=proc_stat)
            if low_power
            else None
        )
    else:
        return False
    if not low_power:
        return False
    if idle_pct is None:
        return None
    return idle_pct < SYSTEM_STARVED_IDLE_PCT


def system_starved(
    *,
    now: Callable[[], float] = time.monotonic,
    force_refresh: bool = False,
) -> bool | None:
    """Low-power + low-idle verdict; None means the probe could not run.

    Unknown is deliberately not cached as healthy. The watcher grants the same
    bounded grace as starvation for that decision, then retries on its next
    poll instead of suppressing the safeguard for the cache TTL.
    """
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
        return None
    if starved is None:
        return None
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


# ── process-group CPU sampling ───────────────────────────────────────────────
#
# Why this measures a DELTA of cumulative cpu-time instead of reading ps's
# ``%cpu`` column, which is what it used to do:
#
#   BSD/Darwin ``%cpu`` is a DECAYING AVERAGE over roughly the last minute, not
#   the rate right now. Measured on this fleet (1155 processes, two ps sweeps
#   4s apart, differencing cumulative cpu-time to get ground truth):
#
#       ps 100.7%  vs true 101.7%   steady load          -> agrees
#       ps  95.4%  vs true  25.0%   just stopped burning -> ps lags HIGH
#       ps   0.7%  vs true   9.2%   short bursts         -> ps reads LOW
#
#   So it is wrong in BOTH directions by up to ~4x precisely at transitions --
#   which is exactly when a watcher is deciding "is this worker still working,
#   or is it wedged". A worker that finishes thinking and starts a test run
#   keeps reading hot for tens of seconds.
#
# The measurement:
#       pct = (cpu_seconds(t1) - cpu_seconds(t0)) / (t1 - t0) * 100
#   Units: cpu-seconds / wall-seconds is dimensionless; x100 gives percent of
#   ONE core, so a group saturating two cores reads 200% -- the same convention
#   ``%cpu`` used, so callers and status consumers need no change.
#
# Resolution: ps reports cpu-time in centiseconds, so the smallest non-zero
#   delta is 0.01 cpu-s and the quantum over a window W is (1/W) percent --
#   ~1.7% at the 0.6s cold-start window, ~0.2% at a 5s warm window. This does
#   NOT weaken the idle test (``--cpu-epsilon`` defaults to 0.1): a group
#   burning no CPU accrues no ticks at all, so its delta is exactly 0.0 for any
#   W. The quantum only blurs small-but-nonzero rates, and it blurs them upward
#   -- toward "busy" -- which is the safe direction for a liveness check.
#
# Sanity check: one process spinning flat out for the whole window burns W
#   cpu-seconds, so W/W*100 = 100%. Two such processes give 200%. A group that
#   burns nothing gives 0%. All three match what the callers already expect.

# Two different bounds, deliberately not one constant.
#
# WARM_MIN is the shortest cached gap worth differencing. It must sit BELOW the
# shortest interval any caller polls at, or the warm path never triggers: with a
# single 0.6 constant, `cpu_liveness_keep_waiting`'s 0.5s resample fell under
# the threshold on every attempt, so each one re-entered the cold path, slept
# another 0.6s, and threw away the continuous series the cache exists to build.
# 0.2s is below every current caller's cadence.
#
# COLD_WINDOW is how long to sleep when there is no usable cached sample and a
# rate must be produced from a standing start. Longer than WARM_MIN because a
# deliberate pause should buy real resolution: at centisecond ps granularity the
# quantum is (1/W) percent, so 0.6s gives ~1.7%.
#
# MAX_AGE bounds the other end: a cached sample older than this would average
# over so long a window that it reintroduces the very lag this replaced.
_CPU_SAMPLE_WARM_MIN_S = 0.2
_CPU_SAMPLE_COLD_WINDOW_S = 0.6
_CPU_SAMPLE_MAX_AGE_S = 60.0

# pgid -> (monotonic timestamp, {pid: cumulative cpu-seconds})
_cpu_samples: dict[int, tuple[float, dict[int, float]]] = {}


def parse_ps_cputime(field: str) -> float:
    """Parse a ps TIME field -- ``[[DD-]HH:]MM:SS[.ss]`` -- into seconds."""
    text = field.strip()
    if not text:
        raise ValueError("empty cpu-time field")
    days = 0.0
    if "-" in text:
        day_part, _, text = text.partition("-")
        days = float(day_part)
    # Right-to-left, each colon-separated group is the next power of 60:
    # seconds, minutes, hours. ps omits leading groups that are zero.
    seconds = 0.0
    for power, value in enumerate(reversed([float(p) for p in text.split(":")])):
        seconds += value * (60.0**power)
    return days * 86400.0 + seconds


def parse_ps_pgroup_cputime(ps_output: str, target_pgid: int | str) -> dict[int, float]:
    """Map pid -> cumulative cpu-seconds for one process group.

    Input is ``ps -A -o pgid=,pid=,time=`` output.
    """
    try:
        target = int(str(target_pgid).strip())
    except (TypeError, ValueError):
        return {}

    sample: dict[int, float] = {}
    for raw_line in ps_output.splitlines():
        parts = raw_line.split()
        if len(parts) < 3:
            continue
        try:
            if int(parts[0]) != target:
                continue
            sample[int(parts[1])] = parse_ps_cputime(parts[2])
        except ValueError:
            continue
    return sample


def cputime_delta_seconds(
    before: dict[int, float],
    after: dict[int, float],
) -> float:
    """CPU-seconds burned by a process group between two snapshots.

    Paired PER PID rather than summing the group, because a group sum is wrong
    in two ways that happen on every tick of a real worker:

    - a child that EXITS during the window drops out of ``after``, so a naive
      ``sum(after) - sum(before)`` goes NEGATIVE -- every finished bash tool
      call does this;
    - a child BORN during the window carries its whole lifetime cpu-time into
      ``after`` with nothing to subtract, inflating the delta.

    Pairing by pid fixes both: matched pids contribute their own delta, pids
    only in ``after`` contribute their full cpu-time (they were born inside the
    window, so all of it IS in-window), and pids that vanished contribute 0.
    That last case undercounts, bounded by one poll interval of one child, and
    biased toward "idle" -- which callers already cross-check against output
    and marker activity before declaring a worker dead.
    """
    busy = 0.0
    for pid, after_s in after.items():
        before_s = before.get(pid)
        if before_s is None:
            busy += after_s
        elif after_s > before_s:
            busy += after_s - before_s
    return max(0.0, busy)


def cpu_pct_from_cputime_delta(
    before: dict[int, float],
    after: dict[int, float],
    window_s: float,
) -> float:
    """Percent of one core burned by a process group over ``window_s``."""
    if window_s <= 0:
        return 0.0
    return cputime_delta_seconds(before, after) / window_s * 100.0


def _pgroup_cputime_snapshot(pgid: int) -> dict[int, float] | None:
    """One ps sweep, or None when the sample itself is unavailable."""
    try:
        output = subprocess.check_output(
            ["ps", "-A", "-o", "pgid=,pid=,time="],
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_ps_pgroup_cputime(output, pgid)


def pgroup_cputime_snapshot(pgid_or_pid: int | str | None) -> dict[int, float] | None:
    """Map pid -> cumulative cpu-seconds for a process group.

    Accepts a pgid or a live pid (resolved to its group). Returns None only
    when the sample itself is unavailable. This is the raw counter the wedge
    detector diffs across two polls; it is not an instantaneous ``%cpu``.
    """
    try:
        target = int(pgid_or_pid) if pgid_or_pid is not None else None
    except (TypeError, ValueError):
        return None
    if target is None:
        return None
    pgid = process_group_id(target) or target
    return _pgroup_cputime_snapshot(pgid)


def pgroup_cpu_pct(pgid_or_pid: int | str | None) -> float | None:
    """Return summed %CPU for a process group.

    Accepts either a process-group id or a worker pid. If given a live pid, the
    pid is resolved to its current pgid first. Returns None only when the CPU
    sample itself is unavailable; a live-but-idle group returns 0.0.
    """
    test_override = goalflight_compat.allowed_env_override(
        "GOALFLIGHT_TEST_PGROUP_CPU_PCT",
        "",
        test_mode=True,
    )
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

    now = time.monotonic()
    sample = _pgroup_cputime_snapshot(pgid)
    if sample is None:
        return None

    # Warm path: difference against the caller's previous call, so a polling
    # watcher pays one ps sweep per tick and the window is its own interval.
    cached = _cpu_samples.get(pgid)
    if cached is not None:
        prev_at, prev_sample = cached
        window = now - prev_at
        if _CPU_SAMPLE_WARM_MIN_S <= window <= _CPU_SAMPLE_MAX_AGE_S:
            _cpu_samples[pgid] = (now, sample)
            return cpu_pct_from_cputime_delta(prev_sample, sample, window)

    # Cold path (first call for this group, or the cached sample aged out):
    # take the second half of the pair now. One deliberate short sleep, not a
    # guess -- a rate cannot be read from a single cumulative counter.
    time.sleep(_CPU_SAMPLE_COLD_WINDOW_S)
    later = time.monotonic()
    later_sample = _pgroup_cputime_snapshot(pgid)
    if later_sample is None:
        return None
    _cpu_samples[pgid] = (later, later_sample)
    return cpu_pct_from_cputime_delta(sample, later_sample, later - now)


def cpu_confirmed_idle(cpu_pct: float | None, epsilon_pct: float) -> bool:
    """Return true only when CPU was measured and is at/below the idle epsilon."""
    return cpu_pct is not None and cpu_pct <= epsilon_pct


def resolve_indeterminate_timeout_s(
    idle_timeout_s: float | None,
    override: float | None = None,
) -> float:
    """Seconds of silence before non-idle liveness starts bounded cleanup.

    Override, when positive, is the bound. Otherwise the bound is
    ``max(idle_timeout, INDETERMINATE_LIVENESS_FLOOR_S)`` so a short one-shot
    idle still waits two hours when probes are positive or fail, and a long
    goal-mode idle does not end earlier just because probes were inconclusive.
    """
    if override is not None and override > 0:
        return float(override)
    idle = float(idle_timeout_s) if idle_timeout_s is not None and idle_timeout_s > 0 else 0.0
    return max(idle, INDETERMINATE_LIVENESS_FLOOR_S)


def classify_liveness(
    pid_alive: bool,
    pgroup_cpu: float | None,
    seconds_since_event: float | None,
    thresholds: LivenessThresholds,
    *,
    low_power_relax: bool = False,
    low_power_relax_factor: float = LOW_POWER_RELAX_FACTOR,
    live_descendants: int | None = None,
    tree_age_s: float | None = None,
    tree_probe: str = TREE_PROBE_SKIPPED,
    indeterminate_timeout_s: float | None = None,
) -> LivenessState:
    """Classify worker liveness from identity, activity, and progress silence.

    Tail/event silence is a proxy for "not working". After the idle window it
    is not proof: a worker can be grinding a test suite whose stdout is
    buffered, or writing the worktree without narrating. Extra signals, when
    measured, veto a wedge:

    - ``live_descendants > 0``: a live (non-zombie) child still exists
      (pytest, a compiler, a tool that sleeps without printing). Zombie
      rows are filtered by the sampler; this count is already live work.
    - ``tree_age_s < idle_timeout``: the worker's own tree was written inside
      the idle window.
    - process-group CPU above epsilon: already-busy work, even with no children
      in the sample.

    The three probes are symmetric. A probe that cannot determine its answer
    is unknown, and unknown is never evidence of death. ``idle_timeout`` /
    ``wedged`` requires every *applicable* probe to have looked and found
    nothing. If the watcher later gives up because it still cannot tell,
    the state is ``liveness_indeterminate``, not ``wedged``: that reason
    names the gap instead of asserting the worker was idle.

    ``tree_probe`` is ``skipped`` when there is no distinct worker cwd
    (canonical-root writes cannot be attributed), ``measured`` when the walk
    finished, and ``unavailable`` when the walk failed. Skipped and unavailable
    are both unknown, never negative evidence. A numeric ``tree_age_s`` without
    an explicit probe is treated as ``measured`` so existing callers stay valid.
    """
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

    if tree_probe == TREE_PROBE_SKIPPED and tree_age_s is not None:
        tree_probe = TREE_PROBE_MEASURED

    descendants_alive = live_descendants is not None and live_descendants > 0
    tree_alive = (
        tree_probe == TREE_PROBE_MEASURED
        and tree_age_s is not None
        and idle_timeout is not None
        and idle_timeout > 0
        and tree_age_s < idle_timeout
    )
    cpu_alive = pgroup_cpu is not None and pgroup_cpu > thresholds.cpu_epsilon_pct
    give_up_s = resolve_indeterminate_timeout_s(
        idle_timeout, indeterminate_timeout_s
    )
    give_up_expired = (
        seconds_since_event is not None and seconds_since_event >= give_up_s
    )
    if descendants_alive or tree_alive or cpu_alive:
        # Positive activity is a liveness verdict, not an unresolved probe.
        # The outer bound exists only to bound "could not tell"; killing here
        # would turn affirmative CPU/child/tree evidence into worker death.
        return "running_quiet"

    unknown = (
        live_descendants is None
        or tree_probe in {TREE_PROBE_SKIPPED, TREE_PROBE_UNAVAILABLE}
        or pgroup_cpu is None
    )
    if unknown:
        if give_up_expired:
            return LIVENESS_INDETERMINATE_STATE
        return "running"

    if low_power_relax and cpu_confirmed_idle(pgroup_cpu, thresholds.cpu_epsilon_pct):
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
        and cpu_confirmed_idle(pgroup_cpu, cpu_epsilon_pct)
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


def _status_epoch_key(path: Path) -> str:
    return str(path.expanduser().resolve(strict=False))


def _payload_epoch(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not value.is_integer():
            return None
        return str(int(value))
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _read_existing_status_epoch(path: Path) -> str | None:
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(existing, dict):
        return None
    return _payload_epoch(existing.get("epoch"))


def _status_epoch_for(path: Path) -> str:
    key = _status_epoch_key(path)
    existing = _read_existing_status_epoch(path)
    if existing is not None:
        _STATUS_EPOCH_CACHE[key] = existing
        return existing
    cached = _STATUS_EPOCH_CACHE.get(key)
    if cached is not None and path.exists():
        return cached
    epoch = f"status-{uuid.uuid4().hex}"
    _STATUS_EPOCH_CACHE[key] = epoch
    return epoch


def reset_status_lineage(path: Path) -> bool:
    """Remove persisted status so the next ``write_status`` mints a new epoch.

    Clears the in-process epoch cache for ``path`` and deletes the status file
    when present. Returns True when a file was removed."""
    resolved = path.expanduser()
    _STATUS_EPOCH_CACHE.pop(_status_epoch_key(resolved), None)
    try:
        resolved.unlink()
        return True
    except FileNotFoundError:
        return False


def _ensure_status_epoch(path: Path, payload: dict) -> None:
    if payload.get("schema") not in _STATUS_EPOCH_SCHEMAS:
        return
    explicit_epoch = _payload_epoch(payload.get("epoch"))
    if explicit_epoch is not None:
        _STATUS_EPOCH_CACHE[_status_epoch_key(path)] = explicit_epoch
        return
    payload["epoch"] = _status_epoch_for(path)


def status_tmp_path(path: Path) -> Path:
    suffix = path.suffix or ".json"
    return path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}{suffix}.tmp")


def write_status(path: Path, payload: dict) -> None:
    """Atomically write status JSON (write temp sibling, then os.replace).

    Same-directory tmp + replace is atomic on POSIX, so a concurrent reader
    never sees a half-written file. Shared by goalflight_acp_run.py (runner
    heartbeat) and goalflight_watch.py (log watcher) so the two writers stay
    byte-identical (grok 2026-05-20 DRY note).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_status_epoch(path, payload)
    redacted = goalflight_output_redact.redact_data(payload)
    if isinstance(redacted, dict):
        if redacted is not payload:
            payload.clear()
            payload.update(redacted)
    else:
        # Fail-closed: never persist the unscrubbed payload.
        payload.clear()
        payload["state"] = "blocked"
        payload["reason"] = "status_redact_failed"
    tmp = status_tmp_path(path)
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        tmp.replace(path)
    finally:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()


async def cpu_liveness_keep_waiting(
    sampler: Callable[[], Awaitable[float | None]],
    cpu_epsilon_pct: float,
    *,
    attempts: int = 3,
    resample_s: float = 0.5,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> tuple[bool | None, float | None]:
    """Decide whether a silent worker is running_quiet (keep waiting) vs wedged.

    Called when an ACP worker has emitted no events for a full idle window.
    Samples process-group CPU up to ``attempts`` times: the first sample above
    ``cpu_epsilon_pct`` returns ``(True, cpu)`` — the worker is alive and busy,
    so the caller keeps waiting (the Phase-1 false-positive killer).

    CPU-sample-failure grace (codex 2026-05-20 P2): ``sampler`` returns None on
    a transient ``ps`` failure. One failed sample must NOT be read as "0 CPU =
    wedged" — that would reintroduce the false positive. Re-sampling rides out a
    transient blip. A numeric sample at/below epsilon proves idle and returns
    ``(False, cpu)`` after the resample window. If every sample is ``None``, the
    helper returns ``(None, None)``: unavailable CPU is not an idle verdict.

    This is the runner-side transient-failure grace. The watchers
    (``goalflight_watch.py``, ``watch-dispatch-tail.sh``) mirror the same intent
    with a consecutive-sample streak (``WEDGE_CONFIRM_SAMPLES``) instead of an
    intra-decision re-sample — different mechanism, same goal; keep them aligned.

    ``sampler``/``sleep`` are injected so the policy is unit-testable without a
    real worker or real delays.
    """
    last_cpu: float | None = None
    last_measured_cpu: float | None = None
    for attempt in range(max(1, attempts)):
        last_cpu = await sampler()
        if last_cpu is not None:
            last_measured_cpu = last_cpu
            if last_cpu > cpu_epsilon_pct:
                return True, last_cpu
        if attempt < attempts - 1:
            await sleep(resample_s)
    if last_measured_cpu is None:
        return None, None
    return False, last_measured_cpu


class IdleLivenessGate:
    """Stateful liveness gate for the ACP runner's idle path.

    Wraps ``cpu_liveness_keep_waiting`` (the transient-ps-failure grace) with a
    *hard wall*. CPU activity and live/unknown descendants can veto an ordinary
    idle decision, but no probe can make event-silent liveness unfalsifiable.
    The gate caps total quiet time since the first idle check at ``hard_wall_s``;
    past it, ``keep_waiting`` returns False before any probe can extend the wait.
    A real ACP event calls ``note_event()`` and resets the wall.
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
        # Start at construction so a caller that delays its first idle probe
        # cannot silently add an idle-timeout window to the hard wall.
        self._quiet_since: float | None = self._now()
        self._hard_wall_expired = False

    @property
    def hard_wall_expired(self) -> bool:
        return self._hard_wall_expired

    def note_event(self) -> None:
        """Call when a real ACP event arrives — resets the event-silence wall."""
        self._quiet_since = self._now()
        self._hard_wall_expired = False

    async def keep_waiting(
        self, sampler: Callable[[], Awaitable[float | None]]
    ) -> tuple[bool, float | None]:
        """Return (keep_waiting, last_cpu), bounded by event silence."""
        t = self._now()
        if self._quiet_since is None:
            self._quiet_since = t
        elif t - self._quiet_since >= self.hard_wall_s:
            self._hard_wall_expired = True
            return False, None
        self._hard_wall_expired = False
        verdict, cpu = await cpu_liveness_keep_waiting(
            sampler, self.cpu_epsilon_pct
        )
        if verdict is None:
            # The ACP idle callback consumes a boolean. Its safe action for an
            # unavailable CPU verdict is to keep the worker until the separate,
            # bounded indeterminate wall can act; False would assert a wedge.
            return True, cpu
        return verdict, cpu
