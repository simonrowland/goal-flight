#!/usr/bin/env python3
"""Tests for fleet billing auth probes and dispatch gate."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_fleet as fleet
import goalflight_fleet_billing as billing


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def green_runner(_argv: list[str]) -> tuple[int, str, str]:
    return 0, "logged_in: true\n", ""


def red_runner(_argv: list[str]) -> tuple[int, str, str]:
    return 1, "", "not logged in"


def _fixture_fleet(fleet_dir: Path, *, node_id: str = "localhost") -> None:
    fleet.bootstrap(fleet_dir)
    fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
    fleet_doc["nodes"] = {
        node_id: {
            "node_id": node_id,
            "status": "active",
            "ssh": {"alias": "localhost", "hostname": "localhost"},
            "repo_root": str(ROOT),
            "state_dir": "~/.goal-flight",
            "billing_accounts": [],
            "added_at": "2026-05-24T12:00:00+00:00",
        }
    }
    fleet._atomic_write_json(fleet_dir / "fleet.json", fleet_doc)


def test_account_link_runs_probe_and_writes_artifact() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        result = billing.link_account_to_node(
            fleet_dir,
            "openai/default",
            "localhost",
            runner=green_runner,
        )
        assert_true("link ok", result["ok"] is True)
        assert_true("probe green", result["auth_probe"]["status"] == "green")
        artifact = billing.read_probe_artifact(fleet_dir, "localhost", "openai/default")
        assert_true("artifact saved", artifact is not None and artifact["status"] == "green")


def test_doctor_fleet_shape() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        billing.link_account_to_node(
            fleet_dir,
            "openai/default",
            "localhost",
            runner=green_runner,
        )
        billing.link_account_to_node(
            fleet_dir,
            "anthropic/session-local",
            "localhost",
            runner=lambda _a: (0, "claude 1.2.3\n", ""),
        )
        summary = billing.fleet_auth_doctor(fleet_dir, refresh=False)
        assert_true("available", summary["available"] is True)
        nodes = summary["nodes"]
        assert_true("one node", len(nodes) == 1)
        accounts = nodes[0]["accounts"]
        assert_true("two accounts", len(accounts) == 2)
        for entry in accounts:
            assert_true("auth_probe field", "auth_probe" in entry)


def test_dispatch_gate_blocks_red_auth() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        billing.link_account_to_node(
            fleet_dir,
            "openai/default",
            "localhost",
            runner=red_runner,
        )
        try:
            billing.assert_dispatch_auth(fleet_dir, "localhost", "openai/default")
            assert_true("should block", False)
        except billing.DispatchAuthError as exc:
            assert_true("red status", exc.auth_probe == "red")


def test_account_unlink_removes_link_and_artifact() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        billing.link_account_to_node(
            fleet_dir,
            "openai/default",
            "localhost",
            runner=green_runner,
        )
        billing.unlink_account_from_node(fleet_dir, "openai/default", "localhost")
        doc = fleet.read_json(fleet_dir / "fleet.json")
        assert_true("unlinked", "openai/default" not in doc["nodes"]["localhost"]["billing_accounts"])
        assert_true(
            "artifact removed",
            billing.read_probe_artifact(fleet_dir, "localhost", "openai/default") is None,
        )


def test_doctor_cli_fleet_json() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        billing.link_account_to_node(
            fleet_dir,
            "openai/default",
            "localhost",
            runner=green_runner,
        )
        import goalflight_doctor as doctor

        payload = doctor.doctor(ROOT, fleet=True, fleet_dir=fleet_dir, fleet_probe=False)
        assert_true("fleet key", "fleet" in payload)
        raw = json.dumps(payload)
        assert_true("auth_probe in json", "auth_probe" in raw)


def main() -> None:
    for test in (
        test_account_link_runs_probe_and_writes_artifact,
        test_doctor_fleet_shape,
        test_dispatch_gate_blocks_red_auth,
        test_account_unlink_removes_link_and_artifact,
        test_doctor_cli_fleet_json,
    ):
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
