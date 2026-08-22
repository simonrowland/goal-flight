#!/usr/bin/env python3
"""Hermetic remote fleet preflight guard tests; never opens SSH."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_capacity as capacity
import goalflight_fleet as fleet
import goalflight_fleet_dispatch as dispatch
import goalflight_fleet_preflight as preflight


BASE_SHA = "0123456789abcdef0123456789abcdef01234567"
AGENT = "codex-acp"


def measurement(
    *,
    hostname: str = "studio-3",
    raw_load: float = 2.0,
    cores: int = 24,
    available_mb: float = 32768.0,
    swap_used_mb: float = 0.0,
    gpu_lock_state: str = "available",
) -> dict[str, object]:
    return {
        "hostname": hostname,
        "measured_at": "2026-08-22T12:00:00+00:00",
        "cores": cores,
        "load_1m": raw_load,
        "load_per_core": raw_load / cores,
        "total_ram_mb": 131072.0,
        "memory_pressure_available_percent": available_mb / 131072.0 * 100.0,
        "pressure_available_mb": available_mb,
        "swap_used_mb": swap_used_mb,
        "gpu_lock_path": preflight.DEFAULT_GPU_LOCK_PATH,
        "gpu_lock_state": gpu_lock_state,
    }


def evaluate(**overrides: object) -> dict[str, object]:
    return preflight.evaluate_measurements(
        measurement(**overrides),
        agent=AGENT,
        expected_hostname="studio-3",
    )


def preview() -> dispatch.DispatchPreview:
    return dispatch.DispatchPreview(
        dispatch_id="preflight-fixture",
        node_id="studio-3-alias",
        agent=AGENT,
        billing_account="openai/default",
        prompt="fixture brief",
        worktree_path=None,
        base_sha=BASE_SHA,
    )


def fixture_fleet(path: Path) -> None:
    fleet.bootstrap(path)
    doc = fleet.read_json(path / "fleet.json")
    doc["nodes"] = {
        "studio-3-alias": {
            "node_id": "studio-3-alias",
            "status": "active",
            "ssh": {"alias": "studio-3-alias", "hostname": "route.example"},
            "repo_root": str(ROOT),
            "state_dir": "/tmp/goal-flight-preflight-fixture",
            "expected_hostname": "studio-3",
            "answering_hostname": "studio-3",
            "gpu_lock_path": preflight.DEFAULT_GPU_LOCK_PATH,
        }
    }
    fleet._atomic_write_json(path / "fleet.json", doc)


def test_healthy_and_per_core_load_thresholds() -> None:
    healthy = evaluate(raw_load=8.0, cores=24)
    saturated = evaluate(raw_load=8.0, cores=4)
    assert healthy["decision"] == "allow", healthy
    assert saturated["decision"] == "refuse", saturated
    assert "load_per_core_hard" in saturated["reasons"]
    assert healthy["load_1m"] == saturated["load_1m"] == 8.0
    assert healthy["measurements"]["load"] == {
        "one_minute": 8.0,
        "cores": 24,
        "per_core": 8.0 / 24.0,
    }


def test_structured_middle_and_hard_refuse_boundaries() -> None:
    load_middle = evaluate(raw_load=(preflight.HARD_LOAD_PER_CORE - 0.01) * 24, cores=24)
    load_hard = evaluate(raw_load=preflight.HARD_LOAD_PER_CORE * 24, cores=24)
    assert load_middle["decision"] == "allow", load_middle
    assert load_middle["measurements"]["load"]["cores"] == 24
    assert load_middle["measurements"]["load"]["per_core"] == preflight.HARD_LOAD_PER_CORE - 0.01
    assert load_hard["decision"] == "refuse", load_hard

    baseline = evaluate()
    thresholds = baseline["thresholds"]
    hard_required = float(thresholds["hard_required_available_mb"])
    memory_middle = evaluate(available_mb=hard_required + 0.01)
    memory_hard = evaluate(available_mb=hard_required)
    assert memory_middle["decision"] == "allow", memory_middle
    assert memory_middle["measurements"]["memory"]["pressure_available_mb"] == hard_required + 0.01
    assert memory_hard["decision"] == "refuse", memory_hard


def test_swap_is_immediate_refusal_and_gpu_lock_tightens_reserve() -> None:
    swapped = evaluate(swap_used_mb=0.01)
    assert swapped["decision"] == "refuse", swapped
    assert "swap_observed" in swapped["reasons"]
    assert swapped["measurements"]["swap"] == {"used_mb": 0.01, "observed": True}

    incoming = capacity.AGENT_RSS_MB[AGENT]
    available = capacity.DEFAULT_RESERVE_MB + incoming + 1
    without_gpu = evaluate(available_mb=available, gpu_lock_state="available")
    with_gpu = evaluate(available_mb=available, gpu_lock_state="held")
    assert without_gpu["decision"] != "refuse", without_gpu
    assert with_gpu["decision"] == "refuse", with_gpu
    assert with_gpu["thresholds"]["gpu_extra_reserve_mb"] == capacity.DEFAULT_WORST_WORKER_MB
    assert with_gpu["measurements"]["gpu"]["lock_held"] is True


def test_pressure_collector_uses_memory_pressure_not_free_pages() -> None:
    commands: list[list[str]] = []

    def fake_runner(argv: list[str], _timeout_s: float) -> tuple[int, str, str]:
        commands.append(argv)
        if argv == ["hostname"]:
            return 0, "studio-3\n", ""
        if argv == ["sysctl", "-n", "hw.ncpu"]:
            return 0, "24\n", ""
        if argv == ["memory_pressure", "-Q"]:
            return 0, (
                "The system has 137438953472 (8388608 pages with a page size of 16384).\n"
                "System-wide memory free percentage: 42%\n"
            ), ""
        if argv == ["sysctl", "-n", "vm.swapusage"]:
            return 0, "total = 0.00M  used = 0.00M  free = 0.00M\n", ""
        raise AssertionError(argv)

    with tempfile.TemporaryDirectory() as td:
        lock = Path(td) / "warpx-gpu-lock"
        lock.write_text("")
        result = preflight.collect_measurements(
            gpu_lock_path=lock,
            timeout_s=2.0,
            runner=fake_runner,
        )
    assert result["hostname"] == "studio-3"
    assert result["memory_pressure_available_percent"] == 42.0
    assert ["memory_pressure", "-Q"] in commands
    assert not any(argv and argv[0] == "vm_stat" for argv in commands)


def test_alias_identity_mismatch_refuses_and_names_answering_host() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        fixture_fleet(fleet_dir)
        mismatched = preflight.evaluate_measurements(
            measurement(hostname="studio-1"),
            agent=AGENT,
            expected_hostname="studio-3",
        )

        def runner(_argv: list[str]) -> tuple[int, str, str]:
            return 0, json.dumps(mismatched), ""

        try:
            dispatch.run_dispatch_preflight(
                fleet_dir,
                preview(),
                runner=runner,
                timeout_s=2.0,
            )
        except dispatch.DispatchGateError as exc:
            message = str(exc)
            assert "node=studio-3-alias" in message
            assert "answering_hostname=studio-1" in message
            assert "expected_hostname=studio-3" in message
            assert "answering_hostname_mismatch" in message
        else:
            raise AssertionError("identity mismatch dispatched")


def test_override_proceeds_but_prints_and_records_measurements() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        fixture_fleet(fleet_dir)
        refused = evaluate(available_mb=1.0)

        def runner(_argv: list[str]) -> tuple[int, str, str]:
            return 0, json.dumps(refused), ""

        result = dispatch.run_dispatch_preflight(
            fleet_dir,
            preview(),
            runner=runner,
            timeout_s=3.0,
            override=True,
        )
        assert result["decision"] == "override", result
        assert result["original_decision"] == "refuse"
        assert result["hostname"] == "studio-3"
        assert result["measurements"]["load"]["cores"] == 24
        assert result["measurements"]["age_s"] >= 0.0
        assert "pressure_available=1.00MB" in result["message"]
        assert "answering_hostname=studio-3" in result["message"]


def test_timeout_is_fail_closed_and_timeout_is_in_ssh_policy() -> None:
    captured: list[list[str]] = []

    def timeout_runner(argv: list[str]) -> tuple[int, str, str]:
        captured.append(argv)
        raise subprocess.TimeoutExpired(argv, 2.5)

    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        fixture_fleet(fleet_dir)
        try:
            dispatch.run_dispatch_preflight(
                fleet_dir,
                preview(),
                runner=timeout_runner,
                timeout_s=2.5,
            )
        except dispatch.DispatchGateError as exc:
            message = str(exc)
            assert "probe_timeout" in message
            assert "timeout=2.50s" in message
        else:
            raise AssertionError("timed-out probe dispatched")
    joined = " ".join(captured[0])
    assert "ConnectTimeout=3" in joined
    assert "ServerAliveCountMax=1" in joined


def test_unpinned_identity_and_invalid_timeout_fail_closed() -> None:
    unpinned = preflight.evaluate_measurements(
        measurement(),
        agent=AGENT,
        expected_hostname=preflight.UNPINNED_HOSTNAME,
    )
    assert unpinned["decision"] == "refuse"
    assert unpinned["expected_hostname"] == ""
    assert "expected_hostname_unpinned" in unpinned["reasons"]
    for invalid in (0, -1, float("inf"), float("nan")):
        try:
            preflight.normalize_timeout_s(invalid)
        except preflight.ProbeCollectionError:
            pass
        else:
            raise AssertionError(f"invalid timeout accepted: {invalid!r}")


def test_measurement_is_never_cached() -> None:
    calls = 0
    payload = evaluate()

    def runner(_argv: list[str]) -> tuple[int, str, str]:
        nonlocal calls
        calls += 1
        return 0, json.dumps(payload), ""

    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        fixture_fleet(fleet_dir)
        first = dispatch.run_dispatch_preflight(fleet_dir, preview(), runner=runner, timeout_s=2.0)
        second = dispatch.run_dispatch_preflight(fleet_dir, preview(), runner=runner, timeout_s=2.0)
    assert calls == 2
    assert first["fresh"] is second["fresh"] is True
    assert first["measurement_age_s"] >= 0.0 and second["measurement_age_s"] >= 0.0
    assert "message" not in first and "message" not in second


def main() -> None:
    tests = (
        test_healthy_and_per_core_load_thresholds,
        test_structured_middle_and_hard_refuse_boundaries,
        test_swap_is_immediate_refusal_and_gpu_lock_tightens_reserve,
        test_pressure_collector_uses_memory_pressure_not_free_pages,
        test_alias_identity_mismatch_refuses_and_names_answering_host,
        test_override_proceeds_but_prints_and_records_measurements,
        test_timeout_is_fail_closed_and_timeout_is_in_ssh_policy,
        test_unpinned_identity_and_invalid_timeout_fail_closed,
        test_measurement_is_never_cached,
    )
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
