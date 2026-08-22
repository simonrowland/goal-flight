#!/usr/bin/env python3
"""Fresh, fail-closed capacity probe for a fleet dispatch target."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

import goalflight_capacity as capacity
import goalflight_compat as fcntl


SCHEMA = "goalflight.fleet.preflight.v1"
DEFAULT_TIMEOUT_S = 8.0
# No incident record pins an exact crash load. Refuse before full per-core
# saturation because a false refusal costs a retry while a saturated Studio can
# wedge every job on it. This is normalized load/cores, never raw load.
HARD_LOAD_PER_CORE = 0.85
# The fleet's GPU launch wrapper uses this flock as its serialization authority.
# Probe the existing inode without creating it: a missing lock is unknown, not
# evidence that the GPU is idle.
DEFAULT_GPU_LOCK_PATH = "/tmp/warpx-gpu-lock"
UNPINNED_HOSTNAME = "__goalflight_unpinned__"

CommandRunner = Callable[[list[str], float], tuple[int, str, str]]


class ProbeCollectionError(RuntimeError):
    pass


def normalize_timeout_s(value: object) -> float:
    try:
        timeout_s = float(value)
    except (TypeError, ValueError) as exc:
        raise ProbeCollectionError("preflight timeout must be numeric") from exc
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ProbeCollectionError("preflight timeout must be finite and positive")
    return timeout_s


def _default_runner(argv: list[str], timeout_s: float) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeCollectionError(f"{argv[0]} timed out after {timeout_s:g}s") from exc
    return proc.returncode, proc.stdout, proc.stderr


def _run_text(argv: list[str], *, timeout_s: float, runner: CommandRunner) -> str:
    code, stdout, stderr = runner(argv, timeout_s)
    if code != 0:
        detail = stderr.strip() or stdout.strip() or "no output"
        raise ProbeCollectionError(f"{argv[0]} failed (exit {code}): {detail}")
    value = stdout.strip()
    if not value:
        raise ProbeCollectionError(f"{argv[0]} returned no output")
    return value


def parse_memory_pressure(output: str) -> tuple[int, float]:
    total_match = re.search(r"system has\s+(\d+)", output, re.IGNORECASE)
    percent_match = re.search(r"memory free percentage:\s*([0-9]+(?:\.[0-9]+)?)%", output, re.IGNORECASE)
    if not total_match or not percent_match:
        raise ProbeCollectionError("memory_pressure -Q output missing total bytes or available percentage")
    total_bytes = int(total_match.group(1))
    available_percent = float(percent_match.group(1))
    if total_bytes <= 0 or not 0.0 <= available_percent <= 100.0:
        raise ProbeCollectionError("memory_pressure -Q returned an invalid memory value")
    return total_bytes, available_percent


def parse_swap_used_mb(output: str) -> float:
    match = re.search(r"\bused\s*=\s*([0-9]+(?:\.[0-9]+)?)([KMGT])", output, re.IGNORECASE)
    if not match:
        raise ProbeCollectionError("vm.swapusage output missing used amount")
    value = float(match.group(1))
    unit = match.group(2).upper()
    factors = {"K": 1.0 / 1024.0, "M": 1.0, "G": 1024.0, "T": 1024.0 * 1024.0}
    return value * factors[unit]


def probe_gpu_lock(path: Path) -> str:
    if not path.is_file():
        return "missing"
    try:
        with path.open("rb") as handle:
            acquired = False
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                return "held"
            finally:
                if acquired:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
    except OSError:
        return "error"
    return "available"


def collect_measurements(
    *,
    gpu_lock_path: Path,
    timeout_s: float,
    runner: CommandRunner = _default_runner,
) -> dict[str, Any]:
    hostname = _run_text(["hostname"], timeout_s=timeout_s, runner=runner).splitlines()[0].strip()
    cores_text = _run_text(["sysctl", "-n", "hw.ncpu"], timeout_s=timeout_s, runner=runner)
    try:
        cores = int(cores_text)
    except ValueError as exc:
        raise ProbeCollectionError("hw.ncpu was not an integer") from exc
    if cores <= 0:
        raise ProbeCollectionError("hw.ncpu must be positive")

    pressure_text = _run_text(["memory_pressure", "-Q"], timeout_s=timeout_s, runner=runner)
    total_bytes, available_percent = parse_memory_pressure(pressure_text)
    swap_text = _run_text(["sysctl", "-n", "vm.swapusage"], timeout_s=timeout_s, runner=runner)
    swap_used_mb = parse_swap_used_mb(swap_text)
    load_1m = float(os.getloadavg()[0])

    # memory_pressure's available percentage follows the kernel's reclaim-aware
    # pressure model. Raw vm_stat free pages do not: compressed and reclaimable
    # pages make that proxy diverge and caused the SC-16 false-trigger class.
    total_ram_mb = total_bytes / (1024.0 * 1024.0)
    pressure_available_mb = total_ram_mb * available_percent / 100.0
    return {
        "hostname": hostname,
        "measured_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "cores": cores,
        "load_1m": load_1m,
        "load_per_core": load_1m / cores,
        "total_ram_mb": total_ram_mb,
        "memory_pressure_available_percent": available_percent,
        "pressure_available_mb": pressure_available_mb,
        "swap_used_mb": swap_used_mb,
        "gpu_lock_path": str(gpu_lock_path),
        "gpu_lock_state": probe_gpu_lock(gpu_lock_path),
    }


def _canonical_hostname(value: object) -> str:
    hostname = str(value or "").strip().lower().rstrip(".")
    return hostname[:-6] if hostname.endswith(".local") else hostname


def evaluate_measurements(
    measurements: dict[str, Any],
    *,
    agent: str,
    expected_hostname: str,
) -> dict[str, Any]:
    incoming_rss_mb = int(capacity.AGENT_RSS_MB.get(agent, capacity.DEFAULT_WORST_WORKER_MB))
    controller_reserve_mb = int(capacity.DEFAULT_RESERVE_MB)
    gpu_lock_state = str(measurements.get("gpu_lock_state") or "unknown")
    gpu_extra_reserve_mb = int(capacity.DEFAULT_WORST_WORKER_MB) if gpu_lock_state == "held" else 0

    try:
        pressure_available_mb = float(measurements["pressure_available_mb"])
        load_per_core = float(measurements["load_per_core"])
        swap_used_mb = float(measurements["swap_used_mb"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ProbeCollectionError(f"preflight measurement missing or invalid: {exc}") from exc

    hard_required_available_mb = incoming_rss_mb + controller_reserve_mb + gpu_extra_reserve_mb
    predicted_post_launch_headroom_mb = pressure_available_mb - hard_required_available_mb
    hard_reasons: list[str] = []
    answering_hostname = str(measurements.get("hostname") or "").strip()
    if not answering_hostname:
        hard_reasons.append("answering_hostname_missing")
    elif not expected_hostname or expected_hostname == UNPINNED_HOSTNAME:
        hard_reasons.append("expected_hostname_unpinned")
    elif _canonical_hostname(answering_hostname) != _canonical_hostname(expected_hostname):
        hard_reasons.append("answering_hostname_mismatch")
    if gpu_lock_state not in {"available", "held"}:
        hard_reasons.append(f"gpu_lock_{gpu_lock_state}")
    if swap_used_mb > 0.0:
        # On this fleet, touching swap starts the reboot watchdog. Observation
        # means the hard condition already happened; it can never be a warning.
        hard_reasons.append("swap_observed")
    if load_per_core >= HARD_LOAD_PER_CORE:
        hard_reasons.append("load_per_core_hard")
    if predicted_post_launch_headroom_mb <= 0.0:
        hard_reasons.append("predicted_post_launch_headroom_hard")

    decision = "refuse" if hard_reasons else "allow"
    reported_expected_hostname = "" if expected_hostname == UNPINNED_HOSTNAME else expected_hostname
    structured_measurements = {
        "identity": {
            "answering_hostname": answering_hostname,
            "expected_hostname": reported_expected_hostname,
        },
        "load": {
            "one_minute": float(measurements.get("load_1m", load_per_core * float(measurements.get("cores") or 0))),
            "cores": int(measurements.get("cores") or 0),
            "per_core": load_per_core,
        },
        "memory": {
            "pressure_available_mb": pressure_available_mb,
            "pressure_available_percent": float(measurements.get("memory_pressure_available_percent") or 0.0),
            "incoming_worker_rss_mb": incoming_rss_mb,
            "controller_reserve_mb": controller_reserve_mb,
            "gpu_extra_reserve_mb": gpu_extra_reserve_mb,
            "predicted_post_launch_headroom_mb": predicted_post_launch_headroom_mb,
        },
        "swap": {
            "used_mb": swap_used_mb,
            "observed": swap_used_mb > 0.0,
        },
        "gpu": {
            "lock_path": str(measurements.get("gpu_lock_path") or ""),
            "lock_state": gpu_lock_state,
            "lock_held": gpu_lock_state == "held",
        },
        # Controller overwrites this with a monotonic upper bound covering the
        # complete SSH probe. Zero here means "evaluated on the answering host".
        "age_s": 0.0,
    }
    return {
        "schema": SCHEMA,
        "ok": True,
        "decision": decision,
        "reasons": hard_reasons,
        "agent": agent,
        "expected_hostname": reported_expected_hostname,
        **measurements,
        "thresholds": {
            "hard_load_per_core": HARD_LOAD_PER_CORE,
            "incoming_worker_rss_mb": incoming_rss_mb,
            "controller_reserve_mb": controller_reserve_mb,
            "gpu_extra_reserve_mb": gpu_extra_reserve_mb,
            "hard_required_available_mb": hard_required_available_mb,
        },
        "predicted_post_launch_headroom_mb": predicted_post_launch_headroom_mb,
        "measurements": structured_measurements,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe remote fleet dispatch headroom")
    parser.add_argument("--agent", required=True)
    parser.add_argument("--expected-hostname", required=True)
    parser.add_argument("--gpu-lock-path", default=DEFAULT_GPU_LOCK_PATH)
    parser.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        timeout_s = normalize_timeout_s(args.timeout_s)
        measurements = collect_measurements(
            gpu_lock_path=Path(args.gpu_lock_path),
            timeout_s=timeout_s,
        )
        payload = evaluate_measurements(
            measurements,
            agent=args.agent,
            expected_hostname=args.expected_hostname,
        )
    except ProbeCollectionError as exc:
        # A probe failure is data about a possibly overloaded node. Return
        # structured refusal data; the controller must never treat it as healthy.
        payload = {
            "schema": SCHEMA,
            "ok": False,
            "decision": "refuse",
            "reasons": ["probe_collection_failed"],
            "agent": args.agent,
            "expected_hostname": args.expected_hostname,
            "error": str(exc),
        }
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
