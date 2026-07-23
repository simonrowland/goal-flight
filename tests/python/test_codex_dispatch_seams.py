#!/usr/bin/env python3
"""Focused regressions for late-bound per-dispatch worker homes."""

from __future__ import annotations

import argparse
import asyncio
import builtins
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
os.environ["GOALFLIGHT_ACP_PYTHON"] = str(ROOT / ".missing-acp-test-python")

import goalflight_acp_run as A  # noqa: E402
import goalflight_dispatch as D  # noqa: E402
import goalflight_ledger as L  # noqa: E402
import goalflight_watch as W  # noqa: E402


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="per-dispatch worker homes are local POSIX-only",
)


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE_DIR", str(tmp_path / "task-store"))
    monkeypatch.setenv("GOALFLIGHT_CODEX_STATE_DIR", str(tmp_path / "codex-state"))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", "/dev/null")
    monkeypatch.setenv("GOAL_FLIGHT_PIDFILE_DIR", str(tmp_path / "pids"))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_WAIT_S", "0")
    monkeypatch.setenv("GOALFLIGHT_DISABLE_NUDGES", "1")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("GOALFLIGHT_CODEX_CONTEXT_MODE", raising=False)
    monkeypatch.setattr(D, "_CODEX_SEAT_API_CACHE", D._CODEX_SEAT_API_UNSET)


def _seat_api(
    *,
    resolved: tuple[str | None, str | None],
    cleanups: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        resolve_codex_seat=lambda *_args: resolved,
        cleanup_dispatch_home=lambda dispatch_id: (
            cleanups.append(dispatch_id) if cleanups is not None else None
        ),
    )


def _stub_bash_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    resolved: tuple[str | None, str | None],
    account: str | None = None,
    api_missing: bool = False,
    failure_phase: str | None = None,
    cleanups: list[str] | None = None,
    foreground: bool = False,
) -> tuple[dict, list[dict]]:
    spawn_calls: list[dict] = []
    ledger_calls: list[dict] = []
    ordering: list[str] = []
    resolve_accounts: list[str | None] = []

    def resolve_seat(_project_root, explicit_account, _dispatch_id):
        ordering.append("resolve")
        resolve_accounts.append(explicit_account)
        return resolved

    if api_missing:
        monkeypatch.setattr(D, "_codex_seat_api", lambda: None)
    else:
        monkeypatch.setattr(
            D,
            "_codex_seat_api",
            lambda: SimpleNamespace(
                resolve_codex_seat=resolve_seat,
                cleanup_dispatch_home=lambda dispatch_id: (
                    cleanups.append(dispatch_id)
                    if cleanups is not None
                    else None
                ),
            ),
        )

    def allow_capacity(*_args, **_kwargs):
        ordering.append("capacity")
        return "lease-test"

    monkeypatch.setattr(D, "_acquire_capacity", allow_capacity)
    def record_ledger(*_args, **kwargs):
        ordering.append(f"ledger:{kwargs['state']}")
        ledger_calls.append(dict(kwargs))
        if failure_phase == "pre_spawn" and kwargs["state"] == "starting":
            raise RuntimeError("pre-spawn failure")

    monkeypatch.setattr(D, "_record_ledger", record_ledger)
    monkeypatch.setattr(D, "_reap_quota_stuck_before_bash_launch", lambda: None)
    monkeypatch.setattr(D, "_mark_queue_claim_launch_started", lambda _args: None)
    monkeypatch.setattr(D, "_mark_queue_claim_worker_spawn_intent", lambda _args: None)
    monkeypatch.setattr(
        D, "_mark_queue_claim_worker_spawned", lambda _args, _pid: None
    )
    monkeypatch.setattr(D, "_process_identity_after_spawn", lambda pid: {"pid": pid})
    monkeypatch.setattr(D, "process_group_id", lambda pid: pid)
    monkeypatch.setattr(D, "_start_caffeinate", lambda *_args, **_kwargs: (None, None))
    monkeypatch.setattr(D, "_attach_worker_to_lease", lambda *_args: None)
    monkeypatch.setattr(D, "_detach_lease_to_worker", lambda *_args: None)
    monkeypatch.setattr(D, "_write_pidfile", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        D,
        "_wait_for_detached_watcher",
        lambda **_kwargs: (
            0,
            {"state": "complete", "worker_alive": False},
            "complete",
        ),
    )
    monkeypatch.setattr(D, "_export_dashboard_status_for_project", lambda *_args: None)
    monkeypatch.setattr(D, "_upsert_project_registry_for_dispatch", lambda *_args: None)
    monkeypatch.setattr(D, "_start_dashboard_refresh_for_project", lambda *_args: None)

    def fake_spawn(argv, **kwargs):
        ordering.append(f"spawn:{kwargs.get('label')}")
        if failure_phase == "spawn" and kwargs.get("label") == "worker":
            raise RuntimeError("spawn failure")
        spawn_calls.append(
            {
                "argv": list(argv),
                "env": dict(kwargs.get("env") or {}),
                "label": kwargs.get("label"),
            }
        )
        return 41000 + len(spawn_calls)

    monkeypatch.setattr(D, "_spawn_daemonized_process", fake_spawn)
    argv = [
        "--agent",
        "codex",
        "--dispatch-id",
        "bash-seat-seam",
        "--cwd",
        str(tmp_path),
        "--tail",
        str(tmp_path / "bash.tail"),
        "--status-json",
        str(tmp_path / "bash.status.json"),
    ]
    if foreground:
        argv.append("--foreground")
    if account is not None:
        canonical_home = Path.home() / ".goal-flight" / "accounts" / account / "codex"
        canonical_home.mkdir(parents=True)
        argv.extend(["--account", account])
    argv.extend(["--", sys.executable, "-c", "pass"])
    rc = D.main(argv)
    assert rc == (1 if failure_phase else 0)
    assert resolve_accounts == ([] if api_missing else [account])
    if not api_missing:
        assert ordering.index("capacity") < ordering.index("resolve")
        assert ordering.index("resolve") < ordering.index("ledger:starting")
    else:
        assert ordering.index("capacity") < ordering.index("ledger:starting")
    if failure_phase != "pre_spawn":
        assert ordering.index("ledger:starting") < ordering.index("spawn:worker")
    worker_spawn = next(
        (call for call in spawn_calls if call["label"] == "worker"),
        {},
    )
    return worker_spawn, ledger_calls


