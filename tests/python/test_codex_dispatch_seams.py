#!/usr/bin/env python3
"""Focused regressions for late-bound per-dispatch worker homes."""

from __future__ import annotations

import argparse
import asyncio
import builtins
from contextlib import nullcontext
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOST_HOME = Path.home()
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_adapter_readiness as R  # noqa: E402
import goalflight_acp_run as A  # noqa: E402
import goalflight_capacity as C  # noqa: E402
import goalflight_dispatch as D  # noqa: E402
import goalflight_ledger as L  # noqa: E402
import goalflight_os_sandbox as S  # noqa: E402
import goalflight_watch as W  # noqa: E402


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="per-dispatch worker homes are local POSIX-only",
)


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(
        "GOALFLIGHT_ACP_PYTHON", str(ROOT / ".missing-acp-test-python")
    )
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv(
        "GOALFLIGHT_DISPATCH_DIR", str(tmp_path / "state" / "dispatch")
    )
    monkeypatch.setenv("GOALFLIGHT_JOURNAL_DIR", str(tmp_path / "journals"))
    monkeypatch.setenv("GOALFLIGHT_MESSAGES_DIR", str(tmp_path / "messages"))
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE_DIR", str(tmp_path / "task-store"))
    monkeypatch.setenv("GOALFLIGHT_CODEX_STATE_DIR", str(tmp_path / "codex-state"))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", "/dev/null")
    monkeypatch.setenv("GOAL_FLIGHT_PIDFILE_DIR", str(tmp_path / "pids"))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_WAIT_S", "0")
    monkeypatch.setenv("GOALFLIGHT_DISABLE_NUDGES", "1")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("GOALFLIGHT_CODEX_CONTEXT_MODE", raising=False)
    monkeypatch.setattr(D, "_CODEX_SEAT_API_CACHE", D._CODEX_SEAT_API_UNSET)


def test_read_only_worker_prompt_guides_writable_repo_copies(tmp_path: Path) -> None:
    prompt_file = tmp_path / "review.md"
    prompt_file.write_text(
        "Review the repository without making changes.\n", encoding="utf-8"
    )
    assembled_path = D._materialize_steer_prompt(
        str(prompt_file),
        tmp_path / "dispatch",
        "grok-research-copy-guidance",
        agent="grok-research",
    )

    assert assembled_path
    assembled = Path(assembled_path).read_text(encoding="utf-8")
    assert (
        'For a writable repo copy, use a local clone (`git clone -q <repo> '
        '"$TMPDIR/<name>"`; it hardlinks objects), or copy files with '
        '`cp -cR <src> "$TMPDIR/<name>"` on APFS and then delete the copied '
        '`.git` because a worktree\'s `.git` points back to the source repo. '
        'Avoid plain `cp -R` and `rsync` of the repo.'
    ) in assembled


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
    agent: str = "codex",
    account: str | None = None,
    api_missing: bool = False,
    failure_phase: str | None = None,
    cleanups: list[str] | None = None,
    foreground: bool = False,
    persist_ledger: bool = True,
    drop_starting_projection: bool = False,
    preset_options: list[str] | None = None,
    expected_refusal: bool = False,
    resolver_error: BaseException | None = None,
    capacity_calls: list[argparse.Namespace] | None = None,
    model: str | None = None,
    dispatch_id: str = "bash-seat-seam",
    from_queue: bool = False,
    recorded_home: str | None = None,
    recorded_account: str | None = None,
    resolver_account_calls: list[str | None] | None = None,
    prompt_file: Path | None = None,
) -> tuple[dict, list[dict]]:
    monkeypatch.delenv("GOALFLIGHT_CONTROLLER_LABEL", raising=False)
    monkeypatch.delenv("GOALFLIGHT_CONTROLLER_SESSION_ID", raising=False)
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_PID", "99999991")
    # Managed resolver fixtures represent a dispatch-time selection; provide
    # the same usage evidence the production selector now requires.
    monkeypatch.setattr(
        D,
        "_codex_usage_probe_says_usable",
        lambda account, **kwargs: True,
    )
    resolved_account = resolved[1]
    monkeypatch.setattr(
        D,
        "_codex_usage_probe_rows",
        lambda *_args, **_kwargs: (
            [
                {
                    "provider": "codex",
                    "account": resolved_account,
                    "remaining": "80%",
                    "reset_at": "2030-01-02T03:04:05Z",
                    "flags": [],
                    "evidence": {
                        "probe": {"state": "reported", "observed_at": time.time()}
                    },
                }
            ]
            if resolved_account
            else []
        ),
    )
    spawn_calls: list[dict] = []
    ledger_calls: list[dict] = []
    ordering: list[str] = []
    resolve_accounts: list[str | None] = []
    real_record_ledger = D._record_ledger

    def resolve_seat(_project_root, explicit_account, _dispatch_id):
        ordering.append("resolve")
        resolve_accounts.append(explicit_account)
        if resolver_account_calls is not None:
            resolver_account_calls.append(explicit_account)
        if resolver_error is not None:
            raise resolver_error
        if resolved[0]:
            Path(resolved[0]).mkdir(parents=True, exist_ok=True)
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
        if capacity_calls is not None:
            capacity_calls.append(_args[0])
        return "lease-test"

    monkeypatch.setattr(D, "_acquire_capacity", allow_capacity)
    # The fake lease is never written to capacity state, so the launcher's
    # "lease still held before spawn" check must be faked alongside it.
    monkeypatch.setattr(D.goalflight_capacity, "mark_lease_spawning", lambda _lease_id: True)
    def record_ledger(*_args, **kwargs):
        ordering.append(f"ledger:{kwargs['state']}")
        ledger_calls.append(dict(kwargs))
        if failure_phase == "pre_spawn" and kwargs["state"] == "starting":
            raise RuntimeError("pre-spawn failure")
        if persist_ledger:
            # P2 prepares the journal attempt in this call; the worker-launch
            # wrapper needs that durable identity before it can claim RUNNING.
            real_record_ledger(*_args, **kwargs)
            if drop_starting_projection and kwargs["state"] == "starting":
                D.goalflight_ledger.record_path(
                    _args[0].dispatch_id,
                    create=False,
                ).unlink()

    if not persist_ledger:
        setattr(record_ledger, "_goalflight_nonpersisting_seam", True)
    monkeypatch.setattr(D, "_record_ledger", record_ledger)
    monkeypatch.setattr(D, "_reap_quota_stuck_before_bash_launch", lambda: None)
    monkeypatch.setattr(D, "_mark_queue_claim_launch_started", lambda _args: None)
    monkeypatch.setattr(
        D,
        "_mark_queue_claim_worker_spawn_intent",
        lambda _args: ordering.append("spawn_intent"),
    )
    real_attempt_peek = D._attempt_claiming_worker_argv

    def recorded_attempt_peek(*peek_args, **peek_kwargs):
        ordering.append("attempt_peek")
        if failure_phase == "after_isolation":
            raise RuntimeError("pre-spawn validation failure")
        return real_attempt_peek(*peek_args, **peek_kwargs)

    monkeypatch.setattr(D, "_attempt_claiming_worker_argv", recorded_attempt_peek)
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
                "stdin_path": kwargs.get("stdin_path"),
                "label": kwargs.get("label"),
            }
        )
        pid = 41000 + len(spawn_calls)
        return pid

    monkeypatch.setattr(D, "_spawn_daemonized_process", fake_spawn)
    retry_home_exists_at_start = bool(recorded_home and Path(recorded_home).is_dir())
    argv = [
        "--agent",
        agent,
        "--unregistered-forced",
        "--dispatch-id",
        dispatch_id,
        "--cwd",
        str(tmp_path),
        "--in-place",
        "--tail",
        str(tmp_path / "bash.tail"),
        "--status-json",
        str(tmp_path / "bash.status.json"),
    ]
    if from_queue:
        argv.append("--from-queue")
    if recorded_home is not None:
        L.write_record(
            {
                "dispatch_id": dispatch_id,
                "state": "queued",
                "codex_home": recorded_home,
                "codex_home_owner_dispatch_id": dispatch_id,
                "effective_account": recorded_account,
                "account": recorded_account or "default",
            }
        )
    if foreground:
        argv.append("--foreground")
    if account is not None:
        canonical_home = Path.home() / ".goal-flight" / "accounts" / account / "codex"
        canonical_home.mkdir(parents=True)
        argv.extend(["--account", account])
    if model is not None:
        argv.extend(["--model", model])
    if prompt_file is not None:
        argv.extend(["--prompt-file", str(prompt_file)])
    argv.extend(preset_options if preset_options is not None else
                ["--", sys.executable, "-c", "pass"])
    rc = D.main(argv)
    if expected_refusal:
        assert rc == 64
        assert not spawn_calls
        return {}, ledger_calls
    assert rc == (1 if failure_phase else 0)
    retry_home_exists = retry_home_exists_at_start
    if resolver_account_calls is not None:
        assert resolve_accounts == resolver_account_calls
    else:
        assert resolve_accounts == (
            [] if api_missing or agent != "codex" or retry_home_exists else [account]
        )
    if not api_missing and agent == "codex" and not retry_home_exists and not from_queue:
        assert ordering.index("resolve") < ordering.index("capacity")
        assert ordering.index("resolve") < ordering.index("ledger:starting")
    else:
        assert ordering.index("capacity") < ordering.index("ledger:starting")
    if failure_phase not in {"pre_spawn", "after_isolation"}:
        assert ordering.index("ledger:starting") < ordering.index("spawn:worker")
        # The attempt peek may busy-wait for the launch budget. It must finish
        # before the claim is stamped with spawn intent: a launcher killed
        # between intent and spawn leaves a claim recovery cannot restore.
        assert ordering.index("attempt_peek") < ordering.index("spawn_intent")
        assert ordering.index("spawn_intent") < ordering.index("spawn:worker")
    worker_spawn = next(
        (call for call in spawn_calls if call["label"] == "worker"),
        {},
    )
    return worker_spawn, ledger_calls


