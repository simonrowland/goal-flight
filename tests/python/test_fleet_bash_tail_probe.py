#!/usr/bin/env python3
"""Tests for fleet bash-tail remote probe (Phase 2 goal 15a)."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_fleet as fleet
import goalflight_fleet_bash_tail_probe as bash_probe


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def _fixture_fleet(fleet_dir: Path) -> None:
    fleet.bootstrap(fleet_dir)
    fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
    fleet_doc["nodes"] = {
        "localhost": {
            "node_id": "localhost",
            "status": "active",
            "ssh": {"alias": "localhost", "hostname": "localhost"},
            "repo_root": str(ROOT),
            "state_dir": "/tmp/goal-flight-bash-probe",
            "billing_accounts": [],
            "added_at": "2026-05-24T12:00:00+00:00",
        }
    }
    fleet._atomic_write_json(fleet_dir / "fleet.json", fleet_doc)


def test_probe_writes_artifact_with_injected_runner() -> None:
    calls: list[list[str]] = []

    def green_runner(_argv: list[str]) -> tuple[int, str, str]:
        calls.append(list(_argv))
        return 0, "", ""

    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        row = bash_probe.run_adapter_probe(
            fleet_dir,
            "localhost",
            "opencode-bash-tail",
            runner=green_runner,
        )
        assert_true("ok", row.get("ok") is True)
        assert_true("calls", len(calls) >= 2)
        path = bash_probe.probe_artifact_path(fleet_dir, "localhost", "opencode-bash-tail")
        assert_true("artifact", path.exists())
        doc = json.loads(path.read_text())
        assert_true("schema", doc.get("schema") == bash_probe.PROBE_SCHEMA)


def test_doctor_surfaces_probe_summary() -> None:
    import goalflight_doctor as doctor

    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        probes_dir = fleet_dir / "probes"
        probes_dir.mkdir(parents=True, exist_ok=True)
        (probes_dir / "bash-tail-localhost-opencode-bash-tail.json").write_text(
            json.dumps(
                {
                    "schema": bash_probe.PROBE_SCHEMA,
                    "ok": False,
                    "node_id": "localhost",
                    "adapter": "opencode-bash-tail",
                    "marker_seen": False,
                    "probed_at": "2026-05-24T12:00:00+00:00",
                }
            )
            + "\n"
        )
        summary = doctor._fleet_bash_tail_probe_summary(fleet_dir)
        assert_true("available", summary.get("available") is True)
        node = summary["nodes"][0]
        assert_true("probe red", node["bash_tail_probe"]["opencode-bash-tail"]["ok"] is False)


def main() -> None:
    test_probe_writes_artifact_with_injected_runner()
    test_doctor_surfaces_probe_summary()
    print("OK: fleet bash-tail probe tests pass")


if __name__ == "__main__":
    main()