def test_bash_pin_is_applied_after_capacity_and_reaches_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "dispatch-home"
    home.mkdir()
    worker_spawn, ledger_calls = _stub_bash_launch(
        monkeypatch,
        tmp_path,
        resolved=(str(home), "seat-a"),
        account="explicit-seat",
    )
    assert worker_spawn["env"]["CODEX_HOME"] == str(home)
    assert [
        call.get("effective_account")
        for call in ledger_calls
        if call["state"] in {"starting", "running"}
    ] == ["seat-a", "seat-a"]


def test_bash_resolve_none_preserves_inherited_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    worker_spawn, ledger_calls = _stub_bash_launch(
        monkeypatch,
        tmp_path,
        resolved=(None, None),
    )
    assert "CODEX_HOME" not in worker_spawn["env"]
    assert all(call.get("effective_account") is None for call in ledger_calls)


def test_bash_ext_absent_preserves_spawn_and_stays_quiet(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    worker_spawn, ledger_calls = _stub_bash_launch(
        monkeypatch,
        tmp_path,
        resolved=(None, None),
        api_missing=True,
    )
    assert worker_spawn["argv"] == [sys.executable, "-c", "pass"]
    assert "CODEX_HOME" not in worker_spawn["env"]
    assert all(call.get("effective_account") is None for call in ledger_calls)
    assert "per-dispatch home" not in capsys.readouterr().err


@pytest.mark.parametrize("failure_phase", ["pre_spawn", "spawn"])
def test_bash_failed_launch_cleanup_is_launcher_owned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_phase: str,
) -> None:
    home = tmp_path / f"{failure_phase}-dispatch-home"
    home.mkdir()
    cleanups: list[str] = []
    _stub_bash_launch(
        monkeypatch,
        tmp_path,
        resolved=(str(home), "seat-failure"),
        failure_phase=failure_phase,
        cleanups=cleanups,
    )
    assert cleanups == ["bash-seat-seam"]


def test_bash_foreground_cleanup_is_launcher_owned_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "foreground-dispatch-home"
    home.mkdir()
    cleanups: list[str] = []
    _stub_bash_launch(
        monkeypatch,
        tmp_path,
        resolved=(str(home), "seat-foreground"),
        cleanups=cleanups,
        foreground=True,
    )
    assert cleanups == ["bash-seat-seam"]


def test_guarded_import_failure_preserves_today_behavior(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original_import = builtins.__import__
    attempts = {"count": 0}

    def failing_import(name, *args, **kwargs):
        if name == "ext" or name.startswith("ext."):
            attempts["count"] += 1
            raise RuntimeError("optional module absent")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)
    assert D._codex_seat_api() is None
    assert D._codex_seat_api() is None
    assert D.resolve_codex_home(tmp_path, None, "import-fallback") == (None, None)
    assert attempts["count"] == 1


