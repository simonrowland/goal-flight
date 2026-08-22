#!/usr/bin/env python3
"""Tests for fleet node add wizard."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_fleet as fleet
import goalflight_fleet_node as fleet_node
import goalflight_fleet_ssh as fleet_ssh


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def mock_runner(argv: list[str]) -> tuple[int, str, str]:
    if "hostname" in " ".join(argv):
        return 0, "fixture-host\n", ""
    return 0, "goal-flight-probe-ok\n", ""


def test_node_add_dry_run_preview() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        fleet_dir = base / "fleet"
        ssh_cfg = base / "ssh_config"
        ssh_cfg.write_text("Host build-1\n  HostName localhost\n  User test\n")
        fleet.bootstrap(fleet_dir)
        result = fleet_node.add_node_from_ssh(
            fleet_dir,
            ssh_alias="build-1",
            repo_root=str(ROOT),
            state_dir="~/.goal-flight",
            expected_hostname="fixture-host",
            ssh_config=ssh_cfg,
            runner=mock_runner,
            dry_run=True,
            iso_now="2026-05-24T12:00:00+00:00",
        )
        assert_true("dry run ok", result["ok"] is True)
        assert_true("preview after", result["preview"]["after"]["repo_root"] == str(ROOT))
        assert_true("dry run does not invent hostname", result["preview"]["after"]["answering_hostname"] == "")


def test_node_add_saves_and_status_lists() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        fleet_dir = base / "fleet"
        ssh_cfg = base / "ssh_config"
        ssh_cfg.write_text("Host build-1\n  HostName localhost\n  User test\n")
        fleet.bootstrap(fleet_dir)
        saved = fleet_node.add_node_from_ssh(
            fleet_dir,
            ssh_alias="build-1",
            repo_root=str(ROOT),
            state_dir="~/.goal-flight",
            expected_hostname="fixture-host",
            ssh_config=ssh_cfg,
            runner=mock_runner,
            iso_now="2026-05-24T12:00:00+00:00",
        )
        assert_true("saved ok", saved["ok"] is True)
        doc = fleet.read_json(fleet_dir / "fleet.json")
        assert_true("node present", "build-1" in doc["nodes"])
        assert_true("answering hostname pinned", doc["nodes"]["build-1"]["answering_hostname"] == "fixture-host")
        assert_true("expected hostname pinned", doc["nodes"]["build-1"]["expected_hostname"] == "fixture-host")
        audit = (fleet_dir / "audit" / "nodes.jsonl").read_text().strip()
        assert_true("audit written", "node_add" in audit)


def test_probe_failure_remediation() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        fleet_dir = base / "fleet"
        ssh_cfg = base / "ssh_config"
        ssh_cfg.write_text("Host build-1\n  HostName localhost\n")

        def fail_runner(_argv: list[str]) -> tuple[int, str, str]:
            return 1, "", "connection refused"

        fleet.bootstrap(fleet_dir)
        result = fleet_node.add_node_from_ssh(
            fleet_dir,
            ssh_alias="build-1",
            repo_root=str(ROOT),
            state_dir="~/.goal-flight",
            ssh_config=ssh_cfg,
            runner=fail_runner,
        )
        assert_true("probe failed", result["ok"] is False)
        assert_true("remediation", "remediation" in result)


def test_node_add_refuses_alias_that_answers_as_wrong_host() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        fleet_dir = base / "fleet"
        ssh_cfg = base / "ssh_config"
        ssh_cfg.write_text("Host studio-3\n  HostName studio-3.local\n")
        fleet.bootstrap(fleet_dir)
        result = fleet_node.add_node_from_ssh(
            fleet_dir,
            ssh_alias="studio-3",
            node_id="studio-3",
            expected_hostname="studio-3",
            repo_root=str(ROOT),
            state_dir="~/.goal-flight",
            ssh_config=ssh_cfg,
            runner=mock_runner,
        )
        assert_true("identity mismatch refused", result["ok"] is False)
        assert_true("identity failure code", result["failure_code"] == "identity_mismatch")
        assert_true("answer named", "fixture-host" in result["remediation"])


def test_all_probes_use_allowlist() -> None:
    host = fleet_ssh.SshHostSpec(alias="x", hostname="x")
    for command_class, _extra in fleet_node.PROBE_PLAN:
        remote = fleet_ssh.build_remote_command(command_class, repo_root=str(ROOT))
        fleet_ssh.build_ssh_command(host, remote, command_class=command_class)
        fleet_ssh.assert_allowed(command_class)


def main() -> None:
    for test in (
        test_node_add_dry_run_preview,
        test_node_add_saves_and_status_lists,
        test_probe_failure_remediation,
        test_node_add_refuses_alias_that_answers_as_wrong_host,
        test_all_probes_use_allowlist,
    ):
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
