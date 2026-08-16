#!/usr/bin/env python3
"""Bounded, non-overlapping scheduled ticks for fleet-console data planes."""

from __future__ import annotations

import argparse
import errno
import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import goalflight_fleet_console as console
import goalflight_fleet_console_history as history
import goalflight_ledger


PLANES = ("attention", "fleet")
EXIT_OVERLAP = 75  # EX_TEMPFAIL: another tick owns this plane, so retry later.

# Live read-only deployed-wrapper samples (2026-08-16) measured attention at
# 1.12s and fleet at 1.84s / 76,473B. Integer-ceiling budgets are twice those
# measurements; both cadences exceed budget plus installer-documented reserve.
DEFAULT_BUDGET_S = {"attention": 3.0, "fleet": 4.0}


class PlaneLock:
    """A nonblocking advisory lock whose identity is scoped to one plane."""

    def __init__(self, lock_dir: Path, plane: str) -> None:
        if plane not in PLANES:
            raise ValueError(f"unknown plane: {plane}")
        self.path = lock_dir / f"fleet-console-{plane}.lock"
        self._handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                return False
            raise
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} plane={self.path.stem.rsplit('-', 1)[-1]}\n")
        handle.flush()
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "PlaneLock":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _default_output(plane: str) -> Path:
    return ROOT / "templates" / "fleet-console" / f"{plane}-data.js"


def _producer_command(
    plane: str,
    output: Path,
    *,
    producer_script: Path,
    python_executable: str,
    readers_dir: Path | None = None,
) -> list[str]:
    command = [
        python_executable,
        str(producer_script),
        plane,
        "--output",
        str(output),
    ]
    if plane == "fleet" and readers_dir is not None:
        command.extend(("--readers-dir", str(readers_dir)))
    return command


def _run_with_budget(
    command: list[str],
    *,
    deadline: float,
    popen_factory: Callable[..., subprocess.Popen[bytes]],
    monotonic: Callable[[], float],
) -> int:
    """Run and reap one sampler process group, stopping all descendants."""
    child = popen_factory(command, start_new_session=True)
    try:
        # Popen time counts too: deadline was set before spawning, so the wait
        # receives only the wall-clock budget that remains.
        return int(child.wait(timeout=max(0.0, deadline - monotonic())))
    except subprocess.TimeoutExpired:
        # goalflight_fleet_console can invoke usage readers of its own. Killing
        # only the direct Python process would let those descendants run past
        # the tick's wall-clock budget, so each sampler gets a fresh process
        # group and the timeout terminates the group before publishing DEGRADED.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()
        raise


def run_tick(
    plane: str,
    *,
    output: Path | None = None,
    lock_dir: Path | None = None,
    budget_s: float | None = None,
    producer_script: Path | None = None,
    readers_dir: Path | None = None,
    python_executable: str = sys.executable,
    popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """Run one scheduled plane tick under its lock and wall-clock budget."""
    if plane not in PLANES:
        raise ValueError(f"unknown plane: {plane}")
    resolved_output = (output or _default_output(plane)).expanduser().resolve()
    resolved_lock_dir = (lock_dir or (Path.home() / ".goal-flight" / "locks")).expanduser().resolve()
    resolved_budget = DEFAULT_BUDGET_S[plane] if budget_s is None else budget_s
    if resolved_budget <= 0:
        raise ValueError("budget must be greater than zero seconds")

    lock = PlaneLock(resolved_lock_dir, plane)
    if not lock.acquire():
        print(
            f"fleet-console {plane} tick SKIPPED: overlap lock held ({lock.path})",
            file=sys.stderr,
        )
        return EXIT_OVERLAP

    started_clock = monotonic()
    deadline = started_clock + resolved_budget
    started_at = console._utc_now()
    try:
        command = _producer_command(
            plane,
            resolved_output,
            producer_script=(producer_script or (SCRIPT_DIR / "goalflight_fleet_console.py")),
            python_executable=python_executable,
            readers_dir=readers_dir,
        )
        try:
            return_code = _run_with_budget(
                command,
                deadline=deadline,
                popen_factory=popen_factory,
                monotonic=monotonic,
            )
        except subprocess.TimeoutExpired:
            # subprocess.run has killed and reaped the sampler before raising.
            # Publish through the projection's existing DEGRADED payload and
            # exit contract; the renderer sees a producer error, never an old
            # sample that merely looks current.
            resolved_output.parent.mkdir(parents=True, exist_ok=True)
            payload = console.build_degraded_plane(
                plane,
                error="budget:TimeoutExpired",
                started_at=started_at,
            )
            console.publish_plane(resolved_output, payload, plane)
            elapsed = monotonic() - started_clock
            print(
                f"fleet-console {plane} budget exhausted after {elapsed:.3f}s "
                f"(limit {resolved_budget:g}s)",
                file=sys.stderr,
            )
            return console.sample_exit_code(payload, plane)
        # Catch-up is slow-plane work. Run it only after the fast file is
        # current and outside the short-poll child deadline; event hooks make
        # the hourly pass normally empty. Explicit producer_script injections
        # are unit-test/fake producers and deliberately skip machine state.
        if plane == "fleet" and producer_script is None and return_code == 0:
            # The slow sweep owns a separate nonblocking lock. Release the
            # cadence lock first so even an unusually large recovery pass can
            # never suppress the next fast-plane publication.
            lock.release()
            try:
                history.catch_up_if_due(
                    goalflight_ledger.read_records,
                    output_dir=resolved_output.parent,
                )
            except Exception as exc:
                print(f"fleet-console history catch-up warning: {type(exc).__name__}", file=sys.stderr)
        return return_code
    finally:
        lock.release()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one bounded, non-overlapping fleet-console producer tick"
    )
    parser.add_argument("plane", choices=PLANES)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--lock-dir", type=Path)
    parser.add_argument("--budget-s", type=float)
    parser.add_argument(
        "--readers-dir",
        type=Path,
        help="fleet usage-reader directory forwarded to the sampler",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run_tick(
            args.plane,
            output=args.output,
            lock_dir=args.lock_dir,
            budget_s=args.budget_s,
            readers_dir=args.readers_dir,
        )
    except (OSError, ValueError) as exc:
        print(f"fleet-console {args.plane} tick ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