def _ledger_args(dispatch_id: str) -> argparse.Namespace:
    return argparse.Namespace(
        dispatch_id=dispatch_id,
        prompt_id=None,
        prompt_path=None,
        task_ids=[],
        agent="codex",
        engine="codex",
        shape="bash",
        account="default",
        effective_account=None,
        transport="dispatch",
        project_root=str(ROOT),
        controller_pid=os.getpid(),
        worker_pid=None,
        acp_session_id=None,
        logical_session_id=dispatch_id,
        lease_id=None,
        stdout_path=None,
        stderr_path=None,
        status_path=None,
        os_sandbox_json=None,
        queue_launch_token=None,
        detached=False,
        state="starting",
        json=True,
    )


def test_ledger_persists_and_surfaces_effective_account_only_when_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(D, "_export_dashboard_status_for_project", lambda *_args: None)
    monkeypatch.setattr(D, "_upsert_project_registry_for_dispatch", lambda *_args: None)
    monkeypatch.setattr(D, "_start_dashboard_refresh_for_project", lambda *_args: None)
    args = SimpleNamespace(
        dispatch_id="bash-ledger-pinned",
        task_ids=[],
        agent="codex",
        shape="bash",
        account=None,
        read_only=False,
        os_sandbox=None,
        controller_pid=None,
        queue_launch_token=None,
        launch_detached=False,
    )
    D._record_ledger(
        args,
        project_root=ROOT,
        prompt_path=None,
        status_json=Path("/dev/null"),
        tail=Path("/dev/null"),
        lease_id=None,
        worker_pid=None,
        state="starting",
        effective_account="seat-b",
    )
    pinned = json.loads(L.record_path(args.dispatch_id).read_text(encoding="utf-8"))
    assert pinned["effective_account"] == "seat-b"
    row = next(
        row
        for row in L.status_payload()["records"]
        if row["dispatch_id"] == args.dispatch_id
    )
    assert row["account"] == "default"
    assert row["effective_account"] == "seat-b"

    unpinned_args = _ledger_args("ledger-unpinned")
    L.cmd_record(unpinned_args)
    unpinned = json.loads(
        L.record_path(unpinned_args.dispatch_id).read_text(encoding="utf-8")
    )
    assert "effective_account" not in unpinned


def _acp_cfg(
    tmp_path: Path,
    *,
    account: str | None,
    agent: str = "codex-acp",
) -> argparse.Namespace:
    return A.normalized_acp_dispatch_cfg(
        SimpleNamespace(
            agent=agent,
            model=None,
            install_slot=None,
            account=account,
            cwd=str(tmp_path),
            worktree="off",
            session_id=None,
            dispatch_id=f"acp-seat-{agent}-{account or 'fallback'}",
            task_ids=[],
            priority="normal",
            capacity_wait_s=0.0,
            prompt_id=None,
            prompt=None,
            prompt_text="Do nothing.",
            prompt_b64=None,
            original_prompt_file=None,
            mode="one-shot",
            idle_timeout=5.0,
            status_json=str(tmp_path / f"acp-{account or 'fallback'}.status.json"),
            steer_file=str(tmp_path / f"acp-{account or 'fallback'}.steer.jsonl"),
            context_mode="disabled",
            os_sandbox=A.OS_SANDBOX_OFF,
            permission_mode="auto",
            permission_dir=None,
            permission_inline_timeout_s=None,
            permission_user_timeout_s=None,
            permission_allow_tool_title_pattern=[],
            read_only=False,
            interactive=False,
            heartbeat_interval=0.05,
            wedge_samples=1,
            max_tool_s=5.0,
            max_consecutive_tool_errors=5,
            max_acp_events=100,
            max_quiet_s=2.0,
            progress_stall_s=2.0,
            stall_kill=False,
            liveness_profile="local_compute",
            remote_turn_silence_s=None,
            remote_turn_cancel_grace_s=1.0,
            cpu_epsilon=0.1,
            json=True,
        )
    )


