#!/usr/bin/env python3
"""Tests for fleet status --fleet CLI aggregation (Track A goal 9c)."""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_fleet as fleet
import goalflight_fleet_billing as billing
import goalflight_fleet_status as status
import goalflight_fleet_status_cli as fleet_status_cli

FIXTURES = ROOT / "tests" / "fixtures" / "fleet_mirrors"


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def green_runner(_argv: list[str]) -> tuple[int, str, str]:
    return 0, "logged_in: true\n", ""


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


def _write_quarantined_dispatch(fleet_dir: Path, *, node_id: str = "localhost") -> str:
    dispatch_id = "acp-codex-fixture-01"
    dispatch_dir = fleet_dir / "register" / "dispatches" / dispatch_id
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "valid_ok.json", dispatch_dir / "status.json")
    fleet._atomic_write_json(
        dispatch_dir / "meta.json",
        {
            "dispatch_id": dispatch_id,
            "node_id": node_id,
            "lease_active": True,
            "pid_hint": "alive",
            "ssh_reachable": True,
            "last_mirror_seq": 10,
        },
    )
    fleet._atomic_write_json(
        fleet_dir / "register" / "aggregate.json",
        {
            "schema": "goalflight.fleet.register.aggregate.v1",
            "schema_version": 1,
            "min_reader_version": 1,
            "open_user_needs": [],
            "active_dispatches": [dispatch_id],
            "last_steering": None,
        },
    )
    return dispatch_id


def test_build_fleet_status_quarantined_mirror_stale_row() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        billing.link_account_to_node(
            fleet_dir,
            "openai/default",
            "localhost",
            runner=green_runner,
        )
        dispatch_id = _write_quarantined_dispatch(fleet_dir)

        payload = fleet_status_cli.build_fleet_status(fleet_dir)
        assert_true("available", payload["available"] is True)
        assert_true("auth nodes", len(payload["auth"]["nodes"]) == 1)
        assert_true("flat dispatches", len(payload["dispatches"]) == 1)

        row = payload["dispatches"][0]
        assert_true("node", row["node"] == "localhost")
        assert_true("dispatch_id", row["dispatch_id"] == dispatch_id)
        assert_true("state", row["state"] == "quarantined")
        assert_true("reason", row["quarantine_reason"] == status.QUARANTINE_MIRROR_STALE)
        assert_true("may_release", row["may_release"] is False)

        node = payload["nodes"][0]
        assert_true("node dispatches", len(node["dispatches"]) == 1)
        assert_true("auth_probe field", node["accounts"][0]["auth_probe"] == "green")


def test_cmd_status_legacy_shape_unchanged() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = fleet.cmd_status(argparse_namespace(fleet_dir=fleet_dir, fleet=False, json=False))
        assert_true("exit ok", code == 0)
        payload = json.loads(buf.getvalue())
        assert_true("legacy files key", "files" in payload)
        assert_true("legacy nodes count", payload["nodes"]["count"] == 1)
        assert_true("no dispatches key", "dispatches" not in payload)


def test_cmd_status_fleet_json_and_table() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        _write_quarantined_dispatch(fleet_dir)

        json_buf = io.StringIO()
        with redirect_stdout(json_buf):
            code = fleet.cmd_status(argparse_namespace(fleet_dir=fleet_dir, fleet=True, json=True))
        assert_true("json exit ok", code == 0)
        payload = json.loads(json_buf.getvalue())
        assert_true("json quarantined", payload["dispatches"][0]["state"] == "quarantined")

        table_buf = io.StringIO()
        with redirect_stdout(table_buf):
            fleet.cmd_status(argparse_namespace(fleet_dir=fleet_dir, fleet=True, json=False))
        table = table_buf.getvalue()
        assert_true("table header", "quarantine_reason" in table)
        assert_true("table row", "mirror_stale" in table)
        assert_true("table dispatch", "acp-codex-fixture-01" in table)


class argparse_namespace:
    def __init__(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


def main() -> None:
    test_build_fleet_status_quarantined_mirror_stale_row()
    test_cmd_status_legacy_shape_unchanged()
    test_cmd_status_fleet_json_and_table()
    print("OK: fleet status CLI tests pass")


if __name__ == "__main__":
    main()