def test_bash_prompt_guidance_uses_inherited_remote_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GOALFLIGHT_REMOTE_CI_CONFIG", raising=False)
    monkeypatch.setenv("GOALFLIGHT_REMOTE_RUN", "/inherited/goalflight-remote-run")
    prompt = tmp_path / "brief.md"
    prompt.write_text("Run the requested work.\n", encoding="utf-8")
    materialized: list[str] = []
    original_materializer = D._materialize_steer_prompt

    def capture_materialized(*args, **kwargs):
        result = original_materializer(*args, **kwargs)
        materialized.append(result)
        return result

    monkeypatch.setattr(D, "_materialize_steer_prompt", capture_materialized)

    worker, _ledger_calls = _stub_bash_launch(
        monkeypatch,
        tmp_path,
        resolved=(None, None),
        dispatch_id="bash-inherited-remote-run",
        prompt_file=prompt,
        preset_options=[],
    )

    assembled_path = Path(materialized[0])
    assembled = assembled_path.read_text(encoding="utf-8")
    assert worker["env"]["GOALFLIGHT_REMOTE_RUN"] == "/inherited/goalflight-remote-run"
    assert "Remote CI: run test suites and other heavy CPU" in assembled
    assert "--cmd-id <configured-command-id>" in assembled


@pytest.mark.parametrize("supports_max", [True, False])
def test_bash_effort_uses_resolved_dispatch_home(
    monkeypatch, tmp_path, capsys, supports_max,
):
    home = tmp_path / "dispatch-home"
    home.mkdir()
    (home / "config.toml").write_text('model = "selected"\n')
    levels = ["high", "max"] if supports_max else ["high"]
    (home / "models_cache.json").write_text(json.dumps({"models": [{
        "slug": "selected",
        "supported_reasoning_levels": [{"effort": level} for level in levels],
    }]}))
    # Inherited home must not win over the home chosen after capacity.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "unrelated-home"))
    worker, _ = _stub_bash_launch(
        monkeypatch, tmp_path, resolved=(str(home), "seat-a"),
        preset_options=["--prompt", "Implement the requested change.",
                        "--reasoning-effort", "max"],
        expected_refusal=not supports_max,
    )
    if supports_max:
        assert worker["env"]["CODEX_HOME"] == str(home)
        assert 'model_reasoning_effort="max"' in worker["argv"]
    else:
        assert "'selected'; supported levels: high" in capsys.readouterr().err


def test_bash_pin_is_applied_before_capacity_and_reaches_spawn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
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
    dispatch_start = next(
        json.loads(line.removeprefix("DISPATCH-START "))
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("DISPATCH-START ")
    )
    row = D.goalflight_journal.Journal(tmp_path).read_all(
        """SELECT effective_account, engine
           FROM dispatch_attempts WHERE dispatch_id = ?""",
        ("bash-seat-seam",),
    )[0]
    assert dispatch_start["effective_account"] == "seat-a"
    assert row["effective_account"] == dispatch_start["effective_account"]
    assert row["effective_account"] != "explicit-seat"
    assert row["engine"] == dispatch_start["engine"] == "codex"


def test_bash_worker_scratch_is_created_and_exported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    worker, _ledger_calls = _stub_bash_launch(
        monkeypatch,
        tmp_path,
        resolved=(None, None),
        dispatch_id="bash-scratch-fresh",
    )

    scratch = Path(worker["env"]["GOALFLIGHT_SCRATCH_DIR"])
    dispatch_id = worker["env"]["GOALFLIGHT_DISPATCH_ID"]

    assert scratch == (
        Path(worker["env"]["GOALFLIGHT_DISPATCH_DIR"])
        / f"{dispatch_id}.scratch"
    )
    assert scratch.is_dir()
    assert worker["env"]["TMPDIR"] == str(scratch)


def test_bash_launch_uses_project_remote_ci_config_and_grants_its_spool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project_root = tmp_path
    config_path = project_root / ".goal-flight" / "remote-ci.json"
    spool = tmp_path / "state" / "remote-ci" / "projects" / "pm2" / "spool"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps({"paths": {
        "queue_dir": str(spool / "requests"),
        "result_dir": str(spool / "results"),
    }}), encoding="utf-8")
    monkeypatch.delenv("GOALFLIGHT_REMOTE_CI_CONFIG", raising=False)

    worker, _ledger_calls = _stub_bash_launch(
        monkeypatch,
        tmp_path,
        resolved=(None, None),
        dispatch_id="bash-project-remote-ci",
    )

    assert worker["env"]["GOALFLIGHT_REMOTE_CI_CONFIG"] == str(config_path)
    assert Path(worker["env"]["GOALFLIGHT_REMOTE_CI_SPOOL_DIR"]) == spool
    environment = {**os.environ, **worker["env"]}
    _profile, roots = S.macos_sandbox_profile(
        str(Path(sys.executable).resolve().parent), S.OS_SANDBOX_WORKSPACE_WRITE,
        agent="codex", command="codex", environment=environment,
    )
    assert (spool / "requests").resolve() in {
        Path(root).resolve() for root in roots
    }


def test_bash_usage_error_does_not_leave_unowned_scratch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dispatch_id = "bash-scratch-refused"

    def refuse_build(*_args, **_kwargs):
        raise D.DispatchUsageError("worker construction refused")

    monkeypatch.setattr(D, "build_worker", refuse_build)
    _stub_bash_launch(
        monkeypatch,
        tmp_path,
        resolved=(None, None),
        dispatch_id=dispatch_id,
        expected_refusal=True,
    )

    dispatch_dir = Path(os.environ["GOALFLIGHT_DISPATCH_DIR"])
    assert not (dispatch_dir / f"{dispatch_id}.scratch").exists()