def _run_acp_to_spawn_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    resolved: tuple[str | None, str | None],
    account: str | None,
    agent: str = "codex-acp",
    block_dispatch_import: bool = False,
    spawn_base_env: dict[str, str] | None = None,
    request_envelope: dict | None = None,
    dispatch_import_attempts: list[str] | None = None,
) -> tuple[argparse.Namespace, dict[str, str], list[str]]:
    cleanups: list[str] = []
    ordering: list[str] = []
    resolve_accounts: list[str | None] = []

    def resolve_seat(_project_root, explicit_account, _dispatch_id):
        ordering.append("resolve")
        resolve_accounts.append(explicit_account)
        return resolved

    monkeypatch.setattr(
        D,
        "_codex_seat_api",
        lambda: SimpleNamespace(
            resolve_codex_seat=resolve_seat,
            cleanup_dispatch_home=lambda dispatch_id: cleanups.append(dispatch_id),
        ),
    )
    monkeypatch.setattr(A, "agent_command", lambda *_args, **_kwargs: ("fake", []))
    monkeypatch.setattr(
        A, "_codex_workspace_write_acp_args", lambda _agent, args, **_kwargs: args
    )
    monkeypatch.setattr(
        A,
        "_worker_spawn_env",
        lambda *_args: dict(spawn_base_env or {"BASE": "captured"}),
    )
    monkeypatch.setattr(A, "validate_acp_dispatch_readiness", lambda *_args: None)
    monkeypatch.setattr(A, "validate_os_sandbox_request", lambda *_args: None)
    monkeypatch.setattr(A, "preflight_os_sandbox", lambda *_args: None)
    monkeypatch.setattr(A, "cleanup_ghosts", lambda: 0)
    if block_dispatch_import:
        original_import = builtins.__import__

        def fail_dispatch_import(name, *args, **kwargs):
            if name == "goalflight_dispatch":
                if dispatch_import_attempts is not None:
                    dispatch_import_attempts.append(name)
                raise RuntimeError("dispatcher integration unavailable")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fail_dispatch_import)

    async def allow_capacity(*_args, **_kwargs):
        ordering.append("capacity")
        return {"decision": "allow", "lease": {}}

    monkeypatch.setattr(A.goalflight_capacity, "acquire_with_wait_async", allow_capacity)
    original_cmd_record = L.cmd_record

    def record_with_order(args):
        ordering.append(f"ledger:{args.state}")
        return original_cmd_record(args)

    monkeypatch.setattr(L, "cmd_record", record_with_order)
    captured: dict[str, str] = {}

    async def fail_after_env(*_args, **kwargs):
        ordering.append("spawn")
        captured.update(kwargs["env"])
        raise RuntimeError("stop after spawn env capture")

    monkeypatch.setattr(A, "spawn_and_handshake_with_retry", fail_after_env)
    cfg = _acp_cfg(tmp_path, account=account, agent=agent)
    cfg.request_envelope = request_envelope
    payload = asyncio.run(A.run_acp_dispatch(cfg))
    assert payload["state"] == "failed"
    expect_resolve = (
        L.infer_engine(agent) == "codex" and not block_dispatch_import
    )
    if expect_resolve:
        assert ordering.index("capacity") < ordering.index("resolve")
        assert ordering.index("resolve") < ordering.index("ledger:starting")
    else:
        assert "resolve" not in ordering
        assert ordering.index("capacity") < ordering.index("ledger:starting")
    assert ordering.index("ledger:starting") < ordering.index("spawn")
    assert resolve_accounts == ([account] if expect_resolve else [])
    return cfg, captured, cleanups


def test_acp_rebuilds_spawn_env_and_writes_truthful_accounts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "acp-dispatch-home"
    home.mkdir()
    (home / "config.toml").write_text(
        "[mcp_servers.context-mode]\nenabled = true\n",
        encoding="utf-8",
    )
    request_envelope = {
        "dispatch_argv": ["--agent", "codex", "--dispatch-id", "queued-acp"],
        "request": {"agent": "codex", "dispatch_id": "queued-acp"},
    }
    cfg, captured, cleanups = _run_acp_to_spawn_failure(
        monkeypatch,
        tmp_path,
        resolved=(str(home), "seat-c"),
        account="explicit-seat",
        request_envelope=request_envelope,
    )
    assert captured == {"BASE": "captured", "CODEX_HOME": str(home)}
    record = json.loads(L.record_path(cfg.dispatch_id).read_text(encoding="utf-8"))
    assert record["account"] == "explicit-seat"
    assert record["effective_account"] == "seat-c"
    assert record["request_envelope"] == request_envelope
    assert cleanups == [cfg.dispatch_id]


def test_acp_resolve_none_does_not_override_or_write_effective_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg, captured, cleanups = _run_acp_to_spawn_failure(
        monkeypatch,
        tmp_path,
        resolved=(None, None),
        account=None,
    )
    assert captured == {"BASE": "captured"}
    record = json.loads(L.record_path(cfg.dispatch_id).read_text(encoding="utf-8"))
    assert record["account"] == "default"
    assert "effective_account" not in record
    assert cleanups == []
    assert cfg.context_mode == "enabled"


