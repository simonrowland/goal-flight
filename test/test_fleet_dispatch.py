#!/usr/bin/env python3
"""Tests for fleet dispatch MVP (Track A goals 11a–11f)."""

from __future__ import annotations

import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_fleet as fleet
import goalflight_fleet_billing as billing
import goalflight_fleet_dispatch as fleet_dispatch
import goalflight_fleet_status as status

FIXTURES = ROOT / "test" / "fixtures" / "fleet_mirrors"


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def green_runner(_argv: list[str]) -> tuple[int, str, str]:
    return 0, "logged_in: true\n", ""


def _fixture_fleet(fleet_dir: Path) -> None:
    fleet.bootstrap(fleet_dir)
    fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
    fleet_doc["nodes"] = {
        "localhost": {
            "node_id": "localhost",
            "status": "active",
            "ssh": {"alias": "localhost", "hostname": "localhost"},
            "repo_root": str(ROOT),
            "state_dir": "/tmp/goal-flight-dispatch-test",
            "billing_accounts": [],
            "added_at": "2026-05-24T12:00:00+00:00",
        }
    }
    fleet._atomic_write_json(fleet_dir / "fleet.json", fleet_doc)
    billing.link_account_to_node(
        fleet_dir,
        "openai/default",
        "localhost",
        runner=green_runner,
    )


class Args:
    def __init__(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


def test_explicit_dry_run_preview() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        preview = fleet_dispatch.preview_dispatch(
            fleet_dir,
            node_id="localhost",
            agent="codex-acp",
            billing_account="openai/default",
            prompt="chunk.md",
            dispatch_id="acp-dispatch-explicit",
        )
        payload = preview.to_dict()
        assert_true("node", payload["node"] == "localhost")
        assert_true("agent", payload["agent"] == "codex-acp")
        assert_true("billing", payload["billing_account"] == "openai/default")
        assert_true("worktree", "worktrees/acp-dispatch-explicit" in payload["worktree_path"])
        assert_true("remote cmds", len(payload["remote_commands"]) >= 2)
        classes = [c["command_class"] for c in payload["remote_commands"]]
        assert_true("worktree add", "git_worktree_add" in classes)


def test_red_auth_blocks_exec() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        billing.write_probe_artifact(
            fleet_dir,
            "localhost",
            {
                "account_key": "openai/default",
                "status": "red",
                "provider": "openai",
                "probed_at": "2026-05-24T12:00:00+00:00",
            },
        )
        args = Args(
            fleet_dir=fleet_dir,
            node="localhost",
            agent="codex-acp",
            billing_account="openai/default",
            prompt="chunk.md",
            exec=True,
            thin_defaults=False,
            stub_remote=True,
            stub_terminal=False,
        )
        code = fleet_dispatch.cmd_dispatch(args)
        assert_true("blocked", code == 1)


def test_thin_defaults_shows_billing_banner() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        preview = fleet_dispatch.preview_dispatch(
            fleet_dir,
            node_id="localhost",
            agent=None,
            billing_account=None,
            prompt="chunk.md",
            thin_mode=True,
        )
        assert_true("banner", preview.billing_banner is not None)
        assert_true("billing visible", preview.billing_account)


def test_lock_chain_rollback_on_worktree_failure() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        preview = fleet_dispatch.preview_dispatch(
            fleet_dir,
            node_id="localhost",
            agent="codex-acp",
            billing_account="openai/default",
            prompt="chunk.md",
            dispatch_id="acp-lock-rollback",
        )
        try:
            fleet_dispatch.acquire_lock_chain(
                fleet_dir,
                preview,
                runner=lambda _a: (1, "", "fail"),
                stop_after="worktree",
            )
            assert_true("should raise", False)
        except fleet_dispatch.DispatchError:
            pass
        lock = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("account lock released", lock is None or lock.get("state") == "released")


def test_quarantine_blocks_dispatch() -> None:
    dispatch_id = "acp-quarantine-block"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        dispatch_dir = fleet_dir / "register" / "dispatches" / dispatch_id
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        (dispatch_dir / "status.json").write_text((FIXTURES / "valid_ok.json").read_text())
        fleet._atomic_write_json(
            dispatch_dir / "meta.json",
            {
                "dispatch_id": dispatch_id,
                "node_id": "localhost",
                "lease_active": True,
                "pid_hint": "alive",
                "ssh_reachable": True,
                "last_mirror_seq": 99,
            },
        )
        try:
            fleet_dispatch.assert_dispatch_gates(
                fleet_dir,
                node_id="localhost",
                billing_account="openai/default",
            )
        except fleet_dispatch.DispatchGateError as exc:
            assert_true("quarantine code", exc.code == "quarantine")
            return
        assert_true("expected gate error", False)


def test_stub_e2e_terminal_clears_locks() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        preview = fleet_dispatch.preview_dispatch(
            fleet_dir,
            node_id="localhost",
            agent="codex-acp",
            billing_account="openai/default",
            prompt="chunk.md",
            dispatch_id="acp-e2e-stub",
        )
        result = fleet_dispatch.execute_dispatch(
            fleet_dir,
            preview,
            runner=lambda _a: (0, "{}", ""),
            stub_terminal=True,
        )
        assert_true("ok", result["ok"] is True)
        assert_true("remote lease", result.get("remote_lease_id"))
        lock = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("lock cleared", lock is None or lock.get("state") == "released")


def test_ledger_remote_lease_id_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        preview = fleet_dispatch.preview_dispatch(
            fleet_dir,
            node_id="localhost",
            agent="codex-acp",
            billing_account="openai/default",
            prompt="chunk.md",
            dispatch_id="acp-ledger-lease",
        )
        chain = fleet_dispatch.LockChainResult(remote_lease_id="lease-123", acquired=["account"])
        info = fleet_dispatch.record_dispatch_ledger(preview, chain)
        record = json.loads(Path(info["path"]).read_text())
        assert_true("remote_lease_id", record.get("remote_lease_id") == "lease-123")


def main() -> None:
    test_explicit_dry_run_preview()
    test_red_auth_blocks_exec()
    test_thin_defaults_shows_billing_banner()
    test_lock_chain_rollback_on_worktree_failure()
    test_quarantine_blocks_dispatch()
    test_stub_e2e_terminal_clears_locks()
    test_ledger_remote_lease_id_roundtrip()
    print("OK: fleet dispatch tests pass")


if __name__ == "__main__":
    main()
