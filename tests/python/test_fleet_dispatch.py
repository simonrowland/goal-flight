#!/usr/bin/env python3
"""Tests for fleet dispatch MVP (Track A goals 11a–11f)."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("fleet dispatch fixtures use POSIX /tmp paths")

import io
import base64
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_fleet as fleet
import goalflight_fleet_billing as billing
import goalflight_fleet_dispatch as fleet_dispatch
import goalflight_fleet_status as status

FIXTURES = ROOT / "tests" / "fixtures" / "fleet_mirrors"


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
        assert_true("cleanup refs", "git_prune_claude_refs" in classes)
        assert_true("git fetch", "git_fetch" in classes)
        assert_true("worktree add", "git_worktree_add" in classes)
        assert_true(
            "cleanup before fetch",
            classes.index("git_prune_claude_refs") < classes.index("git_fetch"),
        )
        assert_true("fetch before worktree", classes.index("git_fetch") < classes.index("git_worktree_add"))
        worktree_add = next(c for c in payload["remote_commands"] if c["command_class"] == "git_worktree_add")
        assert_true("worktree add uses fetched ref", worktree_add["argv"][-1] == "origin/main")
        assert_true("worktree add avoids local HEAD", "HEAD" not in worktree_add["argv"])
        acp = next(c for c in payload["remote_commands"] if c["command_class"] == "acp_run")
        assert_true("acp cwd worktree", payload["worktree_path"] in acp["argv"])
        assert_true(
            "acp status json",
            "/tmp/goal-flight-dispatch-test/dispatches/acp-dispatch-explicit/status.json"
            in acp["argv"],
        )


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


def test_remote_failure_surfaces_ssh_details() -> None:
    def fail_fetch(argv: list[str]) -> tuple[int, str, str]:
        joined = " ".join(argv)
        if "goalflight_cleanup_dispatch_refs.py" in joined:
            return 0, '{"deleted":[]}', ""
        return 255, "", "real stderr"

    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        preview = fleet_dispatch.preview_dispatch(
            fleet_dir,
            node_id="localhost",
            agent="codex-acp",
            billing_account="openai/default",
            prompt="chunk.md",
            dispatch_id="acp-remote-failure",
        )
        try:
            fleet_dispatch.acquire_lock_chain(
                fleet_dir,
                preview,
                runner=fail_fetch,
            )
            assert_true("should raise", False)
        except fleet_dispatch.DispatchError as exc:
            message = str(exc)
            assert_true("command class", "remote git_fetch failed" in message)
            assert_true("exit code", "exit 255" in message)
            assert_true("stderr", "real stderr" in message)
            assert_true("ssh argv", "ssh argv:" in message)


def test_cleanup_refs_failure_stops_before_fetch() -> None:
    secret = "cleanup failure prompt secret"
    prompt_b64 = base64.b64encode(secret.encode("utf-8")).decode("ascii")
    invoked: list[str] = []

    def fail_cleanup(argv: list[str]) -> tuple[int, str, str]:
        joined = " ".join(argv)
        if "goalflight_cleanup_dispatch_refs.py" in joined:
            invoked.append("git_prune_claude_refs")
            return 17, f"stdout leaked {secret}", f"stderr leaked {prompt_b64}"
        if " fetch " in f" {joined} ":
            invoked.append("git_fetch")
        return 0, "{}", ""

    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        preview = fleet_dispatch.preview_dispatch(
            fleet_dir,
            node_id="localhost",
            agent="codex-acp",
            billing_account="openai/default",
            prompt=secret,
            dispatch_id="acp-cleanup-failure",
        )
        try:
            fleet_dispatch.execute_dispatch(fleet_dir, preview, runner=fail_cleanup)
            assert_true("should raise", False)
        except fleet_dispatch.DispatchError as exc:
            message = str(exc)
            assert_true("failure class", "remote git_prune_claude_refs failed" in message)
            assert_true("exit code", "exit 17" in message)
            assert_true("cleanup ran", invoked == ["git_prune_claude_refs"])
            assert_true("fetch not invoked", "git_fetch" not in invoked)
            assert_true("stdout secret redacted", secret not in message)
            assert_true("stderr prompt b64 redacted", prompt_b64 not in message)
            assert_true("failure marker", "<redacted>" in message)


def test_redact_argv_masks_prompt_values_everywhere() -> None:
    secret = "sensitive prompt with spaces"
    prompt_b64 = base64.b64encode(secret.encode("utf-8")).decode("ascii")
    cases = [
        ["cmd", "--prompt", secret],
        ["cmd", f"--prompt={secret}"],
        ["cmd", "--prompt-b64", prompt_b64],
        ["cmd", f"--prompt-b64={prompt_b64}"],
        ["ssh", "host", "--", "/bin/zsh", "-c", f"exec runner --prompt '{secret}' --json"],
        ["ssh", "host", "--", "/bin/zsh", "-c", f"exec runner --prompt-b64 {prompt_b64} --json"],
        ["ssh", "host", "--", "/bin/zsh", "-c", f"exec runner --prompt-b64={prompt_b64} --json"],
    ]
    for argv in cases:
        redacted = " ".join(fleet_dispatch._redact_argv(argv))
        assert_true("secret redacted", secret not in redacted)
        assert_true("prompt b64 redacted", prompt_b64 not in redacted)
        assert_true("redaction marker", "<redacted>" in redacted)

    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        preview = fleet_dispatch.preview_dispatch(
            fleet_dir,
            node_id="localhost",
            agent="codex-acp",
            billing_account="openai/default",
            prompt=secret,
            dispatch_id="acp-redact-preview",
        )
        preview_payload = preview.to_dict()
        assert_true("preview prompt marker", preview_payload["prompt"] == "<redacted>")
        serialized = json.dumps(preview_payload)
        assert_true("preview prompt redacted", secret not in serialized)
        assert_true("preview prompt b64 redacted", prompt_b64 not in serialized)
        assert_true("preview marker", "<redacted>" in serialized)

        def fail_acp(argv: list[str]) -> tuple[int, str, str]:
            joined = " ".join(argv)
            if "goalflight_acp_run.py" in joined:
                return 9, f"stdout echoed {secret}", f"stderr echoed --prompt-b64 {prompt_b64}"
            return 0, "{}", ""

        try:
            fleet_dispatch.acquire_lock_chain(fleet_dir, preview, runner=fail_acp)
            assert_true("should raise", False)
        except fleet_dispatch.DispatchError as exc:
            message = str(exc)
            assert_true("failure class", "remote acp_run failed" in message)
            assert_true("failure prompt redacted", secret not in message)
            assert_true("failure prompt b64 redacted", prompt_b64 not in message)
            assert_true("failure marker", "<redacted>" in message)

        live_preview = fleet_dispatch.preview_dispatch(
            fleet_dir,
            node_id="localhost",
            agent="codex-acp",
            billing_account="openai/default",
            prompt=secret,
            dispatch_id="acp-redact-live",
        )

        def echo_acp(argv: list[str]) -> tuple[int, str, str]:
            joined = " ".join(argv)
            if "goalflight_acp_run.py" in joined:
                return 0, f"stdout echoed {secret}", f"stderr echoed --prompt-b64 {prompt_b64}"
            return 0, "{}", ""

        chain = fleet_dispatch.acquire_lock_chain(fleet_dir, live_preview, runner=echo_acp)
        try:
            live_result = json.dumps({"remote_log": chain.remote_log})
            assert_true("live stdout/stderr prompt redacted", secret not in live_result)
            assert_true("live stdout/stderr prompt b64 redacted", prompt_b64 not in live_result)
            assert_true("live stdout/stderr marker", "<redacted>" in live_result)
        finally:
            fleet_dispatch.release_lock_chain(
                fleet_dir,
                live_preview,
                acquired=chain.acquired,
                fencing_token=chain.fencing_token,
            )


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


def test_resolve_dispatch_runner_stub_and_live() -> None:
    stub_args = Args(stub_remote=True)
    stub_runner = fleet_dispatch.resolve_dispatch_runner(stub_args)
    assert stub_runner is not None
    code, _stdout, _stderr = stub_runner(["ssh", "ignored"])
    assert_true("stub ok", code == 0)

    live_args = Args(stub_remote=False)
    live_runner = fleet_dispatch.resolve_dispatch_runner(live_args)
    assert_true("live default", live_runner is fleet_dispatch.default_ssh_runner)


def test_exec_without_stub_uses_runner() -> None:
    captured: list[list[str]] = []

    def capture_runner(argv: list[str]) -> tuple[int, str, str]:
        captured.append(list(argv))
        return 0, "{}", ""

    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        preview = fleet_dispatch.preview_dispatch(
            fleet_dir,
            node_id="localhost",
            agent="codex-acp",
            billing_account="openai/default",
            prompt="chunk.md",
            dispatch_id="acp-live-runner",
        )
        args = Args(
            fleet_dir=fleet_dir,
            node="localhost",
            agent="codex-acp",
            billing_account="openai/default",
            prompt="chunk.md",
            exec=True,
            thin_defaults=False,
            stub_remote=False,
            stub_runner=capture_runner,
        )
        args.stub_runner = capture_runner
        fleet_dispatch.execute_dispatch(fleet_dir, preview, runner=fleet_dispatch.resolve_dispatch_runner(args))
        assert_true("remote commands", len(captured) >= 1)


def test_exec_runner_uses_node_ssh_identity() -> None:
    captured: list[list[str]] = []

    def capture_runner(argv: list[str]) -> tuple[int, str, str]:
        captured.append(list(argv))
        return 0, "{}", ""

    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
        fleet_doc["nodes"]["localhost"]["ssh"] = {
            "alias": "localhost",
            "hostname": "remote.example",
            "user": "runner",
            "identity_file": "~/.ssh/fleet_key",
        }
        fleet._atomic_write_json(fleet_dir / "fleet.json", fleet_doc)
        preview = fleet_dispatch.preview_dispatch(
            fleet_dir,
            node_id="localhost",
            agent="codex-acp",
            billing_account="openai/default",
            prompt="chunk.md",
            dispatch_id="acp-node-ssh",
        )
        fleet_dispatch.execute_dispatch(fleet_dir, preview, runner=capture_runner)
        assert_true("remote commands", len(captured) >= 1)
        first = captured[0]
        assert_true("identity flag", "-i" in first)
        identity_idx = first.index("-i")
        assert_true("identity path", first[identity_idx + 1].endswith("/.ssh/fleet_key"))
        assert_true("user host target", "runner@remote.example" in first)


def test_sync_finalize_clears_locks() -> None:
    mirror_json = json.dumps(
        {
            "schema": "goalflight.acp-run.v1",
            "dispatch_id": "acp-sync-finalize",
            "state": "complete",
            "agent": "codex-acp",
            "events_seen": 3,
        }
    )

    def mirror_runner(argv: list[str]) -> tuple[int, str, str]:
        joined = " ".join(argv)
        if "goalflight_acp_run.py" in joined:
            return 0, mirror_json, ""
        if joined.endswith("status.json") or "read_status_file" in joined or " cat " in joined:
            return 0, mirror_json, ""
        return 0, "{}", ""

    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        preview = fleet_dispatch.preview_dispatch(
            fleet_dir,
            node_id="localhost",
            agent="codex-acp",
            billing_account="openai/default",
            prompt="chunk.md",
            dispatch_id="acp-sync-finalize",
        )
        result = fleet_dispatch.execute_dispatch(
            fleet_dir,
            preview,
            runner=mirror_runner,
            stub_terminal=False,
        )
        assert_true("ok", result["ok"] is True)
        assert_true("finalize ok", (result.get("finalize") or {}).get("ok") is True)
        lock = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("lock cleared", lock is None or lock.get("state") == "released")
        meta = json.loads(
            (fleet_dir / "register" / "dispatches" / "acp-sync-finalize" / "meta.json").read_text()
        )
        assert_true("remote status path", meta.get("remote_status_path"))
        assert_true("lease inactive", meta.get("lease_active") is False)


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
    test_remote_failure_surfaces_ssh_details()
    test_redact_argv_masks_prompt_values_everywhere()
    test_quarantine_blocks_dispatch()
    test_resolve_dispatch_runner_stub_and_live()
    test_exec_without_stub_uses_runner()
    test_exec_runner_uses_node_ssh_identity()
    test_stub_e2e_terminal_clears_locks()
    test_sync_finalize_clears_locks()
    test_ledger_remote_lease_id_roundtrip()
    print("OK: fleet dispatch tests pass")


if __name__ == "__main__":
    main()