def test_acp_dispatcher_import_failure_keeps_context_guard_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bare = tmp_path / "bare-codex-home"
    bare.mkdir()
    (bare / "config.toml").write_text("[features]\nmemories = false\n")
    import_attempts: list[str] = []
    cfg, captured, cleanups = _run_acp_to_spawn_failure(
        monkeypatch,
        tmp_path,
        resolved=(None, None),
        account=None,
        block_dispatch_import=True,
        spawn_base_env={"BASE": "captured", "CODEX_HOME": str(bare)},
        dispatch_import_attempts=import_attempts,
    )
    assert captured["CODEX_HOME"] == str(bare)
    assert cfg.context_mode == "enabled"
    assert cleanups == []
    assert import_attempts == ["goalflight_dispatch"]


def test_non_codex_acp_skips_dispatcher_integration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import_attempts: list[str] = []
    cfg, captured, cleanups = _run_acp_to_spawn_failure(
        monkeypatch,
        tmp_path,
        resolved=(str(tmp_path / "unused"), "unused-seat"),
        account=None,
        agent="grok-acp",
        block_dispatch_import=True,
        dispatch_import_attempts=import_attempts,
    )
    assert captured == {"BASE": "captured"}
    assert cfg.context_mode == "disabled"
    assert cleanups == []
    assert import_attempts == []


def test_context_mode_disable_requires_effective_home_table(tmp_path: Path) -> None:
    argv = ["codex", "exec", "-c", D._CONTEXT_MODE_DISABLE_KEY, "-"]
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "config.toml").write_text("[features]\nmemories = false\n")
    assert D._CONTEXT_MODE_DISABLE_KEY not in D._guard_codex_context_mode_disable(
        argv, {"CODEX_HOME": str(bare)}
    )

    parity = tmp_path / "parity"
    parity.mkdir()
    (parity / "config.toml").write_text(
        "[mcp_servers.context-mode]\nenabled = true\n"
    )
    assert D._guard_codex_context_mode_disable(
        argv, {"CODEX_HOME": str(parity)}
    ) == argv


def test_watcher_cleanup_is_detached_and_resolved_home_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanups: list[str] = []
    monkeypatch.setattr(
        D,
        "_codex_seat_api",
        lambda: _seat_api(resolved=(None, None), cleanups=cleanups),
    )
    assert (
        W._finish_existing_ledger(
            "watcher-cleanup",
            "complete",
            "test",
            worker_still_alive=False,
            agent="codex",
            detached=True,
            codex_dispatch_home_resolved=True,
        )
        is None
    )
    assert cleanups == ["watcher-cleanup"]
    W._finish_existing_ledger(
        "foreground-no-cleanup",
        "complete",
        "test",
        worker_still_alive=False,
        agent="codex",
        detached=False,
        codex_dispatch_home_resolved=True,
    )
    W._finish_existing_ledger(
        "unresolved-no-cleanup",
        "complete",
        "test",
        worker_still_alive=False,
        agent="codex",
        detached=True,
        codex_dispatch_home_resolved=False,
    )
    W._finish_existing_ledger(
        "live-worker-no-cleanup",
        "blocked",
        "test",
        worker_still_alive=True,
        agent="codex",
        detached=True,
        codex_dispatch_home_resolved=True,
    )
    W._finish_existing_ledger(
        "non-codex-no-cleanup",
        "complete",
        "test",
        worker_still_alive=False,
        agent="grok-acp",
        detached=True,
        codex_dispatch_home_resolved=True,
    )
    assert cleanups == ["watcher-cleanup"]


def _wait_for_terminal_record(dispatch_id: str, *, timeout_s: float = 10.0) -> dict:
    deadline = time.time() + timeout_s
    last: dict = {}
    while time.time() < deadline:
        path = L.record_path(dispatch_id, create=False)
        if path.exists():
            last = json.loads(path.read_text(encoding="utf-8"))
            if D._dispatch_record_is_terminal(last):
                return last
        time.sleep(0.05)
    raise AssertionError(f"dispatch did not terminalize: {last}")


def _capture_test_task(tmp_path: Path) -> str:
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "goalflight_task.py"),
            "capture",
            "Queued requeue lifecycle regression",
            "--json",
        ],
        cwd=tmp_path,
        env=os.environ.copy(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    return str(json.loads(proc.stdout)["id"])


def _install_stub_seat_api(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    home: Path,
) -> None:
    package = tmp_path / "stub-python" / "ext"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "codex_seat_lib.py").write_text(
        "import os\n"
        "def resolve_codex_seat(project_root, explicit_account, dispatch_id):\n"
        "    return os.environ['GOALFLIGHT_TEST_CODEX_HOME'], 'seat-e2e'\n"
        "def cleanup_dispatch_home(dispatch_id):\n"
        "    return None\n",
        encoding="utf-8",
    )
    old_pythonpath = os.environ.get("PYTHONPATH")
    entries = [str(package.parent)]
    if old_pythonpath:
        entries.append(old_pythonpath)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(entries))
    monkeypatch.setenv("GOALFLIGHT_TEST_CODEX_HOME", str(home))


