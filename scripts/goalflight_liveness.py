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
import time
from typing import Awaitable, Callable


@dataclass(frozen=True)
class LivenessThresholds:
    idle_timeout_s: float | None
    cpu_epsilon_pct: float = 0.1


LivenessState = str


def process_group_id(pid: int | str | None) -> int | None:
    """Return a live process' process-group id, or None when unavailable."""
    try:
        pid_int = int(pid) if pid is not None else None
    except (TypeError, ValueError):
        return None
    if pid_int is None:
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
    return "wedged"


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
        now: Callable[[], float] = time.monotonic,
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

