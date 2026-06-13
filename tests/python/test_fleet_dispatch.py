#!/usr/bin/env python3
"""Tests for fleet dispatch MVP (Track A goals 11a–11f)."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("fleet dispatch fixtures use POSIX /tmp paths")

import io
import base64
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_fleet as fleet
import goalflight_fleet_billing as billing
import goalflight_fleet_dispatch as fleet_dispatch
import goalflight_fleet_launch_detached as fleet_launch
import goalflight_fleet_status as status

FIXTURES = ROOT / "tests" / "fixtures" / "fleet_mirrors"
BASE_SHA = "0123456789abcdef0123456789abcdef01234567"


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def green_runner(_argv: list[str]) -> tuple[int, str, str]:
    return 0, "logged_in: true\n", ""


def _extract_wrapped_flag(argv: list[str], flag: str, default: str) -> str:
    joined = " ".join(argv)
    match = re.search(rf"{re.escape(flag)}\s+'?([^'\s]+)'?", joined)
    if match:
        return match.group(1)
    for idx, part in enumerate(argv):
        if part == flag and idx + 1 < len(argv):
            return argv[idx + 1]
    return default


def launch_receipt_for_argv(argv: list[str]) -> str:
    dispatch_id = _extract_wrapped_flag(argv, "--dispatch-id", "acp-test")
    node_id = _extract_wrapped_flag(argv, "--node-id", "localhost")
    status_json = _extract_wrapped_flag(
        argv,
        "--status-json",
        f"/tmp/goal-flight-dispatch-test/dispatches/{dispatch_id}/status.json",
    )
    base_sha = _extract_wrapped_flag(argv, "--base-sha", BASE_SHA)
    return json.dumps(
        {
            "schema": "goalflight.fleet.launch_receipt.v1",
            "dispatch_id": dispatch_id,
            "node_id": node_id,
            "remote_pid": 4242,
            "remote_lstart": "Thu Jun 11 12:00:00 2026",
            "remote_identity": {
                "pid": 4242,
                "lstart": "Thu Jun 11 12:00:00 2026",
                "comm": "python3",
            },
            "remote_status_path": status_json,
            "remote_state_dir": "/tmp/goal-flight-dispatch-test",
            "launcher_log_path": f"/tmp/goal-flight-dispatch-test/dispatches/{dispatch_id}/dispatcher.log",
            "started_at": "2026-06-11T12:00:00+00:00",
            "worktree_base_sha": base_sha,
        },
        sort_keys=True,
    )


def receipt_runner(argv: list[str]) -> tuple[int, str, str]:
    if "goalflight_fleet_launch_detached.py" in " ".join(argv):
        return 0, launch_receipt_for_argv(argv), ""
    return 0, "{}", ""


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


@contextmanager
def live_ssh_env(value: str | None):
    old_live_ssh = os.environ.get("GOALFLIGHT_LIVE_SSH")
    if value is None:
        os.environ.pop("GOALFLIGHT_LIVE_SSH", None)
    else:
        os.environ["GOALFLIGHT_LIVE_SSH"] = value
    try:
        yield
    finally:
        if old_live_ssh is None:
            os.environ.pop("GOALFLIGHT_LIVE_SSH", None)
        else:
            os.environ["GOALFLIGHT_LIVE_SSH"] = old_live_ssh


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
            base_sha=BASE_SHA,
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
        assert_true("verify commit", "git_verify_commit" in classes)
        assert_true("worktree add", "git_worktree_add" in classes)
        assert_true(
            "cleanup before fetch",
            classes.index("git_prune_claude_refs") < classes.index("git_fetch"),
        )
        assert_true("fetch before verify", classes.index("git_fetch") < classes.index("git_verify_commit"))
        assert_true("verify before worktree", classes.index("git_verify_commit") < classes.index("git_worktree_add"))
        assert_true("fetch before worktree", classes.index("git_fetch") < classes.index("git_worktree_add"))
        verify = next(c for c in payload["remote_commands"] if c["command_class"] == "git_verify_commit")
        assert_true("verify exact base", f"{BASE_SHA}^{{commit}}" in verify["argv"])
        worktree_add = next(c for c in payload["remote_commands"] if c["command_class"] == "git_worktree_add")
        assert_true("worktree add uses base sha", worktree_add["argv"][-1] == BASE_SHA)
        assert_true("worktree add detached", "--detach" in worktree_add["argv"])
        assert_true("worktree add avoids local HEAD", "HEAD" not in worktree_add["argv"])
        launch = next(c for c in payload["remote_commands"] if c["command_class"] == "launch_detached")
        assert_true("launch cwd worktree", payload["worktree_path"] in launch["argv"])
        assert_true("launch base sha", launch["argv"][launch["argv"].index("--base-sha") + 1] == BASE_SHA)
        assert_true(
            "launch status json",
            "/tmp/goal-flight-dispatch-test/dispatches/acp-dispatch-explicit/status.json"
            in launch["argv"],
        )


def test_red_auth_blocks_exec() -> None:
    with live_ssh_env("1"):
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
                base_sha=BASE_SHA,
                exec=True,
                thin_defaults=False,
                stub_remote=True,
                stub_terminal=False,
            )
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = fleet_dispatch.cmd_dispatch(args)
            assert_true("blocked", code == 1)


def test_exec_without_live_ssh_env_refuses_before_runner() -> None:
    captured: list[list[str]] = []

    def capture_runner(argv: list[str]) -> tuple[int, str, str]:
        captured.append(list(argv))
        return 0, "{}", ""

    with live_ssh_env(None):
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            _fixture_fleet(fleet_dir)
            args = Args(
                fleet_dir=fleet_dir,
                node="localhost",
                agent="codex-acp",
                billing_account="openai/default",
                prompt="chunk.md",
                base_sha=BASE_SHA,
                exec=True,
                thin_defaults=False,
                stub_remote=False,
                stub_runner=capture_runner,
                stub_terminal=True,
            )
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = fleet_dispatch.cmd_dispatch(args)
            message = stderr.getvalue()
            assert_true("usage refusal", code == 2)
            assert_true("runner not called", captured == [])
            assert_true("env named", "GOALFLIGHT_LIVE_SSH=1" in message)
            assert_true("hermetic safety", "hermetic suites must never live-SSH" in message)


def test_exec_with_live_ssh_env_uses_runner() -> None:
    captured: list[list[str]] = []

    def capture_runner(argv: list[str]) -> tuple[int, str, str]:
        captured.append(list(argv))
        return receipt_runner(argv)

    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            _fixture_fleet(fleet_dir)
            args = Args(
                fleet_dir=fleet_dir,
                node="localhost",
                agent="codex-acp",
                billing_account="openai/default",
                prompt="chunk.md",
                dispatch_id="acp-live-opt-in",
                base_sha=BASE_SHA,
                exec=True,
                thin_defaults=False,
                stub_remote=False,
                stub_runner=capture_runner,
                stub_terminal=True,
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = fleet_dispatch.cmd_dispatch(args)
            assert_true("exec ok", code == 0)
            assert_true("runner called", len(captured) >= 1)


def test_preview_ignores_live_ssh_env() -> None:
    with live_ssh_env(None):
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            _fixture_fleet(fleet_dir)
            args = Args(
                fleet_dir=fleet_dir,
                node="localhost",
                agent="codex-acp",
                billing_account="openai/default",
                prompt="chunk.md",
                base_sha=BASE_SHA,
                exec=False,
                thin_defaults=False,
                stub_remote=False,
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = fleet_dispatch.cmd_dispatch(args)
            payload = json.loads(stdout.getvalue())
            assert_true("preview ok", code == 0)
            assert_true("dry run", payload["dry_run"] is True)


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
            base_sha=BASE_SHA,
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
            base_sha=BASE_SHA,
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
            base_sha=BASE_SHA,
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
        lock = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("pre-launch account lock released", lock is None or lock.get("state") == "released")


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
            base_sha=BASE_SHA,
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


def test_pending_row_written_before_remote_mutation() -> None:
    dispatch_id = "acp-pending-before-ssh"

    def crash_before_remote(_argv: list[str]) -> tuple[int, str, str]:
        meta = json.loads((fleet_dir / "register" / "dispatches" / dispatch_id / "meta.json").read_text())
        assert_true("pending row visible", meta.get("row_state") == "launch_pending")
        raise SystemExit(99)

    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        preview = fleet_dispatch.preview_dispatch(
            fleet_dir,
            node_id="localhost",
            agent="codex-acp",
            billing_account="openai/default",
            prompt="chunk.md",
            dispatch_id=dispatch_id,
            base_sha=BASE_SHA,
        )
        try:
            fleet_dispatch.execute_dispatch(fleet_dir, preview, runner=crash_before_remote)
            assert_true("should exit", False)
        except SystemExit:
            pass
        meta = json.loads((fleet_dir / "register" / "dispatches" / dispatch_id / "meta.json").read_text())
        assert_true("pending row remains", meta.get("row_state") == "launch_pending")
        lock = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("lock active", lock is not None and lock.get("state") == "active")
        assert_true("meta account key", meta.get("account_key") == "openai/default")
        assert_true("meta fencing token", meta.get("account_lock_fencing_token") == lock.get("fencing_token"))
        aggregate = fleet.read_json(fleet_dir / "register" / "aggregate.json")
        assert_true("aggregate intent visible", dispatch_id in aggregate.get("active_dispatches", []))


def test_base_sha_absent_fails_pre_launch_and_rolls_back() -> None:
    invoked: list[str] = []

    def missing_base(argv: list[str]) -> tuple[int, str, str]:
        joined = " ".join(argv)
        if "goalflight_cleanup_dispatch_refs.py" in joined:
            invoked.append("git_prune_claude_refs")
            return 0, '{"deleted":[]}', ""
        if " fetch " in f" {joined} ":
            invoked.append("git_fetch")
            return 0, "", ""
        if " rev-parse " in f" {joined} ":
            invoked.append("git_verify_commit")
            return 128, "", "base commit absent"
        if "goalflight_fleet_launch_detached.py" in joined:
            invoked.append("launch_detached")
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
            dispatch_id="acp-missing-base",
            base_sha=BASE_SHA,
        )
        try:
            fleet_dispatch.execute_dispatch(fleet_dir, preview, runner=missing_base)
            assert_true("should raise", False)
        except fleet_dispatch.DispatchError as exc:
            assert_true("verify failure surfaced", "remote git_verify_commit failed" in str(exc))
        assert_true("launch not issued", invoked == ["git_prune_claude_refs", "git_fetch", "git_verify_commit"])
        lock = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("account lock released", lock is None or lock.get("state") == "released")


def test_dispatch_omitted_base_sha_teaches() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        args = Args(
            fleet_dir=fleet_dir,
            node="localhost",
            agent="codex-acp",
            billing_account="openai/default",
            prompt="chunk.md",
            exec=False,
            thin_defaults=False,
            stub_remote=False,
        )
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = fleet_dispatch.cmd_dispatch(args)
        message = stderr.getvalue()
        assert_true("usage refusal", code == 2)
        assert_true("base sha named", "--base-sha <40-hex commit>" in message)


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
            base_sha=BASE_SHA,
        )
        preview_payload = preview.to_dict()
        assert_true("preview prompt marker", preview_payload["prompt"] == "<redacted>")
        serialized = json.dumps(preview_payload)
        assert_true("preview prompt redacted", secret not in serialized)
        assert_true("preview prompt b64 redacted", prompt_b64 not in serialized)
        assert_true("preview marker", "<redacted>" in serialized)

        def fail_launch(argv: list[str]) -> tuple[int, str, str]:
            joined = " ".join(argv)
            if "goalflight_fleet_launch_detached.py" in joined:
                return 9, f"stdout echoed {secret}", f"stderr echoed --prompt-b64 {prompt_b64}"
            return 0, "{}", ""

        chain = fleet_dispatch.acquire_lock_chain(fleet_dir, preview, runner=fail_launch)
        message = chain.launch_unconfirmed_error or ""
        assert_true("unconfirmed", chain.launch_unconfirmed is True)
        assert_true("failure class", "remote launch_detached failed" in message)
        assert_true("failure prompt redacted", secret not in message)
        assert_true("failure prompt b64 redacted", prompt_b64 not in message)
        assert_true("failure marker", "<redacted>" in message)
        fleet_dispatch.release_lock_chain(
            fleet_dir,
            preview,
            acquired=chain.acquired,
            fencing_token=chain.fencing_token,
        )

        live_preview = fleet_dispatch.preview_dispatch(
            fleet_dir,
            node_id="localhost",
            agent="codex-acp",
            billing_account="openai/default",
            prompt=secret,
            dispatch_id="acp-redact-live",
            base_sha=BASE_SHA,
        )

        def echo_launch(argv: list[str]) -> tuple[int, str, str]:
            joined = " ".join(argv)
            if "goalflight_fleet_launch_detached.py" in joined:
                return 0, launch_receipt_for_argv(argv), f"stderr echoed --prompt-b64 {prompt_b64}"
            return 0, "{}", ""

        chain = fleet_dispatch.acquire_lock_chain(fleet_dir, live_preview, runner=echo_launch)
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
            base_sha=BASE_SHA,
        )
        result = fleet_dispatch.execute_dispatch(
            fleet_dir,
            preview,
            runner=receipt_runner,
            stub_terminal=True,
        )
        assert_true("ok", result["ok"] is True)
        assert_true("launch receipt", result.get("launch_receipt", {}).get("remote_pid") == 4242)
        lock = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("lock cleared", lock is None or lock.get("state") == "released")


def test_launch_unconfirmed_keeps_account_lock() -> None:
    def fail_launch(argv: list[str]) -> tuple[int, str, str]:
        if "goalflight_fleet_launch_detached.py" in " ".join(argv):
            return 1, "", "ssh connection reset after launch command was issued"
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
            dispatch_id="acp-launch-unconfirmed",
            base_sha=BASE_SHA,
        )
        result = fleet_dispatch.execute_dispatch(fleet_dir, preview, runner=fail_launch)
        assert_true("result ok", result["ok"] is True)
        assert_true("unconfirmed", result.get("launch_unconfirmed") is True)
        lock = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("lock remains active", lock is not None and lock.get("state") == "active")
        meta = json.loads(
            (fleet_dir / "register" / "dispatches" / "acp-launch-unconfirmed" / "meta.json").read_text()
        )
        assert_true("meta unconfirmed", meta.get("launch_unconfirmed") is True)
        record = json.loads(Path(result["ledger"]["path"]).read_text())
        assert_true("ledger state", record.get("state") == "launch_unconfirmed")


def test_preview_passes_recovery_flag_only_with_unconfirmed_meta() -> None:
    dispatch_id = "acp-recovery-flag"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        plain = fleet_dispatch.preview_dispatch(
            fleet_dir,
            node_id="localhost",
            agent="codex-acp",
            billing_account="openai/default",
            prompt="chunk.md",
            dispatch_id=dispatch_id,
            base_sha=BASE_SHA,
        )
        plain_launch = next(c for c in plain.remote_commands if c["command_class"] == "launch_detached")
        assert_true("plain no recover", "--recover-unconfirmed" not in plain_launch["argv"])

        dispatch_dir = fleet_dir / "register" / "dispatches" / dispatch_id
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        fleet._atomic_write_json(
            dispatch_dir / "meta.json",
            {
                "dispatch_id": dispatch_id,
                "node_id": "localhost",
                "launch_unconfirmed": True,
            },
        )
        recovery = fleet_dispatch.preview_dispatch(
            fleet_dir,
            node_id="localhost",
            agent="codex-acp",
            billing_account="openai/default",
            prompt="chunk.md",
            dispatch_id=dispatch_id,
            base_sha=BASE_SHA,
        )
        recovery_launch = next(c for c in recovery.remote_commands if c["command_class"] == "launch_detached")
        assert_true("recover flag", "--recover-unconfirmed" in recovery_launch["argv"])


def test_launch_unconfirmed_retry_reuses_same_account_lock() -> None:
    dispatch_id = "acp-launch-retry"

    def fail_first(argv: list[str]) -> tuple[int, str, str]:
        if "goalflight_fleet_launch_detached.py" in " ".join(argv):
            return 255, "", "ssh connection lost after remote launch may have started"
        return 0, "{}", ""

    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        first = fleet_dispatch.preview_dispatch(
            fleet_dir,
            node_id="localhost",
            agent="codex-acp",
            billing_account="openai/default",
            prompt="chunk.md",
            dispatch_id=dispatch_id,
            base_sha=BASE_SHA,
        )
        first_result = fleet_dispatch.execute_dispatch(fleet_dir, first, runner=fail_first)
        assert_true("first unconfirmed", first_result.get("launch_unconfirmed") is True)
        first_lock = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("first lock active", first_lock is not None and first_lock.get("state") == "active")

        second = fleet_dispatch.preview_dispatch(
            fleet_dir,
            node_id="localhost",
            agent="codex-acp",
            billing_account="openai/default",
            prompt="chunk.md",
            dispatch_id=dispatch_id,
            base_sha=BASE_SHA,
        )
        launch = next(c for c in second.remote_commands if c["command_class"] == "launch_detached")
        assert_true("recovery flag", "--recover-unconfirmed" in launch["argv"])
        second_result = fleet_dispatch.execute_dispatch(fleet_dir, second, runner=receipt_runner)
        assert_true("second ok", second_result["ok"] is True)
        assert_true("receipt", second_result.get("launch_receipt", {}).get("remote_pid") == 4242)
        second_lock = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true(
            "same lock token",
            second_lock is not None and second_lock.get("fencing_token") == first_lock.get("fencing_token"),
        )


def test_launch_helper_sanitizes_worker_env() -> None:
    env = fleet_launch._sanitized_env(
        {
            "PATH": "/bin",
            "GOALFLIGHT_STATE_DIR": "/tmp/state",
            "SSH_AUTH_SOCK": "/tmp/ssh.sock",
            "CUSTOM_AGENT_SOCK": "/tmp/agent.sock",
            "UNRELATED_SECRET": "drop",
        }
    )
    assert_true("path kept", env.get("PATH") == "/bin")
    assert_true("goalflight kept", env.get("GOALFLIGHT_STATE_DIR") == "/tmp/state")
    assert_true("ssh sock stripped", "SSH_AUTH_SOCK" not in env)
    assert_true("agent sock stripped", "CUSTOM_AGENT_SOCK" not in env)
    assert_true("unrelated dropped", "UNRELATED_SECRET" not in env)


def test_launch_helper_warn_refuses_duplicate_without_recovery_evidence() -> None:
    dispatch_id = "acp-refuse-existing"
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        status_json = state_dir / "dispatches" / dispatch_id / "status.json"
        status_json.parent.mkdir(parents=True, exist_ok=True)
        status_json.write_text(
            json.dumps(
                {
                    "schema": "goalflight.acp-run.v1",
                    "seq": 3,
                    "dispatch_id": dispatch_id,
                    "state": "running",
                    "worker_pid": 12345,
                    "worker_identity": {
                        "pid": 12345,
                        "lstart": "Thu Jun 11 12:00:00 2026",
                        "comm": "python3",
                    },
                }
            )
        )
        args = Args(
            repo_root=str(ROOT),
            state_dir=str(state_dir),
            dispatch_id=dispatch_id,
            node_id="localhost",
            agent="codex-acp",
            prompt_b64=base64.b64encode(b"colliding prompt").decode("ascii"),
            cwd=str(ROOT),
            status_json=str(status_json),
            read_only=False,
            recover_unconfirmed=False,
            base_sha=BASE_SHA,
        )
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = fleet_launch._launch(args)
        assert_true("warn refuse", code == 17)
        assert_true("teaching message", "WARN-REFUSE duplicate dispatch-id" in stderr.getvalue())
        assert_true("prompt not written", not (status_json.parent / "prompt.md").exists())


def test_launch_helper_recovers_existing_status_with_unconfirmed_evidence() -> None:
    dispatch_id = "acp-reuse-existing"
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        status_json = state_dir / "dispatches" / dispatch_id / "status.json"
        status_json.parent.mkdir(parents=True, exist_ok=True)
        status_json.write_text(
            json.dumps(
                {
                    "schema": "goalflight.acp-run.v1",
                    "seq": 3,
                    "dispatch_id": dispatch_id,
                    "state": "running",
                    "worker_pid": 12345,
                    "worker_identity": {
                        "pid": 12345,
                        "lstart": "Thu Jun 11 12:00:00 2026",
                        "comm": "python3",
                    },
                    "updated_at": "2026-06-11T12:00:00+00:00",
                }
            )
        )
        args = Args(
            repo_root=str(ROOT),
            state_dir=str(state_dir),
            dispatch_id=dispatch_id,
            node_id="localhost",
            agent="codex-acp",
            prompt_b64=base64.b64encode(b"do not write this prompt").decode("ascii"),
            cwd=str(ROOT),
            status_json=str(status_json),
            read_only=False,
            recover_unconfirmed=True,
            base_sha=BASE_SHA,
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = fleet_launch._launch(args)
        receipt = json.loads(stdout.getvalue())
        assert_true("launch helper ok", code == 0)
        assert_true("reused", receipt.get("reused") is True)
        assert_true("recovered", receipt.get("recovered") is True)
        assert_true("reuse source", receipt.get("reuse_source") == "status_json")
        assert_true("existing pid", receipt.get("remote_pid") == 12345)
        assert_true("base sha echoed", receipt.get("worktree_base_sha") == BASE_SHA)
        assert_true("prompt not written", not (status_json.parent / "prompt.md").exists())


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
        return receipt_runner(argv)

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
            base_sha=BASE_SHA,
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
        return receipt_runner(argv)

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
            base_sha=BASE_SHA,
        )
        fleet_dispatch.execute_dispatch(fleet_dir, preview, runner=capture_runner)
        assert_true("remote commands", len(captured) >= 1)
        first = captured[0]
        assert_true("identity flag", "-i" in first)
        identity_idx = first.index("-i")
        assert_true("identity path", first[identity_idx + 1].endswith("/.ssh/fleet_key"))
        assert_true("user host target", "runner@remote.example" in first)


def test_async_launch_receipt_persisted_and_locks_remain() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        preview = fleet_dispatch.preview_dispatch(
            fleet_dir,
            node_id="localhost",
            agent="codex-acp",
            billing_account="openai/default",
            prompt="chunk.md",
            dispatch_id="acp-async-launch",
            base_sha=BASE_SHA,
        )
        result = fleet_dispatch.execute_dispatch(
            fleet_dir,
            preview,
            runner=receipt_runner,
            stub_terminal=False,
        )
        assert_true("ok", result["ok"] is True)
        assert_true("finalize skipped", result.get("finalize") is None)
        assert_true("receipt pid", result.get("launch_receipt", {}).get("remote_pid") == 4242)
        lock = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("lock remains active", lock is not None and lock.get("state") == "active")
        meta = json.loads(
            (fleet_dir / "register" / "dispatches" / "acp-async-launch" / "meta.json").read_text()
        )
        assert_true("remote status path", meta.get("remote_status_path"))
        assert_true("receipt persisted", meta.get("launch_receipt", {}).get("remote_pid") == 4242)
        assert_true("lease superseded", meta.get("remote_lease_id_superseded_by") == "launch_receipt")
        assert_true("lease active", meta.get("lease_active") is True)


def test_ledger_launch_receipt_roundtrip() -> None:
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
            base_sha=BASE_SHA,
        )
        chain = fleet_dispatch.LockChainResult(
            launch_receipt={"dispatch_id": "acp-ledger-lease", "remote_pid": 4242},
            acquired=["account"],
        )
        info = fleet_dispatch.record_dispatch_ledger(preview, chain)
        record = json.loads(Path(info["path"]).read_text())
        assert_true("remote_lease_id retired", record.get("remote_lease_id") is None)
        assert_true("receipt", record.get("remote_launch_receipt", {}).get("remote_pid") == 4242)


def main() -> None:
    test_explicit_dry_run_preview()
    test_red_auth_blocks_exec()
    test_exec_without_live_ssh_env_refuses_before_runner()
    test_exec_with_live_ssh_env_uses_runner()
    test_preview_ignores_live_ssh_env()
    test_thin_defaults_shows_billing_banner()
    test_lock_chain_rollback_on_worktree_failure()
    test_remote_failure_surfaces_ssh_details()
    test_redact_argv_masks_prompt_values_everywhere()
    test_pending_row_written_before_remote_mutation()
    test_base_sha_absent_fails_pre_launch_and_rolls_back()
    test_dispatch_omitted_base_sha_teaches()
    test_quarantine_blocks_dispatch()
    test_launch_unconfirmed_keeps_account_lock()
    test_preview_passes_recovery_flag_only_with_unconfirmed_meta()
    test_launch_unconfirmed_retry_reuses_same_account_lock()
    test_launch_helper_sanitizes_worker_env()
    test_launch_helper_warn_refuses_duplicate_without_recovery_evidence()
    test_launch_helper_recovers_existing_status_with_unconfirmed_evidence()
    test_resolve_dispatch_runner_stub_and_live()
    test_exec_without_stub_uses_runner()
    test_exec_runner_uses_node_ssh_identity()
    test_stub_e2e_terminal_clears_locks()
    test_async_launch_receipt_persisted_and_locks_remain()
    test_ledger_launch_receipt_roundtrip()
    print("OK: fleet dispatch tests pass")


if __name__ == "__main__":
    main()