@pytest.mark.parametrize(
    ("failure_kind", "worker_text"),
    [
        ("auth", "HTTP 401 Unauthorized"),
        ("quota", "quota exceeded"),
    ],
)
def test_queued_terminal_reconcile_requeues_exactly_once_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_kind: str,
    worker_text: str,
) -> None:
    dispatch_id = f"queued-{failure_kind}-parent"
    home = tmp_path / "resolved-home"
    home.mkdir()
    (home / "config.toml").write_text(
        "[mcp_servers.context-mode]\nenabled = true\n",
        encoding="utf-8",
    )
    _install_stub_seat_api(monkeypatch, tmp_path, home)
    task_id = _capture_test_task(tmp_path)
    tail = tmp_path / f"{dispatch_id}.tail"
    status = tmp_path / f"{dispatch_id}.status.json"
    worker_code = (
        f"import sys; print({worker_text!r}, flush=True); sys.exit(1)"
    )
    rc = D.main(
        [
            "--agent",
            "codex",
            "--shape",
            "bash",
            "--dispatch-id",
            dispatch_id,
            "--cwd",
            str(tmp_path),
            "--task",
            task_id,
            "--tail",
            str(tail),
            "--status-json",
            str(status),
            "--poll-secs",
            "0.05",
            "--max-idle-secs",
            "1",
            "--submit",
            "--no-drain-on-submit",
            "--",
            sys.executable,
            "-c",
            worker_code,
        ]
    )
    assert rc == 0
    queue_dir = D._dispatch_queue_dir()
    first = D._drain_queue_once(
        argparse.Namespace(
            queue_dir=str(queue_dir),
            remote_node=None,
            capacity_wait_s=0.0,
            claim_stale_s=D.QUEUE_CLAIM_STALE_S,
            limit=1,
        )
    )
    assert first["launched"] == 1, first
    assert not list(queue_dir.glob(f"{dispatch_id}.json.claimed-*"))

    terminal = _wait_for_terminal_record(dispatch_id)
    assert terminal["effective_account"] == "seat-e2e"
    assert terminal["request_envelope"]["dispatch_argv"]
    if failure_kind == "quota":
        assert terminal["state"] == "rate_limited"
    else:
        assert D._requeue_failure_kind(terminal, tail) == "auth"

    D._recover_claimed_queue_entries(queue_dir, stale_s=0.0)
    parent = json.loads(
        L.record_path(dispatch_id).read_text(encoding="utf-8")
    )
    child_id = parent["requeue"]["child_id"]
    child_paths = list(queue_dir.glob("*.json"))
    assert len(child_paths) == 1
    child = json.loads(child_paths[0].read_text(encoding="utf-8"))
    assert child["dispatch_id"] == child_id
    assert child["requeued_from"] == dispatch_id
    assert child["request"]["requeued_from"] == dispatch_id

    D._recover_claimed_queue_entries(queue_dir, stale_s=0.0)
    assert [path.name for path in queue_dir.glob("*.json")] == [
        D._queue_entry_path(child_id, queue_dir=queue_dir).name
    ]


def test_ledger_only_nonterminal_reconcile_uses_durable_request_envelope(
    tmp_path: Path,
) -> None:
    dispatch_id = "ledger-only-auth-parent"
    task_id = _capture_test_task(tmp_path)
    entry, queue_dir, tail = _claimed_entry(tmp_path, dispatch_id)
    entry["task_ids"] = [task_id]
    entry["request"]["task_ids"] = [task_id]
    dead_pid = 99_999_991
    L.write_record(
        {
            "schema": L.SCHEMA,
            "dispatch_id": dispatch_id,
            "agent": "codex",
            "engine": "codex",
            "shape": "bash",
            "account": "default",
            "effective_account": "seat-ledger-only",
            "transport": "dispatch",
            "project_root": str(tmp_path),
            "state": "running",
            "terminal_state": "unknown",
            "worker_pid": dead_pid,
            "worker_identity": {"pid": dead_pid},
            "worker_pgid": dead_pid,
            "task_ids": [task_id],
            "stdout_path": str(tail),
            "status_path": entry["request"]["status_json"],
            "started_at": L.utc_now(),
            "request_envelope": entry,
        }
    )
    stats = D._recover_claimed_queue_entries(queue_dir, stale_s=0.0)
    assert stats["ledger_terminalized"] == 1
    parent = json.loads(
        L.record_path(dispatch_id).read_text(encoding="utf-8")
    )
    child_id = parent["requeue"]["child_id"]
    child_path = D._queue_entry_path(child_id, queue_dir=queue_dir)
    assert child_path.exists()
    assert json.loads(child_path.read_text(encoding="utf-8"))[
        "requeued_from"
    ] == dispatch_id