@pytest.mark.parametrize("from_queue", [False, True])
def test_grok_read_only_build_has_scratch_before_sandbox_and_cleans_refusal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    from_queue: bool,
) -> None:
    dispatch_id = f"grok-scratch-before-build-{int(from_queue)}"
    account_home = D._account_home("probe", "grok")
    account_env = {
        "HOME": str(account_home),
        "XDG_CONFIG_HOME": str(account_home / ".config"),
        "XDG_STATE_HOME": str(account_home / ".local" / "state"),
        "XDG_DATA_HOME": str(account_home / ".local" / "share"),
    }
    monkeypatch.setattr(
        D, "_validate_before_side_effects", lambda *_args: dict(account_env)
    )
    monkeypatch.setattr(
        D, "_resolve_launch_account_env", lambda *_args: dict(account_env)
    )
    monkeypatch.setattr(D, "_validate_os_sandbox_boundary", lambda *_args: None)
    monkeypatch.setattr(D, "_worker_cwd", lambda _args: str(HOST_HOME))
    monkeypatch.setattr(D.goalflight_compat, "is_macos", lambda: True)
    real_prepare_sandbox = S.prepare_os_sandbox_command
    monkeypatch.setattr(S, "preflight_os_sandbox", lambda _profile: "read-only")
    monkeypatch.setattr(S.shutil, "which", lambda _name: "/usr/bin/sandbox-exec")
    sandbox_calls: list[tuple[Path, Path]] = []

    def stop_after_profile_build(
        _command,
        _argv,
        *,
        cwd,
        os_sandbox,
        agent,
        environment,
    ):
        scratch = Path(environment["GOALFLIGHT_SCRATCH_DIR"])
        private_tmp = Path(environment["TMPDIR"])
        assert scratch.is_dir()
        assert private_tmp.is_dir() and private_tmp.parent == scratch
        real_prepare_sandbox(
            _command,
            _argv,
            cwd=cwd,
            os_sandbox=os_sandbox,
            agent=agent,
            environment=environment,
        )
        sandbox_calls.append((scratch, private_tmp))
        raise D.DispatchUsageError("stop after verifying Grok sandbox inputs")

    monkeypatch.setattr(S, "prepare_os_sandbox_command", stop_after_profile_build)
    scratch = Path(os.environ["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.scratch"
    assert not scratch.exists()
    _stub_bash_launch(
        monkeypatch,
        tmp_path,
        resolved=(None, None),
        agent="grok-code",
        account="probe",
        dispatch_id=dispatch_id,
        from_queue=from_queue,
        preset_options=[
            "--read-only",
            "--os-sandbox",
            "read-only",
            "--prompt",
            "Review the change.",
        ],
        expected_refusal=True,
    )

    assert sandbox_calls and sandbox_calls[0][0] == scratch
    assert not scratch.exists()


def test_cursor_bash_sandbox_grants_child_tmpdir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scratch_root = tmp_path / "dispatch.scratch"
    custom_tmp = scratch_root / "cursor-tmp"
    custom_tmp.mkdir(parents=True)
    args = SimpleNamespace(
        agent="cursor",
        os_sandbox="workspace-write",
        cwd=str(HOST_HOME),
        _account_env={
            "GOALFLIGHT_SCRATCH_DIR": str(scratch_root),
            "TMPDIR": str(custom_tmp),
        },
    )
    monkeypatch.setattr(R, "validate_os_sandbox_request", lambda *_args: None)
    monkeypatch.setattr(
        S,
        "preflight_os_sandbox",
        lambda profile: S.OS_SANDBOX_WORKSPACE_WRITE,
    )
    monkeypatch.setattr(S.shutil, "which", lambda _name: "/usr/bin/sandbox-exec")

    wrapped = D._wrap_cursor_os_sandbox(["cursor-agent", "-p"], args)

    profile = wrapped[2]
    assert f'(subpath "{custom_tmp}")' in profile, profile


def test_bash_pinned_resolution_refusal_leaves_no_admission_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    capacity_calls: list[argparse.Namespace] = []
    _worker, ledger_calls = _stub_bash_launch(
        monkeypatch,
        tmp_path,
        resolved=(None, None),
        account="explicit-seat",
        expected_refusal=True,
        resolver_error=D.DispatchUsageError("explicit pin refusal"),
        capacity_calls=capacity_calls,
    )
    assert capacity_calls == []
    assert ledger_calls == []


def test_bash_resolver_only_account_reaches_capacity_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "resolver-only-home"
    home.mkdir()
    capacity_calls: list[argparse.Namespace] = []
    worker_spawn, _ledger_calls = _stub_bash_launch(
        monkeypatch,
        tmp_path,
        resolved=(str(home), "resolver-only"),
        capacity_calls=capacity_calls,
        model="gpt-5.6-luna",
    )
    assert capacity_calls[0]._codex_account_pre_resolved is True
    assert capacity_calls[0]._capacity_account == "resolver-only"
    assert capacity_calls[0].model == "gpt-5.6-luna"
    assert worker_spawn["env"]["CODEX_HOME"] == str(home)


def test_bash_launch_refuses_missing_starting_ledger_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    worker_spawn, ledger_calls = _stub_bash_launch(
        monkeypatch,
        tmp_path,
        resolved=(None, None),
        drop_starting_projection=True,
        foreground=True,
        expected_refusal=True,
    )

    assert worker_spawn == {}
    # Keep the launch projection provisional while the journal terminal event
    # is written, then expose the prelaunch_failed terminal state.
    assert [call["state"] for call in ledger_calls] == [
        "waiting_capacity",
        "waiting_capacity",
        "starting",
        "waiting_capacity",
    ]
    record = L.read_record("bash-seat-seam")
    assert record and record["state"] == "prelaunch_failed", record
    assert "committed STARTING attempt id unavailable" in capsys.readouterr().err


def test_bash_launch_preserves_nonpersisting_ledger_injection_seam(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    D.goalflight_journal.open_or_create_journal(tmp_path)
    worker_spawn, ledger_calls = _stub_bash_launch(
        monkeypatch,
        tmp_path,
        resolved=(None, None),
        persist_ledger=False,
        foreground=True,
    )

    assert worker_spawn["argv"] == [sys.executable, "-c", "pass"]
    assert worker_spawn["env"]["GOALFLIGHT_ATTEMPT_ID"]
    assert [call["state"] for call in ledger_calls] == [
        "waiting_capacity",
        "waiting_capacity",
        "starting",
        "running",
    ]


def test_native_ledger_missing_attempt_still_refuses_worker_launch(
    tmp_path: Path,
) -> None:
    D.goalflight_journal.open_or_create_journal(tmp_path)

    with pytest.raises(
        RuntimeError,
        match="prepared attempt missing for missing-attempt",
    ):
        D._attempt_claiming_worker_argv(
            tmp_path,
            "missing-attempt",
            [sys.executable, "-c", "pass"],
        )


def _forbid_writer_journal(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    recorded: list[dict] = []
    real_open_reader = D.goalflight_journal.Journal.open_reader

    def tracking_open_reader(cls, project_root, **kwargs):  # type: ignore[no-untyped-def]
        recorded.append(dict(kwargs))
        return real_open_reader(project_root, **kwargs)

    def forbid_writer(*_args, **_kwargs):
        raise AssertionError("launch peek used writer Journal()")

    monkeypatch.setattr(
        D.goalflight_journal.Journal,
        "open_reader",
        classmethod(tracking_open_reader),
    )
    monkeypatch.setattr(D.goalflight_journal.Journal, "__init__", forbid_writer)
    return recorded


def _assert_reader_peek_budget(kwargs: dict) -> None:
    retry = float(
        kwargs.get(
            "retry_budget_s",
            D.goalflight_journal.JOURNAL_READER_RETRY_BUDGET_S,
        )
    )
    assert retry <= D.goalflight_journal.JOURNAL_READER_RETRY_BUDGET_S
    assert retry < D.goalflight_journal.JOURNAL_WRITER_RETRY_BUDGET_S


def test_attempt_claiming_worker_argv_peeks_through_open_reader(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    authority = D.goalflight_journal.open_or_create_journal(tmp_path)
    prepared = authority.prepare_attempt("peek-start")
    assert prepared.committed and prepared.value is not None
    started = authority.start_attempt(
        prepared.value.attempt_id,
        prepared.value.launch_token,
        expected_launch_epoch=prepared.value.launch_epoch,
    )
    assert started.committed
    L.write_record(
        {
            "dispatch_id": "peek-start",
            "state": "starting",
            "attempt_id": prepared.value.attempt_id,
        }
    )
    recorded = _forbid_writer_journal(monkeypatch)
    argv, claimed = D._attempt_claiming_worker_argv(
        tmp_path,
        "peek-start",
        [sys.executable, "-c", "pass"],
    )
    assert claimed is True
    assert argv == [sys.executable, "-c", "pass"]
    assert recorded
    # The launch peek reads through open_reader (no writer construction lock)
    # but must not be the first contender to give up on a busy shared journal:
    # it waits as long as a writer would.
    retry = float(
        recorded[0].get(
            "retry_budget_s",
            D.goalflight_journal.JOURNAL_READER_RETRY_BUDGET_S,
        )
    )
    assert retry >= D.goalflight_journal.JOURNAL_WRITER_RETRY_BUDGET_S


def test_queue_launch_token_peeks_through_open_reader(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    authority = D.goalflight_journal.open_or_create_journal(tmp_path)
    prepared = authority.prepare_attempt("peek-queue")
    assert prepared.committed and prepared.value is not None
    recorded = _forbid_writer_journal(monkeypatch)
    token = D._queue_launch_token(
        {
            "dispatch_id": "peek-queue",
            "project_root": str(tmp_path),
        }
    )
    assert token == prepared.value.launch_token
    assert recorded
    _assert_reader_peek_budget(recorded[0])


def test_bash_resolve_none_preserves_inherited_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    worker_spawn, ledger_calls = _stub_bash_launch(
        monkeypatch,
        tmp_path,
        resolved=(None, None),
    )
    assert "CODEX_HOME" not in worker_spawn["env"]
    billed = [
        call.get("effective_account")
        for call in ledger_calls
        if call["state"] in {"starting", "running"}
    ]
    assert billed == ["host", "host"]
    row = D.goalflight_journal.Journal(tmp_path).read_all(
        """SELECT effective_account, engine
           FROM dispatch_attempts WHERE dispatch_id = ?""",
        ("bash-seat-seam",),
    )[0]
    assert row["effective_account"] == "host"
    assert row["engine"] == "codex"


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
    worker_argv = worker_spawn["argv"]
    assert worker_argv == [sys.executable, "-c", "pass"]
    assert "CODEX_HOME" not in worker_spawn["env"]
    assert all(call.get("effective_account") is None for call in ledger_calls)
    assert "per-dispatch home" not in capsys.readouterr().err


def test_bash_missing_resolver_does_not_charge_selected_account(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        D, "select_codex_account", lambda **_kwargs: ("configured-seat", [])
    )
    capacity_calls: list[argparse.Namespace] = []
    _worker_spawn, ledger_calls = _stub_bash_launch(
        monkeypatch,
        tmp_path,
        resolved=(None, None),
        api_missing=True,
        capacity_calls=capacity_calls,
    )
    assert capacity_calls[0]._capacity_account is None
    assert all(call.get("effective_account") is None for call in ledger_calls)


def test_bash_missing_optional_library_keeps_host_home_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    worker, _ledger_calls = _stub_bash_launch(
        monkeypatch,
        tmp_path,
        resolved=(None, None),
        api_missing=True,
        foreground=True,
        persist_ledger=False,
    )
    assert "CODEX_HOME" not in worker["env"]


def test_bash_queue_retry_rebuilds_missing_recorded_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dispatch_id = "bash-missing-home-retry"
    home = D._codex_dispatch_homes_dir() / dispatch_id
    assert not home.exists()
    monkeypatch.setattr(D, "select_codex_account", lambda **_kwargs: (None, []))
    resolver_accounts: list[str | None] = []
    worker, _ledger_calls = _stub_bash_launch(
        monkeypatch,
        tmp_path,
        resolved=(str(home), "seat-a"),
        dispatch_id=dispatch_id,
        from_queue=True,
        recorded_home=str(home),
        recorded_account="seat-a",
        resolver_account_calls=resolver_accounts,
    )
    assert worker["env"]["CODEX_HOME"] == str(home)
    assert home.is_dir()
    assert resolver_accounts == [None]
    record = json.loads(L.record_path(dispatch_id).read_text(encoding="utf-8"))
    assert record["effective_account"] == "seat-a"


def test_bash_queue_retry_rebuilds_missing_home_for_new_admitted_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dispatch_id = "bash-reassigned-home-retry"
    home = D._codex_dispatch_homes_dir() / dispatch_id
    resolver_accounts: list[str | None] = []
    monkeypatch.setattr(
        D, "select_codex_account", lambda **_kwargs: ("seat-b", [])
    )
    worker, ledger_calls = _stub_bash_launch(
        monkeypatch,
        tmp_path,
        resolved=(str(home), "seat-b"),
        dispatch_id=dispatch_id,
        from_queue=True,
        recorded_home=str(home),
        recorded_account="seat-a",
        resolver_account_calls=resolver_accounts,
    )
    assert resolver_accounts == ["seat-b"]
    assert worker["env"]["CODEX_HOME"] == str(home)
    assert home.is_dir()
    starting = next(call for call in ledger_calls if call["state"] == "starting")
    assert starting["effective_account"] == "seat-b"
    record = json.loads(L.record_path(dispatch_id).read_text(encoding="utf-8"))
    assert record["effective_account"] == "seat-b"


def test_bash_queue_retry_refuses_when_recorded_home_cannot_be_rebuilt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dispatch_id = "bash-unrebuildable-home"
    home = D._codex_dispatch_homes_dir() / dispatch_id
    _stub_bash_launch(
        monkeypatch,
        tmp_path,
        resolved=(None, None),
        dispatch_id=dispatch_id,
        from_queue=True,
        recorded_home=str(home),
        expected_refusal=True,
    )
    assert "codex_home_missing" in capsys.readouterr().err
    assert not home.exists()


def test_bash_queue_retry_rejects_non_dispatch_resolver_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dispatch_id = "bash-alternate-home-retry"
    recorded_home = D._codex_dispatch_homes_dir() / dispatch_id
    alternate_home = tmp_path / "alternate-home"
    _stub_bash_launch(
        monkeypatch,
        tmp_path,
        resolved=(str(alternate_home), "seat-a"),
        dispatch_id=dispatch_id,
        from_queue=True,
        recorded_home=str(recorded_home),
        recorded_account="seat-a",
        expected_refusal=True,
    )
    assert "codex_home_missing" in capsys.readouterr().err
    assert alternate_home.is_dir()
    assert not recorded_home.exists()


def test_bash_queue_home_account_mismatch_keeps_existing_rollout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dispatch_id = "bash-account-mismatch"
    home = D._codex_dispatch_homes_dir() / dispatch_id
    rollout = home / "sessions" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text("session data\n", encoding="utf-8")
    monkeypatch.setattr(D, "select_codex_account", lambda **_kwargs: ("seat-b", []))
    cleanups: list[str] = []
    _stub_bash_launch(
        monkeypatch,
        tmp_path,
        resolved=(str(home), "seat-b"),
        account="seat-b",
        cleanups=cleanups,
        dispatch_id=dispatch_id,
        from_queue=True,
        recorded_home=str(home),
        recorded_account="seat-a",
        expected_refusal=True,
    )
    assert "codex_home_account_mismatch" in capsys.readouterr().err
    assert cleanups == []
    assert rollout.read_text(encoding="utf-8") == "session data\n"


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
    assert cleanups == []


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
    assert cleanups == []


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
        priority="normal",
        billing="sub",
        poll_secs=2.0,
        max_idle_secs=300,
        prompt_file=None,
        prompt=None,
        model=None,
        read_only=False,
        os_sandbox=None,
        web_research_ok=False,
        ignore_git_warn=False,
        no_orientation=False,
        capacity_wait_s=None,
        interactive=False,
        permission_mode=None,
        permission_dir=None,
        permission_inline_timeout_s=None,
        permission_user_timeout_s=None,
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
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(D, "_export_dashboard_status_for_project", lambda *_args: None)
    monkeypatch.setattr(D, "_upsert_project_registry_for_dispatch", lambda *_args: None)
    monkeypatch.setattr(D, "_start_dashboard_refresh_for_project", lambda *_args: None)
    args = SimpleNamespace(
        dispatch_id="bash-ledger-pinned",
        task_ids=[],
        agent="codex",
        shape="bash",
        account="requested-seat",
        read_only=False,
        os_sandbox=None,
        controller_pid=None,
        queue_launch_token=None,
        launch_detached=False,
        cwd=None,
        priority="normal",
        billing="sub",
        poll_secs=2.0,
        max_idle_secs=0,
        prompt_file=None,
        prompt=None,
        model=None,
        web_research_ok=False,
        ignore_git_warn=False,
        capacity_wait_s=None,
        interactive=False,
        permission_mode=None,
        permission_dir=None,
        permission_inline_timeout_s=None,
        permission_user_timeout_s=None,
    )
    D._record_ledger(
        args,
        project_root=tmp_path,
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
    assert row["account"] == "requested-seat"
    assert row["effective_account"] == "seat-b"
    journal_row = D.goalflight_journal.Journal(tmp_path).read_all(
        """SELECT effective_account, engine
           FROM dispatch_attempts WHERE dispatch_id = ?""",
        (args.dispatch_id,),
    )[0]
    assert journal_row["effective_account"] == "seat-b"
    assert journal_row["engine"] == "codex"

    unpinned_args = _ledger_args("ledger-unpinned")
    unpinned_args.project_root = str(tmp_path)
    unpinned_args.agent = "worker"
    unpinned_args.engine = "worker"
    L.cmd_record(unpinned_args)
    unpinned = json.loads(
        L.record_path(unpinned_args.dispatch_id).read_text(encoding="utf-8")
    )
    assert "effective_account" not in unpinned
    unpinned_row = D.goalflight_journal.Journal(tmp_path).read_all(
        """SELECT effective_account, engine
           FROM dispatch_attempts WHERE dispatch_id = ?""",
        (unpinned_args.dispatch_id,),
    )[0]
    assert unpinned_row["effective_account"] is None
    assert unpinned_row["engine"] == "worker"


def test_ledger_projection_records_capacity_selected_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(D, "_export_dashboard_status_for_project", lambda *_args: None)
    monkeypatch.setattr(D, "_upsert_project_registry_for_dispatch", lambda *_args: None)
    monkeypatch.setattr(D, "_start_dashboard_refresh_for_project", lambda *_args: None)
    args = _ledger_args("ledger-selected-account")
    args.account = "default"
    args._capacity_account = "emptier-seat"

    D._record_ledger(
        args,
        project_root=tmp_path,
        prompt_path=None,
        status_json=Path("/dev/null"),
        tail=Path("/dev/null"),
        lease_id="lease-selected-account",
        worker_pid=None,
        state="waiting_capacity",
    )

    record = json.loads(
        L.record_path(args.dispatch_id).read_text(encoding="utf-8")
    )
    assert record["effective_account"] == "emptier-seat"


def _acp_cfg(
    tmp_path: Path,
    *,
    account: str | None,
    agent: str = "codex-acp",
    parent_dispatch_id: str | None = None,
    cwd: Path | None = None,
) -> argparse.Namespace:
    return A.normalized_acp_dispatch_cfg(
        SimpleNamespace(
            agent=agent,
            model=None,
            install_slot=None,
            account=account,
            parent_dispatch_id=parent_dispatch_id,
            cwd=str(cwd or tmp_path),
            worktree="off",
            in_place=True,
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
            unregistered_forced=True,
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
    capacity_calls: list[argparse.Namespace] | None = None,
    model: str | None = None,
    resolver_error: BaseException | None = None,
    parent_dispatch_id: str | None = None,
    expect_pre_resolve: bool = False,
    post_capacity_hook=None,
    capacity_lease_id: str | None = None,
    expect_post_capacity_refusal: bool = False,
    api_missing: bool = False,
    recorded_home: str | None = None,
    recorded_account: str | None = None,
    from_queue: bool = False,
    expect_home_refusal: bool = False,
    dispatch_id: str | None = None,
    project_root: Path | None = None,
    worktree_mode: str | None = None,
    spawn_uncertain: bool = False,
) -> tuple[argparse.Namespace, dict[str, str], list[str]]:
    cleanups: list[str] = []
    ordering: list[str] = []
    resolve_accounts: list[str | None] = []

    def resolve_seat(_project_root, explicit_account, _dispatch_id):
        ordering.append("resolve")
        resolve_accounts.append(explicit_account)
        if resolver_error is not None:
            raise resolver_error
        if resolved[0]:
            Path(resolved[0]).mkdir(parents=True, exist_ok=True)
        return resolved

    if api_missing:
        monkeypatch.setattr(D, "_codex_seat_api", lambda: None)
    else:
        monkeypatch.setattr(
            D,
            "_codex_seat_api",
            lambda: SimpleNamespace(
                resolve_codex_seat=resolve_seat,
                cleanup_dispatch_home=lambda dispatch_id: cleanups.append(dispatch_id),
            ),
        )
    resolved_account = resolved[1]
    probe_account = account or resolved_account
    probe_rows = (
        [
            {
                "provider": "codex",
                "account": probe_account,
                "remaining": "80%",
                "reset_at": "2030-01-02T03:04:05Z",
                "flags": [],
                "evidence": {
                    "probe": {"state": "reported", "observed_at": time.time()}
                },
            }
        ]
        if probe_account
        else []
    )
    monkeypatch.setattr(
        D,
        "_codex_usage_probe_rows",
        lambda *_args, **_kwargs: list(probe_rows),
    )
    monkeypatch.setattr(D, "_stamp_controller_session", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        D, "_prepare_attempt_controller_registration", lambda *_args, **_kwargs: None
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

    async def allow_capacity(acquire_args, *_args, **_kwargs):
        ordering.append("capacity")
        if capacity_calls is not None:
            capacity_calls.append(acquire_args)
        if post_capacity_hook is not None:
            post_capacity_hook(probe_rows)
        lease = {"lease_id": capacity_lease_id} if capacity_lease_id else {}
        return {"decision": "allow", "lease": lease}

    monkeypatch.setattr(A.goalflight_capacity, "acquire_with_wait_async", allow_capacity)
    original_cmd_record = L.cmd_record

    def record_with_order(args):
        ordering.append(f"ledger:{args.state}")
        return original_cmd_record(args)

    monkeypatch.setattr(L, "cmd_record", record_with_order)
    captured: dict[str, str] = {}

    async def fail_after_env(*_args, **kwargs):
        admission = kwargs.get("launch_admission")
        with admission() if callable(admission) else nullcontext():
            ordering.append("spawn")
            captured.update(kwargs["env"])
        if spawn_uncertain:
            on_attempt = kwargs.get("on_attempt")
            if callable(on_attempt):
                await on_attempt(0, SimpleNamespace(pid=2_147_483_000))
        raise RuntimeError("stop after spawn env capture")

    if spawn_uncertain:
        monkeypatch.setattr(
            A,
            "termination_result_for_process",
            lambda *, pid, pgid, reason: A.AcpTerminationResult(
                pid=pid,
                pgid=pgid or pid,
                confirmed=False,
                scope_alive=None,
                reason="test leaves spawned worker death unconfirmed",
            ),
        )
    monkeypatch.setattr(A, "spawn_and_handshake_with_retry", fail_after_env)
    cfg = _acp_cfg(
        project_root or tmp_path,
        account=account,
        agent=agent,
        parent_dispatch_id=parent_dispatch_id,
    )
    if worktree_mode is not None:
        cfg.worktree = worktree_mode
        cfg.in_place = False
    if dispatch_id is not None:
        cfg.dispatch_id = dispatch_id
    cfg.model = model
    cfg.request_envelope = request_envelope
    cfg.from_queue = from_queue
    cfg._codex_recorded_home = recorded_home
    cfg._codex_retry_home = (
        recorded_home if recorded_home and Path(recorded_home).is_dir() else None
    )
    cfg._codex_recorded_account = recorded_account
    cfg._codex_selected_account = (
        resolved[1] if from_queue or account is not None else None
    )
    cfg._capacity_account = cfg._codex_selected_account or recorded_account
    if recorded_home is not None:
        L.write_record(
            {
                "dispatch_id": cfg.dispatch_id,
                "state": "queued",
                "codex_home": recorded_home,
                "codex_home_owner_dispatch_id": cfg.dispatch_id,
                "effective_account": recorded_account,
                "account": recorded_account or "default",
            }
        )
    payload = asyncio.run(A.run_acp_dispatch(cfg))
    cfg._test_payload = payload
    cfg._test_ordering = ordering
    cfg._test_resolve_accounts = list(resolve_accounts)
    if block_dispatch_import:
        assert payload["state"] == "blocked_controller_registration"
        assert ordering == []
        assert captured == {}
        assert resolve_accounts == []
        return cfg, captured, cleanups
    assert payload["state"] == "failed"
    if expect_post_capacity_refusal:
        assert "ledger:starting" not in ordering
        assert "spawn" not in ordering
        assert ordering.index("capacity") < len(ordering)
        return cfg, captured, cleanups
    if expect_home_refusal:
        assert "spawn" not in ordering
        assert captured == {}
        return cfg, captured, cleanups
    expect_resolve = (
        L.infer_engine(agent) == "codex"
        and not block_dispatch_import
        and not api_missing
        and not cfg._codex_retry_home
    )
    if expect_resolve:
        if getattr(cfg, "_codex_account_pre_resolved", False):
            assert ordering.index("resolve") < ordering.index("capacity")
        else:
            assert ordering.index("capacity") < ordering.index("resolve")
        assert ordering.index("resolve") < ordering.index("ledger:starting")
    else:
        assert "resolve" not in ordering
        assert ordering.index("capacity") < ordering.index("ledger:starting")
    if expect_pre_resolve:
        assert ordering.index("resolve") < ordering.index("capacity")
    assert ordering.index("ledger:starting") < ordering.index("spawn")
    expected_resolve_account = (
        account
        if getattr(cfg, "_codex_account_pre_resolved", False) and account
        else (
            None
            if getattr(cfg, "_codex_account_pre_resolved", False)
            else getattr(cfg, "_codex_selected_account", None) or account
        )
    )
    assert resolve_accounts == ([expected_resolve_account] if expect_resolve else [])
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
    assert cleanups == []


def test_acp_unselectable_seat_preserves_inherited_env_and_labels_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg, captured, cleanups = _run_acp_to_spawn_failure(
        monkeypatch,
        tmp_path,
        resolved=(None, None),
        account=None,
    )
    occupancy_fd = "GOALFLIGHT_OCCUPANCY_LOCK_FD"
    assert captured["BASE"] == "captured"
    assert set(captured) == {"BASE", occupancy_fd}
    assert captured[occupancy_fd].isdigit()
    record = json.loads(L.record_path(cfg.dispatch_id).read_text(encoding="utf-8"))
    assert record["account"] == "default"
    assert record["effective_account"] == "host"
    assert cleanups == []
    assert cfg.context_mode == "enabled"


def test_acp_queue_retry_rebuilds_missing_recorded_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dispatch_id = "acp-seat-codex-acp-fallback"
    home = D._codex_dispatch_homes_dir() / dispatch_id
    cfg, captured, _cleanups = _run_acp_to_spawn_failure(
        monkeypatch,
        tmp_path,
        resolved=(str(home), "seat-a"),
        account=None,
        recorded_home=str(home),
        recorded_account="seat-a",
        from_queue=True,
        dispatch_id=dispatch_id,
    )
    assert cfg.dispatch_id == dispatch_id
    assert captured["CODEX_HOME"] == str(home)
    assert home.is_dir()
    assert cfg._test_resolve_accounts == ["seat-a"]
    record = json.loads(L.record_path(dispatch_id).read_text(encoding="utf-8"))
    assert record["effective_account"] == "seat-a"


def test_acp_queue_retry_rebuilds_missing_home_for_new_admitted_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dispatch_id = "acp-reassigned-home-retry"
    home = D._codex_dispatch_homes_dir() / dispatch_id
    cfg, captured, _cleanups = _run_acp_to_spawn_failure(
        monkeypatch,
        tmp_path,
        resolved=(str(home), "seat-b"),
        account=None,
        recorded_home=str(home),
        recorded_account="seat-a",
        from_queue=True,
        dispatch_id=dispatch_id,
    )
    assert captured["CODEX_HOME"] == str(home)
    assert home.is_dir()
    assert cfg._test_resolve_accounts == ["seat-b"]
    record = json.loads(L.record_path(dispatch_id).read_text(encoding="utf-8"))
    assert record["effective_account"] == "seat-b"


def test_acp_queue_retry_refuses_when_recorded_home_cannot_be_rebuilt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dispatch_id = "acp-seat-codex-acp-fallback"
    home = D._codex_dispatch_homes_dir() / dispatch_id
    cfg, captured, _cleanups = _run_acp_to_spawn_failure(
        monkeypatch,
        tmp_path,
        resolved=(None, None),
        account=None,
        recorded_home=str(home),
        from_queue=True,
        expect_home_refusal=True,
    )
    assert captured == {}
    assert "codex_home_missing" in json.dumps(cfg._test_payload)
    assert not home.exists()


def test_acp_queue_retry_rejects_non_dispatch_resolver_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dispatch_id = "acp-alternate-home-retry"
    recorded_home = D._codex_dispatch_homes_dir() / dispatch_id
    alternate_home = tmp_path / "alternate-home"
    cfg, captured, _cleanups = _run_acp_to_spawn_failure(
        monkeypatch,
        tmp_path,
        resolved=(str(alternate_home), "seat-a"),
        account=None,
        recorded_home=str(recorded_home),
        recorded_account="seat-a",
        from_queue=True,
        dispatch_id=dispatch_id,
        expect_home_refusal=True,
    )
    assert captured == {}
    assert "codex_home_missing" in json.dumps(cfg._test_payload)
    assert alternate_home.is_dir()
    assert not recorded_home.exists()


def test_acp_queue_home_account_mismatch_keeps_existing_rollout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dispatch_id = "acp-seat-codex-acp-seat-b"
    home = D._codex_dispatch_homes_dir() / dispatch_id
    rollout = home / "sessions" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text("session data\n", encoding="utf-8")
    cfg, captured, cleanups = _run_acp_to_spawn_failure(
        monkeypatch,
        tmp_path,
        resolved=(str(home), "seat-b"),
        account="seat-b",
        recorded_home=str(home),
        recorded_account="seat-a",
        from_queue=True,
        dispatch_id=dispatch_id,
        expect_home_refusal=True,
    )
    assert cfg.dispatch_id == dispatch_id
    assert captured == {}
    assert "codex_home_account_mismatch" in json.dumps(cfg._test_payload)
    assert cleanups == []
    assert rollout.read_text(encoding="utf-8") == "session data\n"


def test_acp_post_admission_home_refusal_releases_captive_seat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Tests"], cwd=repo, check=True
    )
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)

    dispatch_id = "acp-home-refusal-holder"
    home = D._codex_dispatch_homes_dir() / dispatch_id
    home.mkdir(parents=True)
    cfg, captured, _cleanups = _run_acp_to_spawn_failure(
        monkeypatch,
        tmp_path,
        resolved=(str(home), "seat-b"),
        account="seat-b",
        recorded_home=str(home),
        recorded_account="seat-a",
        dispatch_id=dispatch_id,
        project_root=repo,
        worktree_mode="create",
        expect_home_refusal=True,
    )

    assert captured == {}
    assert "spawn" not in cfg._test_ordering
    assert "codex_home_account_mismatch:" in cfg._test_payload["error"]
    D._release_worktree_occupancy_lock(cfg)
    lease = D.goalflight_worktree_pool.acquire_worktree_seat(
        repo, "acp-home-refusal-reclaimed"
    )
    try:
        assert lease.branch == "worktree/acp-home-refusal-reclaimed"
        assert lease.path == Path(cfg._worktree_path)
    finally:
        lease.release()
    record = json.loads(L.record_path(dispatch_id).read_text(encoding="utf-8"))
    assert record["prelaunch_failure"] is True
    assert record["reason"].startswith("codex_home_account_mismatch:")


def test_acp_cfg_passes_recorded_codex_home_and_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True
    )
    monkeypatch.chdir(tmp_path)
    args = D._build_launch_parser().parse_args(
        [
            "--agent", "codex-acp", "--shape", "acp", "--from-queue",
            "--dispatch-id", "acp-recorded-home", "--cwd", str(tmp_path),
            "--in-place", "--no-orientation", "--prompt", "launch",
        ]
    )
    args._codex_recorded_home = str(tmp_path / "dispatch-home")
    args._codex_retry_home = args._codex_recorded_home
    args._codex_recorded_account = "seat-a"
    cfg = D._build_acp_cfg(
        args,
        status_json=tmp_path / "acp.status.json",
        base=tmp_path,
    )
    assert cfg._codex_recorded_home == args._codex_recorded_home
    assert cfg._codex_retry_home == args._codex_retry_home
    assert cfg._codex_recorded_account == "seat-a"


def test_acp_missing_optional_library_keeps_inherited_home_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg, captured, cleanups = _run_acp_to_spawn_failure(
        monkeypatch,
        tmp_path,
        resolved=(None, None),
        account=None,
        api_missing=True,
    )
    assert cfg._test_payload["state"] == "failed"
    occupancy_fd = "GOALFLIGHT_OCCUPANCY_LOCK_FD"
    assert captured["BASE"] == "captured"
    assert set(captured) == {"BASE", occupancy_fd}
    assert captured[occupancy_fd].isdigit()
    assert "CODEX_HOME" not in captured
    assert cleanups == []


def test_acp_capacity_request_keeps_pinned_account_and_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    capacity_calls: list[argparse.Namespace] = []
    cfg, _captured, _cleanups = _run_acp_to_spawn_failure(
        monkeypatch,
        tmp_path,
        resolved=(None, None),
        account="explicit-account",
        capacity_calls=capacity_calls,
        model="gpt-5.6-luna",
    )
    assert capacity_calls[0].account == "explicit-account"
    assert capacity_calls[0].model == "gpt-5.6-luna"


def test_acp_parent_pin_resolves_before_capacity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent_dispatch_id = "parent-dispatch"
    session_id = "00000000-0000-4000-8000-000000000033"
    home = D._codex_dispatch_homes_dir() / parent_dispatch_id
    home.mkdir(parents=True)
    rollout = (
        home
        / "sessions"
        / "2026"
        / "07"
        / "28"
        / f"rollout-2026-07-28T12-00-00-{session_id}.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
    L.write_record(
        {
            "schema": L.SCHEMA,
            "dispatch_id": parent_dispatch_id,
            "agent": "codex",
            "engine": "codex",
            "shape": "bash",
            "account": "parent-seat",
            "effective_account": "parent-seat",
            "project_root": str(tmp_path),
            "worker_cwd": str(tmp_path),
            "state": "blocked",
            "terminal_state": "blocked",
            "started_at": L.utc_now(),
            "codex_session_id": session_id,
            "codex_home": str(home),
            "codex_home_owner_dispatch_id": parent_dispatch_id,
        }
    )
    capacity_calls: list[argparse.Namespace] = []
    _run_acp_to_spawn_failure(
        monkeypatch,
        tmp_path,
        resolved=(str(home), "parent-seat"),
        account="parent-seat",
        parent_dispatch_id=parent_dispatch_id,
        expect_pre_resolve=True,
        capacity_calls=capacity_calls,
    )
    assert capacity_calls[0].account == "parent-seat"


@pytest.mark.parametrize("account", ["explicit-account", "default"])
def test_acp_post_capacity_revalidation_refuses_newly_walled_pinned_account(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    account: str,
) -> None:
    home = tmp_path / "acp-pinned-home"
    home.mkdir()

    def wall_after_capacity(rows: list[dict]) -> None:
        rows[0]["remaining"] = "0%"
        rows[0]["flags"] = ["walled"]
        rows[0]["evidence"]["probe"]["state"] = "walled"

    cfg, captured, cleanups = _run_acp_to_spawn_failure(
        monkeypatch,
        tmp_path,
        resolved=(str(home), account),
        account=account,
        capacity_lease_id="lease-post-capacity",
        post_capacity_hook=wall_after_capacity,
        expect_post_capacity_refusal=True,
    )

    assert captured == {}
    assert cleanups == []
    assert not L.record_path(cfg.dispatch_id).exists()
    assert C.load_state().get("leases") == {}


def test_post_capacity_failover_rebinds_pre_resolved_account_without_reacquire(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    old_home = tmp_path / "old-home"
    new_home = tmp_path / "new-home"
    old_home.mkdir()
    new_home.mkdir()
    now = time.time()
    rows = [
        {
            "provider": "codex",
            "account": "old-seat",
            "remaining": "0%",
            "flags": ["walled"],
            "evidence": {"probe": {"state": "walled", "observed_at": now}},
        },
        {
            "provider": "codex",
            "account": "new-seat",
            "remaining": "80%",
            "flags": [],
            "evidence": {"probe": {"state": "reported", "observed_at": now}},
        },
    ]
    monkeypatch.setattr(D, "_configured_account_names", lambda _engine: ["old-seat", "new-seat"])
    monkeypatch.setattr(
        D, "_codex_usage_probe_rows", lambda *_args, **_kwargs: list(rows)
    )
    monkeypatch.setattr(D, "_account_quota_blocked", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        D.goalflight_capacity,
        "launch_slot_budget",
        lambda _agent, *, account, **_kwargs: {
            "account_remaining": 30,
            "request_weight": 1.0,
            "by_account": {f"codex/{account}": {"active_weight": 0, "cap": 30}},
        },
    )
    monkeypatch.setattr(
        D,
        "resolve_codex_home",
        lambda _root, account, _dispatch_id, **_kwargs: (str(new_home), account),
    )
    rebound: list[tuple[str, str]] = []
    monkeypatch.setattr(
        D.goalflight_capacity,
        "rebind_capacity_lease_account",
        lambda lease_id, account: rebound.append((lease_id, account)) or True,
    )
    waiting = _ledger_args("post-capacity-failover")
    waiting.project_root = str(tmp_path)
    waiting.agent = "codex-acp"
    waiting.engine = "codex"
    waiting.shape = "acp"
    waiting.state = "waiting_capacity"
    waiting.effective_account = "old-seat"
    assert L.cmd_record(waiting) == 0
    cfg = SimpleNamespace(
        agent="codex-acp",
        account=None,
        model=None,
        _codex_account_pre_resolved=True,
        _codex_pre_resolved_home=str(old_home),
        _codex_pre_resolved_account="old-seat",
        _codex_selected_account="old-seat",
        _capacity_account="old-seat",
    )

    result = D.revalidate_codex_account_after_capacity(
        cfg,
        project_root=tmp_path,
        dispatch_id="post-capacity-failover",
        lease_id="lease-1",
    )

    assert result == "new-seat"
    assert rebound == [("lease-1", "new-seat")]
    assert cfg._codex_selected_account == "new-seat"
    assert cfg._codex_pre_resolved_account == "new-seat"
    assert cfg._codex_pre_resolved_home == str(new_home)
    journal_row = D.goalflight_journal.Journal(tmp_path).read_all(
        "SELECT effective_account FROM dispatch_attempts WHERE dispatch_id = ?",
        ("post-capacity-failover",),
    )[0]
    assert journal_row["effective_account"] == "new-seat"


def test_acp_pinned_resolution_refusal_leaves_no_admission_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = _acp_cfg(tmp_path, account="explicit-account")

    with pytest.raises(D.DispatchUsageError, match="explicit pin refusal"):
        _run_acp_to_spawn_failure(
            monkeypatch,
            tmp_path,
            resolved=(None, None),
            account="explicit-account",
            resolver_error=D.DispatchUsageError("explicit pin refusal"),
        )

    assert not L.record_path(cfg.dispatch_id).exists()
    assert C.load_state().get("leases") == {}


@pytest.mark.parametrize(
    ("weekly_used", "expected_reason"),
    [(95.0, "weekly resume reserve"), (80.0, None)],
)
def test_post_capacity_usage_refresh_rechecks_full_admission(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    weekly_used: float,
    expected_reason: str | None,
) -> None:
    cfg = SimpleNamespace(agent="codex", account="pinned-seat", model=None)
    refreshed_rows = [
        {
            "provider": "codex",
            "account": "pinned-seat",
            "weekly_used_percent": weekly_used,
        }
    ]
    usage_rows = iter(([], refreshed_rows))
    monkeypatch.setattr(
        D,
        "_codex_usage_probe_rows",
        lambda *_a, **_k: next(usage_rows),
    )
    monkeypatch.setattr(
        D,
        "_codex_usage_probe_says_usable",
        lambda _account, *, rows=None, **_kwargs: (
            True if rows is refreshed_rows else None
        ),
    )

    if expected_reason is not None:
        with pytest.raises(D.DispatchUsageError, match=expected_reason):
            D.revalidate_codex_account_after_capacity(
                cfg,
                project_root=tmp_path,
                dispatch_id="refresh-then-launch",
            )
    else:
        selected = D.revalidate_codex_account_after_capacity(
            cfg,
            project_root=tmp_path,
            dispatch_id="refresh-then-launch",
        )
        assert selected == "pinned-seat"
        assert cfg._codex_post_capacity_pinned_refusal is False


def test_acp_grok_selection_reaches_capacity_before_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    selected: list[str] = []
    monkeypatch.setattr(
        D,
        "grok_selected_account",
        lambda _cfg: selected.append("healthy") or "healthy",
    )
    capacity_calls: list[argparse.Namespace] = []
    cfg, _captured, _cleanups = _run_acp_to_spawn_failure(
        monkeypatch,
        tmp_path,
        resolved=(None, None),
        account=None,
        agent="grok-acp",
        capacity_calls=capacity_calls,
    )
    assert selected == ["healthy"]
    assert capacity_calls[0].account == "healthy"
    record = json.loads(L.record_path(cfg.dispatch_id).read_text(encoding="utf-8"))
    assert record["account"] == "default"
    assert record["effective_account"] == "healthy"


def test_acp_capacity_uses_selected_account_before_home_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "selected-home"
    home.mkdir()
    monkeypatch.setattr(D, "_configured_account_names", lambda engine: ["healthy"])
    monkeypatch.setattr(
        D,
        "_codex_usage_probe_says_usable",
        lambda account, **kwargs: account == "healthy",
    )
    monkeypatch.setattr(D, "_account_quota_blocked", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        D.goalflight_capacity,
        "launch_slot_budget",
        lambda *args, **kwargs: {
            "account_remaining": 30,
            "request_weight": 0.25,
            "by_account": {"codex/healthy": {"active_weight": 0, "cap": 30}},
        },
    )
    capacity_calls: list[argparse.Namespace] = []
    cfg, _captured, _cleanups = _run_acp_to_spawn_failure(
        monkeypatch,
        tmp_path,
        resolved=(str(home), "healthy"),
        account=None,
        capacity_calls=capacity_calls,
    )
    assert cfg._codex_selected_account == "healthy"
    assert capacity_calls[0].account == "healthy"
    assert capacity_calls[0].model is None


def test_acp_resolver_only_account_reaches_capacity_before_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "resolver-only-home"
    home.mkdir()
    monkeypatch.setattr(
        D,
        "_codex_usage_probe_says_usable",
        lambda account, **kwargs: True,
    )
    capacity_calls: list[argparse.Namespace] = []
    cfg, _captured, _cleanups = _run_acp_to_spawn_failure(
        monkeypatch,
        tmp_path,
        resolved=(str(home), "resolver-only"),
        account=None,
        capacity_calls=capacity_calls,
        model="gpt-5.6-luna",
    )
    assert cfg._codex_account_pre_resolved is True
    assert cfg._codex_selected_account == "resolver-only"
    assert capacity_calls[0].account == "resolver-only"
    assert capacity_calls[0].model == "gpt-5.6-luna"


def test_acp_dispatcher_import_failure_refuses_before_capacity_or_spawn(
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
    assert captured == {}
    assert cfg.context_mode == "disabled"
    assert cleanups == []
    assert import_attempts == ["goalflight_dispatch"]


def test_non_codex_acp_requires_dispatcher_integration_for_registration_gate(
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
    assert captured == {}
    assert cfg.context_mode == "disabled"
    assert cleanups == []
    assert import_attempts == ["goalflight_dispatch"]


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
    state_dir = Path(os.environ["GOALFLIGHT_CODEX_STATE_DIR"])
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "codex-seat-states.json").write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": time.time(),
                "seats": {"seat-e2e": {"healthy": True, "worst_used": 20.0}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        D,
        "_codex_usage_probe_rows",
        lambda *_args, **_kwargs: [
            {
                "provider": "codex",
                "account": "seat-e2e",
                "remaining": "80%",
                "reset_at": "2030-01-02T03:04:05Z",
                "flags": [],
                "evidence": {
                    "probe": {"state": "reported", "observed_at": time.time()}
                },
            }
        ],
    )
    (package.parent / "sitecustomize.py").write_text(
        f"import sys\n"
        f"sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
        "import time as _time\n"
        "import goalflight_usage as _usage\n"
        "_usage.collect_usage = lambda **_kwargs: [{\n"
        "    'provider': 'codex',\n"
        "    'account': 'seat-e2e',\n"
        "    'remaining': '80%',\n"
        "    'reset_at': '2030-01-02T03:04:05Z',\n"
        "    'flags': [],\n"
        "    'evidence': {'probe': {'state': 'reported', 'observed_at': _time.time()}},\n"
        "}]\n",
        encoding="utf-8",
    )
    (Path.home() / ".goal-flight" / "accounts" / "seat-e2e" / "codex").mkdir(
        parents=True,
        exist_ok=True,
    )


def _usage_limit_text(days_ahead: int = 30) -> str:
    """Real usage-limit phrasing with a reset that is ALWAYS in the future.

    The vendor prints an absolute local timestamp ("try again at Aug 8th, 2026
    1:41 PM"). Pinning a literal date here made the fixture a time bomb: it was
    two days ahead when written and silently became a PAST reset, at which point
    the requeue correctly declines to schedule a relaunch at an elapsed instant
    and `not_before` is None -- so the test failed for a reason that had nothing
    to do with the behaviour under test. Derive the date instead, so "exhausted
    WITH a measured future reset" stays the thing being asserted.
    """
    when = datetime.now().astimezone() + timedelta(days=days_ahead)
    suffix = (
        "th"
        if 11 <= when.day <= 13
        else {1: "st", 2: "nd", 3: "rd"}.get(when.day % 10, "th")
    )
    stamp = (
        f"{when.strftime('%b')} {when.day}{suffix}, {when.year} "
        f"{when.strftime('%I').lstrip('0') or '12'}:{when.strftime('%M %p')}"
    )
    return (
        "ERROR: You've hit your usage limit. Visit "
        "https://chatgpt.com/codex/settings/usage to purchase more credits "
        f"or try again at {stamp}."
    )


@pytest.mark.parametrize(
    ("failure_kind", "worker_text", "expect_state"),
    [
        ("auth", "HTTP 401 Unauthorized", None),
        # Real usage-limit phrasing from a 2026-08-05 seat exhaustion; must
        # classify as the measured exhausted kind, not the legacy literal.
        (
            "quota",
            _usage_limit_text(),
            "quota_exhausted",
        ),
        # Exhaustion phrasing with NO parseable reset: still the exhausted
        # kind (the words mean consumption); the missing reset is a separate
        # fact carried as hold_reset_unknown in the retry policy.
        ("quota", "quota exceeded", "quota_exhausted"),
    ],
)
def test_queued_terminal_reconcile_requeues_exactly_once_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_kind: str,
    worker_text: str,
    expect_state: str | None,
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
    dispatch_argv = [
            "--agent",
            "codex",
            "--account",
            "seat-e2e",
            "--unregistered-forced",
            "--shape",
            "bash",
                "--dispatch-id",
                dispatch_id,
                "--cwd",
                str(tmp_path),
                "--in-place",
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
            "--",
            sys.executable,
            "-c",
            worker_code,
        ]
    queue_dir = D._dispatch_queue_dir()
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue_path = D._queue_entry_path(dispatch_id, queue_dir=queue_dir)
    now = L.utc_now()
    D._write_json_atomic(
        queue_path,
        {
            "schema": D.DISPATCH_QUEUE_SCHEMA,
            "state": "queued",
            "dispatch_id": dispatch_id,
            "agent": "codex",
            "shape": "bash",
            "project_root": str(tmp_path),
            "process_cwd": str(tmp_path),
            "created_at": now,
            "updated_at": now,
            "queue_path": str(queue_path),
            "task_ids": [task_id],
            "dispatch_argv": dispatch_argv,
            "request": {
                "agent": "codex",
                "cwd": str(tmp_path),
                "task_ids": [task_id],
                "tail": str(tail),
                "status_json": str(status),
            },
        },
    )
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
        assert terminal["state"] == expect_state
        assert D._requeue_failure_kind(terminal, tail) == "quota"
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
    if failure_kind == "quota" and terminal.get("state") == "quota_exhausted":
        reset_at = (terminal.get("limit_evidence") or {}).get("reset_at") or terminal.get("reset_at")
        if reset_at:
            # Exhausted WITH a measured reset: the child relaunches AT the
            # reset, not never and not immediately. This is the requeue half
            # of the state split; without it the split only renames the death.
            assert child.get("not_before") == reset_at, child
        else:
            # Exhausted with reset unknown: falls back to the account cooldown
            # (may be None when no cooldown is recorded) -- but must NOT be an
            # invented eligibility timestamp.
            assert "not_before" in child or child.get("not_before") is None

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
    state_dir.mkdir(parents=True, exist_ok=True)
    cooldown = "2030-01-02T03:04:05Z"
    (state_dir / "codex-seat-states.json").write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": time.time(),
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
            cross_project=True,
        )
    )
    assert payload["launched"] == 0
    assert payload["left_queued"] == 1
    assert payload["remaining"] == 1
    assert len(payload["details"]) == 1
    row = payload["details"][0]
    assert row["dispatch_id"] == "future-requeue"
    assert row["state"] == "queued"
    assert row["reason"] == "not_before"
    assert row["not_before"] == "2999-01-02T03:04:05Z"
    assert row["winner"] in {"probe", "dispatch", "none"}
    assert "winner:" in str(row.get("headroom") or "")
    assert "unknown time" in str(row.get("winner_age") or "")
    assert "age " not in str(row.get("winner_age") or "")
    assert "age 0m" not in str(row.get("headroom") or "")
    holds_nb = (payload.get("holds") or {}).get("not_before") or {}
    assert holds_nb.get("count") == 1
    assert holds_nb.get("winner") == row["winner"]
    assert holds_nb.get("winner_age") == row["winner_age"]


def test_not_before_contradicted_by_newer_healthy_probe_no_longer_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A frozen reset prediction must not hold a seat that now probes healthy.

    Re-derived at drain time; the queue file is not mutated. Injected probe
    rows stand in for the usage reader so this never hits a billing endpoint.
    """
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir()
    queue_path = queue_dir / "healthy-retry.json"
    D._write_json_atomic(
        queue_path,
        {
            "schema": D.DISPATCH_QUEUE_SCHEMA,
            "state": "queued",
            "dispatch_id": "healthy-retry",
            "agent": "codex",
            "created_at": L.utc_now(),
            "not_before": "2026-09-01T11:44:00Z",
            "request": {
                "not_before": "2026-09-01T11:44:00Z",
                "account": "25ca6b",
                "agent": "codex",
            },
            "dispatch_argv": ["--agent", "codex", "--account", "25ca6b"],
        },
    )
    monkeypatch.setattr(
        D,
        "_NOT_BEFORE_PROBE_ROWS",
        [
            {
                "provider": "codex",
                "account": "25ca6b",
                "remaining": "100%",
                "reset_at": None,
                "flags": [],
                "evidence": {
                    "probe": {
                        "source": "quota_probe",
                        "state": "reported",
                        "observed_at": datetime.fromisoformat(
                            "2026-08-27T18:00:00+00:00"
                        ).timestamp(),
                    },
                    "dispatch": None,
                    "conflict": False,
                },
            }
        ],
    )
    L.write_record(
        {
            "schema": L.SCHEMA,
            "dispatch_id": "stale-wall",
            "agent": "codex",
            "effective_account": "25ca6b",
            "state": "quota_exhausted",
            "limit_kind": "exhausted",
            "reset_at": "2026-09-01T11:44:00Z",
            "ended_at": "2026-08-26T00:48:00+00:00",
        }
    )
    payload = D._drain_queue_once(
        argparse.Namespace(
            queue_dir=str(queue_dir),
            remote_node=None,
            capacity_wait_s=0.0,
            claim_stale_s=D.QUEUE_CLAIM_STALE_S,
            limit=0,
            cross_project=True,
        )
    )
    not_before_holds = [
        row
        for row in payload.get("details") or []
        if row.get("reason") == "not_before"
    ]
    assert not_before_holds == [], payload
    leftover = json.loads(queue_path.read_text(encoding="utf-8"))
    assert leftover["not_before"] == "2026-09-01T11:44:00Z"


def _drain_held_not_before(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    dispatch_id: str,
    probe_row: dict,
    ledger_record: dict | None,
) -> tuple[dict, dict]:
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir()
    queue_path = queue_dir / f"{dispatch_id}.json"
    not_before = "2999-01-02T03:04:05Z"
    D._write_json_atomic(
        queue_path,
        {
            "schema": D.DISPATCH_QUEUE_SCHEMA,
            "state": "queued",
            "dispatch_id": dispatch_id,
            "agent": "codex",
            "created_at": L.utc_now(),
            "not_before": not_before,
            "request": {
                "not_before": not_before,
                "account": "25ca6b",
                "agent": "codex",
            },
            "dispatch_argv": ["--agent", "codex", "--account", "25ca6b"],
        },
    )
    monkeypatch.setattr(D, "_NOT_BEFORE_PROBE_ROWS", [probe_row])
    if ledger_record is not None:
        L.write_record(ledger_record)
    payload = D._drain_queue_once(
        argparse.Namespace(
            queue_dir=str(queue_dir),
            remote_node=None,
            capacity_wait_s=0.0,
            claim_stale_s=D.QUEUE_CLAIM_STALE_S,
            limit=0,
            cross_project=True,
        )
    )
    leftover = json.loads(queue_path.read_text(encoding="utf-8"))
    assert leftover["not_before"] == not_before
    holds = [
        row
        for row in payload.get("details") or []
        if row.get("reason") == "not_before"
    ]
    assert len(holds) == 1, payload
    return holds[0], payload


def test_not_before_hold_detail_names_winning_probe_and_age(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held not_before row must name the live probe, not only the timestamp.

    Fresh exhaustion probe vs stale healthy dispatch: the entry stays gated
    (verdict is not healthy) and the detail uses the same winner vocabulary
    as the usage table. Queue bytes are not rewritten.
    """
    probe_at = time.time() - 5 * 86400
    row, payload = _drain_held_not_before(
        tmp_path,
        monkeypatch,
        dispatch_id="held-probe-win",
        probe_row={
            "provider": "codex",
            "account": "25ca6b",
            "remaining": "0%",
            "reset_at": None,
            "flags": ["walled"],
            "evidence": {
                "probe": {
                    "source": "quota_probe",
                    "state": "walled",
                    "observed_at": probe_at,
                },
                "dispatch": None,
                "conflict": False,
            },
        },
        ledger_record={
            "schema": L.SCHEMA,
            "dispatch_id": "stale-ok",
            "agent": "codex",
            "effective_account": "25ca6b",
            "state": "complete",
            "ended_at": "2026-08-20T00:48:00+00:00",
        },
    )
    assert row["winner"] == "probe", row
    assert row["verdict"] == "exhausted", row
    assert "winner: probe → exhausted" in row["headroom"], row
    assert "age " in row["winner_age"], row
    assert "age " in row["headroom"], row
    holds_nb = (payload.get("holds") or {}).get("not_before") or {}
    assert holds_nb.get("count") == 1, holds_nb
    assert holds_nb.get("winner") == "probe", holds_nb
    assert holds_nb.get("winner_age") == row["winner_age"], holds_nb


def test_not_before_hold_detail_names_winning_dispatch_and_age(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reverse: a newer dispatch wall beating a stale healthy probe is named."""
    now = time.time()
    row, payload = _drain_held_not_before(
        tmp_path,
        monkeypatch,
        dispatch_id="held-dispatch-win",
        probe_row={
            "provider": "codex",
            "account": "25ca6b",
            "remaining": "92%",
            "reset_at": None,
            "flags": [],
            "evidence": {
                "probe": {
                    "source": "quota_probe",
                    "state": "reported",
                    "observed_at": now - 5 * 86400,
                },
                "dispatch": None,
                "conflict": False,
            },
        },
        ledger_record={
            "schema": L.SCHEMA,
            "dispatch_id": "fresh-wall",
            "agent": "codex",
            "effective_account": "25ca6b",
            "state": "quota_exhausted",
            "limit_kind": "exhausted",
            "reset_at": "2026-09-01T11:44:00Z",
            "ended_at": datetime.fromtimestamp(
                now - 60, tz=datetime.now().astimezone().tzinfo
            ).isoformat(),
        },
    )
    assert row["winner"] == "dispatch", row
    assert row["verdict"] == "exhausted", row
    assert "winner: dispatch → exhausted" in row["headroom"], row
    assert "age " in row["winner_age"], row
    holds_nb = (payload.get("holds") or {}).get("not_before") or {}
    assert holds_nb.get("count") == 1, holds_nb
    assert holds_nb.get("winner") == "dispatch", holds_nb
    assert holds_nb.get("winner_age") == row["winner_age"], holds_nb


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


def test_quota_reconcile_writes_the_measured_limit_state(tmp_path: Path) -> None:
    """_quota_limited_state_reason must return the kind it measured.

    The reconcile path re-examines idle_timeout/worker_dead/legacy deaths whose
    tails carry a limit signature. Hardcoding the legacy literal here kept
    minting new kind-unknown records from the one code path that had just
    classified the kind -- the field-asserting-unmeasured-state class.
    """
    tail = tmp_path / "reconcile.tail"
    tail.write_text(_usage_limit_text() + "\n", encoding="utf-8")
    state, reason = D._quota_limited_state_reason(
        "worker_dead", None, tail, agent="codex"
    )
    assert state == "quota_exhausted", (state, reason)
    assert isinstance(reason, dict) and reason.get("limit_state") == "quota_exhausted"

    # A state outside the reconcile-input set passes through untouched.
    state2, _ = D._quota_limited_state_reason("complete", None, tail, agent="codex")
    assert state2 == "complete"
