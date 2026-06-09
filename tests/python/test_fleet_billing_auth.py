#!/usr/bin/env python3
"""Tests for fleet billing auth probes and dispatch gate."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
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


def tooling_runner(_argv: list[str]) -> tuple[int, str, str]:
    return 127, "", "python: command not found"


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


def test_remote_auth_probe_uses_node_venv_python() -> None:
    captured: list[list[str]] = []

    def ssh_runner(argv: list[str]) -> tuple[int, str, str]:
        captured.append(list(argv))
        return (
            0,
            json.dumps(
                {
                    "schema": billing.AUTH_PROBE_SCHEMA,
                    "account_key": "openai/default",
                    "provider": "openai",
                    "status": "green",
                }
            ),
            "",
        )

    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir, node_id="remote-node")
        fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
        node = fleet_doc["nodes"]["remote-node"]
        node["ssh"] = {"alias": "mac-studio-test", "hostname": "10.0.0.10"}
        node["state_dir"] = "/Users/dev/.goal-flight"
        fleet._atomic_write_json(fleet_dir / "fleet.json", fleet_doc)

        result = billing.link_account_to_node(
            fleet_dir,
            "openai/default",
            "remote-node",
            ssh_runner=ssh_runner,
        )
        assert_true("probe green", result["auth_probe"]["status"] == "green")
        assert_true("ssh captured", bool(captured))
        assert_true(
            "node venv python",
            "/Users/dev/.goal-flight/venvs/acp-0.10/bin/python" in " ".join(captured[0]),
        )


def test_tooling_auth_probe_is_inconclusive_not_red() -> None:
    payload = billing.run_local_auth_probe(
        "openai/default",
        {"accounts": [{"account_key": "openai/default", "provider": "openai"}]},
        runner=tooling_runner,
    )
    assert_true("local 127 inconclusive", payload["status"] == "inconclusive")


def test_remote_tooling_probe_reprobes_instead_of_cached_red() -> None:
    def setup_remote(fleet_dir: Path) -> None:
        _fixture_fleet(fleet_dir, node_id="remote-node")
        fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
        node = fleet_doc["nodes"]["remote-node"]
        node["ssh"] = {"alias": "mac-studio-test", "hostname": "10.0.0.10"}
        node["state_dir"] = "/Users/dev/.goal-flight"
        fleet._atomic_write_json(fleet_dir / "fleet.json", fleet_doc)

    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        setup_remote(fleet_dir)
        result = billing.link_account_to_node(
            fleet_dir,
            "openai/default",
            "remote-node",
            ssh_runner=lambda _a: (127, "", "bare-python-not-found"),
        )
        assert_true("remote 127 inconclusive", result["auth_probe"]["status"] == "inconclusive")
        artifact = billing.read_probe_artifact(fleet_dir, "remote-node", "openai/default")
        assert_true("artifact is not red", artifact is not None and artifact["status"] == "inconclusive")
        try:
            billing.assert_dispatch_auth(fleet_dir, "remote-node", "openai/default")
            assert_true("should block", False)
        except billing.DispatchAuthError as exc:
            assert_true("gate says inconclusive", exc.auth_probe == "inconclusive")

        calls: list[list[str]] = []

        def green_remote(argv: list[str]) -> tuple[int, str, str]:
            calls.append(list(argv))
            return (
                0,
                json.dumps(
                    {
                        "schema": billing.AUTH_PROBE_SCHEMA,
                        "account_key": "openai/default",
                        "provider": "openai",
                        "status": "green",
                    }
                ),
                "",
            )

        summary = billing.fleet_auth_doctor(fleet_dir, refresh=False, ssh_runner=green_remote)
        assert_true("doctor re-probed inconclusive", len(calls) == 1)
        account = summary["nodes"][0]["accounts"][0]
        assert_true("doctor refreshed green", account["auth_probe"] == "green")


def test_remote_auth_denied_json_can_cache_red() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir, node_id="remote-node")
        fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
        node = fleet_doc["nodes"]["remote-node"]
        node["ssh"] = {"alias": "mac-studio-test", "hostname": "10.0.0.10"}
        fleet._atomic_write_json(fleet_dir / "fleet.json", fleet_doc)

        result = billing.link_account_to_node(
            fleet_dir,
            "openai/default",
            "remote-node",
            ssh_runner=lambda _a: (
                1,
                json.dumps(
                    {
                        "schema": billing.AUTH_PROBE_SCHEMA,
                        "account_key": "openai/default",
                        "provider": "openai",
                        "status": "red",
                    }
                ),
                "",
            ),
        )
        assert_true("remote auth denied stays red", result["auth_probe"]["status"] == "red")
        artifact = billing.read_probe_artifact(fleet_dir, "remote-node", "openai/default")
        assert_true("red artifact saved", artifact is not None and artifact["status"] == "red")


def test_grok_auth_probe_green() -> None:
    payload = billing.run_local_auth_probe(
        "grok/shared",
        {"accounts": [{"account_key": "grok/shared", "provider": "grok"}]},
        runner=lambda _a: (0, "logged_in\n", ""),
    )
    assert_true("grok green", payload["status"] == "green")


def test_cursor_auth_probe_uses_status_not_version() -> None:
    assert_true(
        "cursor status argv",
        billing.probe_argv("cursor") == ["cursor-agent", "status"],
    )
    green = billing.run_local_auth_probe(
        "cursor/shared",
        {"accounts": [{"account_key": "cursor/shared", "provider": "cursor"}]},
        runner=lambda _a: (0, "✓ Logged in as simon@example.com\n", ""),
    )
    assert_true("cursor green when logged in", green["status"] == "green")
    login_ok = billing.run_local_auth_probe(
        "cursor/shared",
        {"accounts": [{"account_key": "cursor/shared", "provider": "cursor"}]},
        runner=lambda _a: (0, "Login successful!\n", ""),
    )
    assert_true("cursor green on login successful", login_ok["status"] == "green")
    red = billing.run_local_auth_probe(
        "cursor/shared",
        {"accounts": [{"account_key": "cursor/shared", "provider": "cursor"}]},
        runner=lambda _a: (0, "Not logged in\n", ""),
    )
    assert_true("cursor red when not logged in", red["status"] == "red")
    version_only = billing.run_local_auth_probe(
        "cursor/shared",
        {"accounts": [{"account_key": "cursor/shared", "provider": "cursor"}]},
        runner=lambda _a: (0, "cursor-agent 2026.05.20\n", ""),
    )
    assert_true("cursor version-only inconclusive", version_only["status"] == "inconclusive")


def main() -> None:
    for test in (
        test_account_link_runs_probe_and_writes_artifact,
        test_doctor_fleet_shape,
        test_dispatch_gate_blocks_red_auth,
        test_account_unlink_removes_link_and_artifact,
        test_doctor_cli_fleet_json,
        test_remote_auth_probe_uses_node_venv_python,
        test_tooling_auth_probe_is_inconclusive_not_red,
        test_remote_tooling_probe_reprobes_instead_of_cached_red,
        test_remote_auth_denied_json_can_cache_red,
        test_grok_auth_probe_green,
        test_cursor_auth_probe_uses_status_not_version,
    ):
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