def _claimed_entry(
    tmp_path: Path,
    dispatch_id: str,
    *,
    requeued_from: str | None = None,
) -> tuple[dict, Path, Path]:
    queue_dir = D._dispatch_queue_dir()
    queue_dir.mkdir(parents=True, exist_ok=True)
    tail = tmp_path / f"{dispatch_id}.tail"
    tail.write_text("Error: HTTP 401 Unauthorized\n", encoding="utf-8")
    request = {
        "agent": "codex",
        "cwd": str(tmp_path),
        "dispatch_id": dispatch_id,
        "tail": str(tail),
        "status_json": str(tmp_path / f"{dispatch_id}.status.json"),
    }
    if requeued_from:
        request["requeued_from"] = requeued_from
    entry = {
        "schema": D.DISPATCH_QUEUE_SCHEMA,
        "state": "claimed",
        "dispatch_id": dispatch_id,
        "agent": "codex",
        "shape": "bash",
        "project_root": str(tmp_path),
        "process_cwd": str(tmp_path),
        "created_at": L.utc_now(),
        "updated_at": L.utc_now(),
        "dispatch_argv": [
            "--agent",
            "codex",
            "--dispatch-id",
            dispatch_id,
            "--tail",
            str(tail),
            "--status-json",
            str(tmp_path / f"{dispatch_id}.status.json"),
            "--cwd",
            str(tmp_path),
            "--",
            sys.executable,
            "-c",
            "pass",
        ],
        "request": request,
    }
    if requeued_from:
        entry["requeued_from"] = requeued_from
    return entry, queue_dir, tail


def _terminal_record(
    dispatch_id: str,
    *,
    state: str = "worker_dead",
    effective_account: str = "seat-r",
) -> None:
    L.write_record(
        {
            "schema": L.SCHEMA,
            "dispatch_id": dispatch_id,
            "agent": "codex",
            "engine": "codex",
            "shape": "bash",
            "account": "default",
            "effective_account": effective_account,
            "transport": "dispatch",
            "project_root": str(ROOT),
            "state": state,
            "terminal_state": state,
            "started_at": L.utc_now(),
        }
    )


def _txn() -> SimpleNamespace:
    return SimpleNamespace(queue_locked=True, ledger_locked=True)


@pytest.mark.parametrize("crash_phase", ["before_intent", "after_intent", "after_child"])
def test_requeue_crash_interleavings_never_duplicate_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    crash_phase: str,
) -> None:
    dispatch_id = f"requeue-{crash_phase}"
    entry, queue_dir, tail = _claimed_entry(tmp_path, dispatch_id)
    _terminal_record(dispatch_id)
    original_record_write = L.write_record
    original_child_write = D._write_json_exclusive

    if crash_phase == "before_intent":
        calls = {"count": 0}

        def fail_first_record(record):
            calls["count"] += 1
            if calls["count"] == 1 and record.get("requeue"):
                raise OSError("crash before intent commit")
            return original_record_write(record)

        monkeypatch.setattr(L, "write_record", fail_first_record)
    elif crash_phase == "after_intent":
        monkeypatch.setattr(
            D,
            "_write_json_exclusive",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("crash after intent")
            ),
        )
    else:
        calls = {"count": 0}

        def write_then_crash(path, payload):
            calls["count"] += 1
            created = original_child_write(path, payload)
            if calls["count"] == 1:
                raise OSError("crash after child")
            return created

        monkeypatch.setattr(D, "_write_json_exclusive", write_then_crash)

    assert not D._maybe_requeue_terminal_claim(
        _txn(), entry, queue_dir=queue_dir, tail=tail
    )
    monkeypatch.setattr(L, "write_record", original_record_write)
    monkeypatch.setattr(D, "_write_json_exclusive", original_child_write)
    assert D._maybe_requeue_terminal_claim(
        _txn(), entry, queue_dir=queue_dir, tail=tail
    )

    old = json.loads(L.record_path(dispatch_id).read_text(encoding="utf-8"))
    child_id = old["requeue"]["child_id"]
    children = list(queue_dir.glob(f"{D._safe_dispatch_filename(child_id)}.json"))
    assert len(children) == 1
    child = json.loads(children[0].read_text(encoding="utf-8"))
    assert child["requeued_from"] == dispatch_id
    assert child["request"]["requeued_from"] == dispatch_id


def test_quota_requeue_carries_cooldown_not_before(tmp_path: Path) -> None:
    dispatch_id = "requeue-quota"
    entry, queue_dir, tail = _claimed_entry(tmp_path, dispatch_id)
    _terminal_record(dispatch_id, state="rate_limited", effective_account="seat-q")
    state_dir = Path(os.environ["GOALFLIGHT_CODEX_STATE_DIR"])
    state_dir.mkdir(parents=True)
    cooldown = "2030-01-02T03:04:05Z"
    (state_dir / "codex-seat-states.json").write_text(
        json.dumps(
            {
                "version": 1,
                "seats": {"seat-q": {"cooldown_until": cooldown}},
            }
        ),
        encoding="utf-8",
    )
    assert D._maybe_requeue_terminal_claim(
        _txn(), entry, queue_dir=queue_dir, tail=tail
    )
    old = json.loads(L.record_path(dispatch_id).read_text(encoding="utf-8"))
    child_path = D._queue_entry_path(old["requeue"]["child_id"], queue_dir=queue_dir)
    child = json.loads(child_path.read_text(encoding="utf-8"))
    assert child["not_before"] == cooldown
    assert child["request"]["not_before"] == cooldown


def test_future_requeue_not_before_is_left_queued(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir()
    queue_path = queue_dir / "future-requeue.json"
    D._write_json_atomic(
        queue_path,
        {
            "schema": D.DISPATCH_QUEUE_SCHEMA,
            "state": "queued",
            "dispatch_id": "future-requeue",
            "created_at": L.utc_now(),
            "not_before": "2999-01-02T03:04:05Z",
            "request": {"not_before": "2999-01-02T03:04:05Z"},
            "dispatch_argv": ["--agent", "codex"],
        },
    )
    payload = D._drain_queue_once(
        argparse.Namespace(
            queue_dir=str(queue_dir),
            remote_node=None,
            capacity_wait_s=0.0,
            claim_stale_s=D.QUEUE_CLAIM_STALE_S,
            limit=0,
        )
    )
    assert payload["launched"] == 0
    assert payload["left_queued"] == 1
    assert payload["remaining"] == 1
    assert payload["details"] == [
        {
            "dispatch_id": "future-requeue",
            "state": "queued",
            "reason": "not_before",
            "not_before": "2999-01-02T03:04:05Z",
        }
    ]


def test_requeue_child_ledger_evidence_prevents_duplicate(
    tmp_path: Path,
) -> None:
    dispatch_id = "requeue-ledger-evidence"
    child_id = "requeue-ledger-evidence-retry-fixed"
    entry, queue_dir, tail = _claimed_entry(tmp_path, dispatch_id)
    _terminal_record(dispatch_id)
    record = json.loads(L.record_path(dispatch_id).read_text(encoding="utf-8"))
    record["requeue"] = {"child_id": child_id, "requeued_at": L.utc_now()}
    L.write_record(record)
    _terminal_record(child_id, state="complete", effective_account="seat-next")

    assert D._maybe_requeue_terminal_claim(
        _txn(), entry, queue_dir=queue_dir, tail=tail
    )
    assert not D._queue_entry_path(child_id, queue_dir=queue_dir).exists()


def test_success_terminal_with_old_auth_text_is_not_requeued(tmp_path: Path) -> None:
    dispatch_id = "requeue-success-auth-text"
    entry, queue_dir, tail = _claimed_entry(tmp_path, dispatch_id)
    _terminal_record(dispatch_id, state="complete")
    assert D._maybe_requeue_terminal_claim(
        _txn(), entry, queue_dir=queue_dir, tail=tail
    )
    record = json.loads(L.record_path(dispatch_id).read_text(encoding="utf-8"))
    assert "requeue" not in record
    assert list(queue_dir.glob("*.json")) == []


def test_requeued_child_is_never_auto_requeued(tmp_path: Path) -> None:
    dispatch_id = "already-requeued"
    entry, queue_dir, tail = _claimed_entry(
        tmp_path,
        dispatch_id,
        requeued_from="original-dispatch",
    )
    _terminal_record(dispatch_id, state="rate_limited")
    assert D._maybe_requeue_terminal_claim(
        _txn(), entry, queue_dir=queue_dir, tail=tail
    )
    record = json.loads(L.record_path(dispatch_id).read_text(encoding="utf-8"))
    assert "requeue" not in record
    assert list(queue_dir.glob("*.json")) == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
